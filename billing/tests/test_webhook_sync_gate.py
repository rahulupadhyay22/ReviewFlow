"""Razorpay billing webhook and its sync task: exactly one post-commit enqueue
per newly stored event, settled-only marking seen through the webhook path
(unknown plan, paused, unpaid then paid), cross-tenant isolation of the task's
fetch and marking, the real task's D1 fail-closed behaviour, task idempotency
and log hygiene of a failed sync.

Razorpay is the FakeProvider from conftest (never the network). The D1 invoice
fetch is deliberately NOT implemented or worked around: the one test that
touches the real fetch_invoices expects its NotImplementedError. The W7 sweep
does not exist yet, so nothing here relies on it."""
import logging

import pytest

from billing import razorpay, tasks
from billing.exceptions import BillingProviderUnavailable
from billing.models import BillingEvent, Subscription
from billing.tests.webhook_helpers import (
    ORIGINAL_FETCH_INVOICES,
    REF,
    deliver,
    deliver_committed,
    event,
    processed,
    rows,
)
from core.tenancy import tenant_atomic, tenant_context

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("webhook_secret")]

# === exactly one post-commit enqueue per newly stored event ==========================


def test_each_newly_stored_event_enqueues_exactly_one_task_and_duplicates_and_rejections_none(
    webhook_setup, queued, django_capture_on_commit_callbacks
):
    a = webhook_setup.a.merchant
    per_request = []
    requests = [
        ("evt_1", event(), "__sign__"),
        ("evt_2", event(), "__sign__"),
        ("evt_2", event(), "__sign__"),  # duplicate delivery
        ("evt_3", event(), "__sign__"),
        ("evt_4", event(), "0" * 64),  # rejected: invalid signature
        ("evt_5", event(ref="sub_NobodyKnowsThis"), "__sign__"),  # no matching subscription
    ]
    for event_id, body, signature in requests:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            deliver(body, event_id=event_id, signature=signature)
        per_request.append(len(callbacks))
        for callback in callbacks:
            callback()
    assert per_request == [1, 1, 0, 1, 0, 0]
    assert queued == [a.id] * 3
    assert sorted(processed(a)) == ["evt_1", "evt_2", "evt_3"]


# === the task never crosses tenants ===================================================


def test_a_task_for_one_merchant_never_fetches_for_or_marks_another_merchants_event(
    webhook_setup, queued, django_capture_on_commit_callbacks
):
    a, b = webhook_setup.a.merchant, webhook_setup.b.merchant
    webhook_setup.provider.add(
        "sub_B", status="active", plan_id=webhook_setup.sub_b.plan.provider_plan_id, current_start=1, current_end=2
    )
    deliver_committed(django_capture_on_commit_callbacks, event(), event_id="evt_a")
    deliver_committed(django_capture_on_commit_callbacks, event(ref="sub_B"), event_id="evt_b")
    assert queued == [a.id, b.id]

    tasks.sync_subscription(a.id)

    assert webhook_setup.provider.calls[0] == ("fetch_subscription", REF)
    assert all(call[1] == REF for call in webhook_setup.provider.calls)  # B's ref never fetched
    assert processed(a) == {"evt_a": True}
    assert processed(b) == {"evt_b": False}  # A's sync could not mark B's event
    with tenant_context(b.id), tenant_atomic():
        sub_b = Subscription.objects.get()
        assert sub_b.provider_synced_at is None and sub_b.status == "ACTIVE"


# === settled-only marking, seen through the webhook path ================================


def test_an_unknown_provider_plan_never_settles_so_every_task_fetches(
    webhook_setup, queued, django_capture_on_commit_callbacks
):
    a = webhook_setup.a.merchant
    webhook_setup.provider.entities[REF]["plan_id"] = "plan_NotInReviewFlow"
    for n in range(3):
        deliver_committed(django_capture_on_commit_callbacks, event(), event_id=f"evt_plan_{n}")
    for merchant_id in queued:
        tasks.sync_subscription(merchant_id)
    assert webhook_setup.provider.count("fetch_subscription") == 3
    assert set(processed(a).values()) == {False}
    r = rows(a)
    assert r["status"] == "INCOMPLETE" and r["usage"] == 0 and r["payments"] == []


