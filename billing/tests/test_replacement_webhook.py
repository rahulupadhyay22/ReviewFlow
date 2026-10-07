"""07-plan-change-replacement, "Webhook and event handling": the existing signed
receiver also matches the replacement ref (extended billing_ref_lookup); an event
naming the retired ref does not match the row and is acknowledged and ignored.

Every request is signed locally with a test secret. Razorpay is the FakeProvider;
nothing here claims any Razorpay behavior. On-commit callbacks are run with
django_capture_on_commit_callbacks (Celery eager), as in test_webhook.py."""
import json

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from billing import services
from billing.models import BillingEvent, Subscription
from billing.tests.replacement_helpers import RETIRED_REF, REPL_REF, provide_replacement, set_replacement, set_retired
from billing.tests.snapshot_helpers import START, entity
from billing.tests.state_helpers import read_state
from billing.tests.webhook_helpers import SECRET, deliver, event, sign
from core.tenancy import tenant_atomic, tenant_context

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("webhook_secret")]


@pytest.fixture
def w(lifecycle_setup):
    """A is ACTIVE on the small plan with a pending upgrade replacement to the big
    plan; B is ACTIVE with nothing pending."""
    w = lifecycle_setup
    w.sub(w.a, "ACTIVE", plan=w.small)
    w.sub(w.b, "ACTIVE", plan=w.small)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START)
    set_replacement(w.a.merchant, plan=w.big)
    return w


def stored(owner):
    """(provider_event_id, provider_ref, processed) for the merchant's events."""
    with tenant_context(owner.merchant.id), tenant_atomic():
        return sorted(
            (e.provider_event_id, e.provider_ref, e.processed_at is not None) for e in BillingEvent.objects.all()
        )


def row(owner):
    with tenant_context(owner.merchant.id), tenant_atomic():
        return Subscription.objects.select_related("plan").get()


def test_a_signed_event_naming_the_replacement_ref_reaches_the_row_and_is_stored_with_that_ref(
    w, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        resp = deliver(event(ref=REPL_REF, status="authenticated"))
    assert resp.status_code == 200 and resp.json() == {}
    assert stored(w.a) == [("evt_test_0001", REPL_REF, False)]  # the ref the event named
    assert stored(w.b) == []  # the merchant came from the row, not from anything else
    assert len(callbacks) == 1  # one sync enqueued, after commit
    assert w.provider.calls == []  # the receiver itself makes no provider call


def test_the_triggered_sync_fetches_the_replacement_and_changes_nothing_while_it_is_unpaid(
    w, django_capture_on_commit_callbacks
):
    provide_replacement(w.provider, w.big, status="authenticated", paid=False)
    before = read_state(w.a.merchant)
    with django_capture_on_commit_callbacks(execute=True):
        assert deliver(event(ref=REPL_REF, status="authenticated")).status_code == 200
    assert ("fetch_subscription", REPL_REF) in w.provider.calls  # the sync read the replacement
    after = read_state(w.a.merchant)
    assert (after.status, after.period, after.audits, after.usages, after.payments) == (
        before.status,
        before.period,
        before.audits,
        before.usages,
        before.payments,
    )
    assert after.replacement_provider_ref == REPL_REF


def test_the_triggered_sync_switches_a_paid_replacement_and_marks_the_event_processed(
    w, django_capture_on_commit_callbacks
):
    provide_replacement(w.provider, w.big)  # active and a qualifying paid invoice
    with django_capture_on_commit_callbacks(execute=True):
        assert deliver(event(ref=REPL_REF)).status_code == 200
    sub = row(w.a)
    assert (sub.plan_id, sub.payment_provider_ref, sub.replacement_provider_ref) == (w.big.pk, REPL_REF, None)
    assert read_state(w.a.merchant).audits == ["billing.plan_changed"]
    assert stored(w.a) == [("evt_test_0001", REPL_REF, True)]


def test_the_same_replacement_event_delivered_three_times_is_one_event_and_one_enqueue(
    w, django_capture_on_commit_callbacks
):
    enqueued = 0
    for _ in range(3):
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            assert deliver(event(ref=REPL_REF, status="authenticated")).status_code == 200
        enqueued += len(callbacks)
    assert stored(w.a) == [("evt_test_0001", REPL_REF, False)] and enqueued == 1


@pytest.mark.parametrize("signature", [None, "", "0" * 64, "wrong-key"])
def test_an_unsigned_or_invalidly_signed_replacement_event_is_401_with_no_read_and_nothing_stored(w, signature):
    body = event(ref=REPL_REF, status="authenticated")
    if signature == "wrong-key":
        signature = sign(json.dumps(body).encode(), "another-" + SECRET)
    with CaptureQueriesContext(connection) as ctx:
        resp = deliver(body, signature=signature)
    assert resp.status_code == 401
    assert ctx.captured_queries == []  # signature first: no lookup, no write, nothing read
    assert stored(w.a) == [] and stored(w.b) == []
    assert w.provider.calls == []


def test_an_event_naming_the_retired_ref_is_acknowledged_and_ignored(w, django_capture_on_commit_callbacks):
    set_retired(w.a.merchant)
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        resp = deliver(event(ref=RETIRED_REF))
    assert resp.status_code == 200 and resp.json() == {}
    assert stored(w.a) == [] and stored(w.b) == []  # no BillingEvent
    assert callbacks == [] and w.provider.calls == []  # no sync triggered


def test_after_a_switch_an_event_naming_the_old_ref_is_acknowledged_and_ignored(
    w, django_capture_on_commit_callbacks
):
    old_ref = w.ref(w.a)
    provide_replacement(w.provider, w.big)
    with tenant_context(w.a.merchant.id):
        services._sync_replacement()  # the switch only: the old ref now sits in the retired slot
    assert row(w.a).retired_provider_ref == old_ref
    calls_before = list(w.provider.calls)
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        resp = deliver(event(ref=old_ref))
    assert resp.status_code == 200 and resp.json() == {}
    assert stored(w.a) == [] and callbacks == []
    assert w.provider.calls == calls_before
    with django_capture_on_commit_callbacks(execute=False) as callbacks:  # the new ref still matches
        assert deliver(event(ref=REPL_REF), event_id="evt_test_0002").status_code == 200
    assert stored(w.a) == [("evt_test_0002", REPL_REF, False)] and len(callbacks) == 1