def test_a_paused_provider_state_settles_the_events_without_granting_entitlement(
    webhook_setup, queued, django_capture_on_commit_callbacks
):
    """A successfully reconciled no-change snapshot is settled (SnapshotResult),
    so its events are marked and later tasks make no fetch; it never entitles."""
    a = webhook_setup.a.merchant
    webhook_setup.provider.entities[REF]["status"] = "paused"
    deliver_committed(django_capture_on_commit_callbacks, event(), event_id="evt_paused_1")
    deliver_committed(django_capture_on_commit_callbacks, event(), event_id="evt_paused_2")
    for merchant_id in queued:
        tasks.sync_subscription(merchant_id)
    assert webhook_setup.provider.count("fetch_subscription") == 1
    assert set(processed(a).values()) == {True}
    r = rows(a)
    assert r["status"] == "INCOMPLETE" and r["usage"] == 0 and r["payments"] == [] and r["audits"] == []


def test_events_left_unprocessed_by_an_unpaid_period_are_all_settled_once_it_is_paid(
    webhook_setup, queued, django_capture_on_commit_callbacks
):
    a = webhook_setup.a.merchant
    paid_invoices = webhook_setup.provider.invoices
    webhook_setup.provider.invoices = []
    for n in range(3):
        deliver_committed(django_capture_on_commit_callbacks, event(), event_id=f"evt_unpaid_{n}")
    for merchant_id in queued:
        tasks.sync_subscription(merchant_id)
    assert set(processed(a).values()) == {False} and rows(a)["status"] == "INCOMPLETE"

    webhook_setup.provider.invoices = paid_invoices  # the payment lands
    deliver_committed(django_capture_on_commit_callbacks, event(), event_id="evt_paid")
    tasks.sync_subscription(queued[-1])

    assert set(processed(a).values()) == {True} and len(processed(a)) == 4  # nothing stranded
    r = rows(a)
    assert r["status"] == "ACTIVE" and r["usage"] == 1 and r["payments"] == ["pay_TestPay0000001"]
    assert r["audits"] == ["billing.subscription_activated"]


# === task idempotency =====================================================================


def test_running_the_task_again_after_it_settled_changes_nothing_and_makes_no_call(
    webhook_setup, queued, django_capture_on_commit_callbacks
):
    a = webhook_setup.a.merchant
    deliver_committed(django_capture_on_commit_callbacks, event(), event_id="evt_once")
    tasks.sync_subscription(a.id)
    before, calls_before = rows(a), list(webhook_setup.provider.calls)
    tasks.sync_subscription(a.id)
    tasks.sync_subscription(a.id)
    assert rows(a) == before
    assert webhook_setup.provider.calls == calls_before


# === D1: the real invoice fetch fails closed through the real task ===========================


def test_d1_the_task_surfaces_the_unimplemented_invoice_fetch_and_applies_nothing(
    webhook_setup, queued, monkeypatch, django_capture_on_commit_callbacks
):
    """Not caught anywhere (spec D1): no path pretends the reconciliation
    succeeded. D1 itself is neither implemented nor worked around here."""
    a, b = webhook_setup.a.merchant, webhook_setup.b.merchant
    monkeypatch.setattr(razorpay, "fetch_invoices", ORIGINAL_FETCH_INVOICES)
    deliver_committed(django_capture_on_commit_callbacks, event(), event_id="evt_d1")

    with pytest.raises(NotImplementedError):
        tasks.sync_subscription(a.id)

    r = rows(a)
    assert r["status"] == "INCOMPLETE" and r["usage"] == 0 and r["payments"] == [] and r["audits"] == []
    assert processed(a) == {"evt_d1": False}
    with tenant_context(a.id), tenant_atomic():
        sub = Subscription.objects.get()
        assert sub.current_period_start is None and sub.current_period_end is None
        assert sub.provider_synced_at is None
    assert rows(b)["status"] == "ACTIVE"


# === log hygiene of a failed sync ===============================================================


def test_a_failed_sync_logs_the_error_class_and_merchant_only(
    webhook_setup, queued, caplog, django_capture_on_commit_callbacks
):
    a = webhook_setup.a.merchant
    webhook_setup.provider.fetch_error = BillingProviderUnavailable()
    deliver_committed(django_capture_on_commit_callbacks, event(), event_id="evt_secret_id_77")
    with caplog.at_level(logging.DEBUG):
        tasks.sync_subscription(a.id)
    text = caplog.text
    assert "BillingProviderUnavailable" in text and str(a.id) in text
    for forbidden in (REF, "evt_secret_id_77", "pay_TestPay0000001", "inv_Test1"):
        assert forbidden not in text
    assert processed(a) == {"evt_secret_id_77": False}
    with tenant_context(a.id), tenant_atomic():
        assert BillingEvent.objects.filter(processed_at__isnull=True).count() == 1
