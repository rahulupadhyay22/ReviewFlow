"""Billing maintenance tasks (spec 07 W7): the Beat entry, the per-merchant
fan-out and its failure isolation, the two independent per-merchant tasks,
dunning independence from the sync, event handling, recovery of webhook
events, idempotency, tenant isolation, log hygiene and the D1 fail-closed
behaviour.

Razorpay is the FakeProvider from conftest (never the network). Plain django_db
tests never commit, so the on-commit enqueue of the fan-out is observed with
django_capture_on_commit_callbacks(execute=True). The tasks are called
directly (their bodies run inline); the expiry-versus-sync race rules have
their own files."""
import inspect
import logging
from datetime import timedelta

import pytest
from django.conf import settings
from django.db import connection
from django.utils import timezone as dj_timezone

from accounts.models import Merchant
from billing import razorpay, services, tasks
from billing.exceptions import BillingProviderRejected, BillingProviderUnavailable
from billing.tests.snapshot_helpers import NOW, START, entity, invoice
from billing.tests.state_helpers import make_event, read_state
from billing.tests.webhook_helpers import ORIGINAL_FETCH_INVOICES, deliver, event
from core.tenancy import get_current_merchant_id, tenant_context

pytestmark = pytest.mark.django_db

NEW_START = NOW - timedelta(days=1)  # a provider period that differs from the stored one
BILLING_ENTRY = {
    "task": "billing.tasks.run_billing_maintenance",
    "schedule": 900.0,
    "options": {"queue": "default"},
}
RETRY_ENTRY = {
    "task": "events.tasks.retry_failed_events",
    "schedule": 300.0,
    "options": {"queue": "events"},
}


@pytest.fixture
def w(lifecycle_setup):
    return lifecycle_setup


@pytest.fixture
def delays(monkeypatch):
    """.delay of both per-merchant tasks replaced by one ordered recorder of
    (task name, args, kwargs)."""
    calls = []

    def recorder(name):
        def _delay(*args, **kwargs):
            calls.append((name, args, kwargs))

        return _delay

    monkeypatch.setattr(tasks.maintain_subscription, "delay", recorder("maintain"))
    monkeypatch.setattr(tasks.advance_subscription_dunning, "delay", recorder("dunning"))
    return calls


def sweep(capture):
    with capture(execute=True):
        tasks.run_billing_maintenance()


def enqueued(calls, merchant):
    """The (task name, args) pairs recorded for one merchant, in order."""
    return [(name, args) for name, args, _ in calls if args == (merchant.merchant.id,)]


def past_due(w, owner, days_ago, **kwargs):
    kwargs.setdefault("past_due_at", dj_timezone.now() - timedelta(days=days_ago))
    return w.sub(owner, "PAST_DUE", **kwargs)


def provider_period(w, owner, *, status="active", plan=None, invoices=True):
    """Razorpay (fake) reports `owner`'s subscription `status` for a new
    period, with (or without) the paid invoice that qualifies it."""
    ref = w.ref(owner)
    w.provider.entities[ref] = entity(plan or w.small, status=status, start=NEW_START, ref=ref)
    w.provider.invoices = [invoice(start=NEW_START, subscription_id=ref)] if invoices else []


def comparable(state):
    """The state without provider_synced_at (a repeat sync re-stamps it)."""
    values = dict(vars(state))
    values.pop("provider_synced_at")
    return values


# === Beat and registry ============================================================


def test_the_billing_beat_entry_is_exact_and_the_retry_entry_is_unchanged():
    assert settings.CELERY_BEAT_SCHEDULE["billing-maintenance"] == BILLING_ENTRY
    assert type(settings.CELERY_BEAT_SCHEDULE["billing-maintenance"]["schedule"]) is float
    assert settings.CELERY_BEAT_SCHEDULE["retry-failed-events"] == RETRY_ENTRY


def test_the_tasks_are_registered_on_the_default_queue_and_the_beat_task_resolves():
    from config.celery import app

    app.loader.import_default_modules()
    names = (
        "billing.tasks.sync_subscription",
        "billing.tasks.maintain_subscription",
        "billing.tasks.advance_subscription_dunning",
        "billing.tasks.run_billing_maintenance",
    )
    for name in names:
        assert name in app.tasks
        assert app.tasks[name].queue == "default"
    assert settings.CELERY_BEAT_SCHEDULE["billing-maintenance"]["task"] in app.tasks


def test_no_task_has_retry_configuration_and_nothing_uses_eta_or_countdown():
    from config.celery import app

    app.loader.import_default_modules()
    for name in (
        "billing.tasks.maintain_subscription",
        "billing.tasks.advance_subscription_dunning",
        "billing.tasks.run_billing_maintenance",
    ):
        assert not getattr(app.tasks[name], "autoretry_for", ())
    source = inspect.getsource(tasks)
    for forbidden in ("eta=", "countdown=", ".retry(", "autoretry_for", "retry_backoff"):
        assert forbidden not in source


# === the fan-out =====================================================================


def test_a_due_merchant_gets_one_sync_task_then_one_dunning_task_with_only_its_id(
    w, delays, django_capture_on_commit_callbacks
):
    past_due(w, w.a, 1)  # due; B has no subscription at all
    sweep(django_capture_on_commit_callbacks)
    a_id = w.a.merchant.id
    assert delays == [("maintain", (a_id,), {}), ("dunning", (a_id,), {})]


def test_the_enqueue_happens_only_after_the_merchants_commit(w, delays, django_capture_on_commit_callbacks):
    past_due(w, w.a, 1)
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        tasks.run_billing_maintenance()
    assert delays == []  # nothing enqueued before the commit
    assert len(callbacks) == 1  # one callback for the one due merchant
    for callback in callbacks:
        callback()
    assert [name for name, _ in enqueued(delays, w.a)] == ["maintain", "dunning"]


DUE = {
    "unprocessed_event_at_least_2_minutes_old": lambda w: (
        w.sub(w.a, "ACTIVE"),
        make_event(w.a.merchant, w.ref(w.a), age=timedelta(minutes=3)),
    ),
    "incomplete_with_a_ref": lambda w: w.sub(w.a, "INCOMPLETE"),
    "lapsed_active_with_a_ref": lambda w: w.sub(
        w.a, "ACTIVE", current_period_end=dj_timezone.now() - timedelta(days=1)
    ),
    "past_due": lambda w: past_due(w, w.a, 1),
    "expired_while_the_provider_subscription_is_open": lambda w: w.sub(
        w.a, "EXPIRED", provider_status="active"
    ),
}
NOT_DUE = {
    "active_inside_its_period": lambda w: w.sub(w.a, "ACTIVE"),
    "lapsed_active_without_a_ref": lambda w: w.sub(
        w.a, "ACTIVE", ref=None, current_period_end=dj_timezone.now() - timedelta(days=1)
    ),
    "cancelled_and_terminal_at_the_provider": lambda w: w.sub(w.a, "CANCELLED", provider_status="cancelled"),
    "unprocessed_event_only_30_seconds_old": lambda w: (
        w.sub(w.a, "ACTIVE"),
        make_event(w.a.merchant, w.ref(w.a), age=timedelta(seconds=30)),
    ),
}


@pytest.mark.parametrize("build", list(DUE.values()), ids=list(DUE))
def test_each_maintenance_due_category_is_enqueued(w, delays, django_capture_on_commit_callbacks, build):
    build(w)
    sweep(django_capture_on_commit_callbacks)
    assert [name for name, _, _ in delays] == ["maintain", "dunning"]


@pytest.mark.parametrize("build", list(NOT_DUE.values()), ids=list(NOT_DUE))
def test_a_merchant_that_is_not_due_gets_nothing(w, delays, django_capture_on_commit_callbacks, build):
    build(w)
    sweep(django_capture_on_commit_callbacks)
    assert delays == []


def test_deleted_merchants_are_skipped_and_suspended_merchants_are_handled(
    w, delays, django_capture_on_commit_callbacks
):
    past_due(w, w.a, 1)
    past_due(w, w.b, 1)
    Merchant.objects.filter(pk=w.a.merchant.id).update(status=Merchant.Status.DELETED)
    Merchant.objects.filter(pk=w.b.merchant.id).update(status=Merchant.Status.SUSPENDED)
    sweep(django_capture_on_commit_callbacks)
    assert enqueued(delays, w.a) == []
    assert [name for name, _ in enqueued(delays, w.b)] == ["maintain", "dunning"]


def test_the_due_check_runs_inside_each_merchants_own_tenant_context(
    w, delays, monkeypatch, django_capture_on_commit_callbacks
):
    past_due(w, w.a, 1)
    past_due(w, w.b, 1)
    seen = []
    real = services.maintenance_due

    def spy(*args, **kwargs):
        seen.append(get_current_merchant_id())
        return real(*args, **kwargs)

    monkeypatch.setattr(services, "maintenance_due", spy)
    sweep(django_capture_on_commit_callbacks)
    assert sorted(seen, key=str) == sorted([w.a.merchant.id, w.b.merchant.id], key=str)
    assert get_current_merchant_id() is None


# === per-merchant failure isolation ===================================================


@pytest.fixture
def three(w, make_merchant):
    """A, B and C all due; B is the one the tests break."""
    c = make_merchant("C")
    for owner in (w.a, w.b, c):
        past_due(w, owner, 1)
    w.c = c
    return w


def assert_others_enqueued(delays, w):
    for owner in (w.a, w.c):
        assert [name for name, _ in enqueued(delays, owner)] == ["maintain", "dunning"]


def test_a_rollback_after_the_callback_was_registered_discards_the_enqueue(
    three, delays, monkeypatch, django_capture_on_commit_callbacks
):
    """The merchant's transaction fails after on_commit registered the
    callback: the callback must never run, and the others are unaffected."""
    w = three
    real_on_commit = tasks.transaction.on_commit

    def register_then_fail(callback, *args, **kwargs):
        real_on_commit(callback, *args, **kwargs)
        if get_current_merchant_id() == w.b.merchant.id:
            raise RuntimeError("the transaction fails after registration")

    monkeypatch.setattr(tasks.transaction, "on_commit", register_then_fail)
    sweep(django_capture_on_commit_callbacks)
    assert enqueued(delays, w.b) == []
    assert_others_enqueued(delays, w)
    assert get_current_merchant_id() is None


def test_a_due_check_error_for_one_merchant_does_not_stop_the_others(
    three, delays, monkeypatch, django_capture_on_commit_callbacks, caplog
):
    w = three
    calls = []
    real = services.maintenance_due

    def flaky(*args, **kwargs):
        calls.append(get_current_merchant_id())
        if get_current_merchant_id() == w.b.merchant.id:
            raise RuntimeError("PLANTED-SECRET-in-the-message")
        return real(*args, **kwargs)

    monkeypatch.setattr(services, "maintenance_due", flaky)
    with caplog.at_level(logging.DEBUG):
        sweep(django_capture_on_commit_callbacks)
    assert_others_enqueued(delays, w)
    assert enqueued(delays, w.b) == []
    assert calls.count(w.b.merchant.id) == 1  # not retried within the run
    assert str(w.b.merchant.id) in caplog.text and "RuntimeError" in caplog.text
    assert "PLANTED-SECRET" not in caplog.text
    assert get_current_merchant_id() is None


def test_a_real_database_error_in_one_merchants_block_does_not_stop_the_others(
    three, delays, monkeypatch, django_capture_on_commit_callbacks, caplog
):
    w = three
    real = services.maintenance_due

    def broken_sql(*args, **kwargs):
        if get_current_merchant_id() == w.b.merchant.id:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1/0")
        return real(*args, **kwargs)

    monkeypatch.setattr(services, "maintenance_due", broken_sql)
    with caplog.at_level(logging.DEBUG):
        sweep(django_capture_on_commit_callbacks)
    assert_others_enqueued(delays, w)
    assert enqueued(delays, w.b) == []
    assert str(w.b.merchant.id) in caplog.text
    assert "division by zero" not in caplog.text.lower()  # the class name only, never the message
    assert get_current_merchant_id() is None


@pytest.mark.parametrize("failing", ["maintain", "dunning"])
def test_a_failed_enqueue_never_skips_the_merchants_other_task_or_other_merchants(
    three, monkeypatch, django_capture_on_commit_callbacks, caplog, failing
):
    w = three
    calls = []

    def recorder(name):
        def _delay(*args, **kwargs):
            calls.append((name, args))
            if name == failing and args == (w.b.merchant.id,):
                raise ConnectionError("redis://user:PLANTED@host:6379")

        return _delay

    monkeypatch.setattr(tasks.maintain_subscription, "delay", recorder("maintain"))
    monkeypatch.setattr(tasks.advance_subscription_dunning, "delay", recorder("dunning"))
    with caplog.at_level(logging.DEBUG):
        sweep(django_capture_on_commit_callbacks)
    for owner in (w.a, w.b, w.c):
        mine = [name for name, args in calls if args == (owner.merchant.id,)]
        assert mine == ["maintain", "dunning"]  # each attempted exactly once, in order
    assert "ConnectionError" in caplog.text and "PLANTED" not in caplog.text
    assert get_current_merchant_id() is None


# === dunning does not depend on the sync ==============================================


def expired_audit(state):
    return state.audits == ["billing.subscription_expired"]


def test_expiry_happens_when_the_sync_hits_the_unimplemented_invoice_fetch(w, monkeypatch):
    """D1: the real fetch_invoices raises NotImplementedError, caught nowhere."""
    past_due(w, w.a, 8)
    provider_period(w, w.a)
    monkeypatch.setattr(razorpay, "fetch_invoices", ORIGINAL_FETCH_INVOICES)
    with pytest.raises(NotImplementedError):
        tasks.maintain_subscription(w.a.merchant.id)
    assert read_state(w.a.merchant).status == "PAST_DUE"
    tasks.advance_subscription_dunning(w.a.merchant.id)
    state = read_state(w.a.merchant)
    assert state.status == "EXPIRED" and expired_audit(state)


def test_expiry_happens_when_the_sync_hits_a_provider_error(w):
    past_due(w, w.a, 8)
    w.provider.fetch_error = BillingProviderUnavailable()
    assert tasks.maintain_subscription(w.a.merchant.id) is None  # swallowed, retried next tick
    tasks.advance_subscription_dunning(w.a.merchant.id)
    assert read_state(w.a.merchant).status == "EXPIRED"


def test_expiry_happens_when_the_sync_raises_anything_else(w, monkeypatch):
    past_due(w, w.a, 8)

    def boom():
        raise RuntimeError("sync bug")

    monkeypatch.setattr(services, "sync_subscription", boom)
    with pytest.raises(RuntimeError):
        tasks.maintain_subscription(w.a.merchant.id)
    tasks.advance_subscription_dunning(w.a.merchant.id)
    assert read_state(w.a.merchant).status == "EXPIRED"


def test_expiry_happens_when_the_sync_task_never_runs_and_makes_no_fetch(w):
    past_due(w, w.a, 8)
    tasks.advance_subscription_dunning(w.a.merchant.id)
    state = read_state(w.a.merchant)
    assert state.status == "EXPIRED" and expired_audit(state)
    assert "fetch_subscription" not in w.provider.names() and "fetch_invoices" not in w.provider.names()


@pytest.mark.parametrize("days_ago, stage", [(4, 3), (6.5, 6)])
def test_the_day_3_and_day_6_checkpoints_advance_without_any_sync(w, days_ago, stage):
    past_due(w, w.a, days_ago)
    tasks.advance_subscription_dunning(w.a.merchant.id)
    state = read_state(w.a.merchant)
    assert state.status == "PAST_DUE" and state.dunning_stage == stage
    assert state.audits == ["billing.dunning_checkpoint"]
    assert w.provider.names() == []


def test_neither_task_calls_the_others_service(w, monkeypatch):
    past_due(w, w.a, 8)
    w.provider.fetch_error = BillingProviderUnavailable()

    def forbidden(*args, **kwargs):
        raise AssertionError("called by the wrong task")

    monkeypatch.setattr(services, "advance_dunning", forbidden)
    tasks.maintain_subscription(w.a.merchant.id)  # must not reach advance_dunning
    monkeypatch.undo()

    monkeypatch.setattr(services, "sync_subscription", forbidden)
    monkeypatch.setattr(services, "sync_pending_events", forbidden)
    tasks.advance_subscription_dunning(w.a.merchant.id)  # must not reach either sync
    assert read_state(w.a.merchant).status == "EXPIRED"


def test_the_maintenance_sync_bypasses_the_pending_event_skip(w, monkeypatch):
    """No unprocessed event at all: the webhook task would skip the fetch, the
    sweep's task must fetch, and must not go through the gate."""

    def forbidden(*args, **kwargs):
        raise AssertionError("the sweep must not use the pending-event skip")

    monkeypatch.setattr(services, "sync_pending_events", forbidden)
    w.sub(w.a, "ACTIVE")
    w.provider.entities[w.ref(w.a)] = entity(w.small, start=START, ref=w.ref(w.a))
    tasks.maintain_subscription(w.a.merchant.id)
    assert w.provider.count("fetch_subscription") == 1


# === tenant isolation and idempotency ================================================


def test_given_a_merchants_id_neither_task_touches_another_merchants_rows(w):
    past_due(w, w.a, 8)
    past_due(w, w.b, 8)
    make_event(w.b.merchant, w.ref(w.b), "evt_b")
    w.provider.add(w.ref(w.a), id=w.ref(w.a), status="halted", plan_id="plan_small")
    before_b = read_state(w.b.merchant)
    tasks.maintain_subscription(w.a.merchant.id)
    tasks.advance_subscription_dunning(w.a.merchant.id)
    assert read_state(w.a.merchant).status == "EXPIRED"  # the tasks did real work for A
    assert read_state(w.b.merchant) == before_b
    assert {call[1] for call in w.provider.calls if len(call) > 1} <= {w.ref(w.a)}


def test_the_sync_task_twice_leaves_the_same_state_and_no_extra_rows(w):
    w.sub(w.a, "INCOMPLETE")
    make_event(w.a.merchant, w.ref(w.a))
    provider_period(w, w.a)
    tasks.maintain_subscription(w.a.merchant.id)
    first = read_state(w.a.merchant)
    assert first.status == "ACTIVE" and len(first.payments) == 1 and len(first.usages) == 1
    tasks.maintain_subscription(w.a.merchant.id)
    assert comparable(read_state(w.a.merchant)) == comparable(first)


def test_the_dunning_task_twice_leaves_the_same_state_and_one_audit_row(w):
    past_due(w, w.a, 8)
    tasks.advance_subscription_dunning(w.a.merchant.id)
    first = read_state(w.a.merchant)
    tasks.advance_subscription_dunning(w.a.merchant.id)
    assert read_state(w.a.merchant) == first
    assert first.audits == ["billing.subscription_expired"]


# === events ===========================================================================


def active_in_period_with_an_old_event(w):
    """Due only because of the event: ACTIVE, inside its period, with an
    unprocessed event older than 2 minutes."""
    w.sub(w.a, "ACTIVE")
    make_event(w.a.merchant, w.ref(w.a), "evt_old")


def still_due(w):
    with tenant_context(w.a.merchant.id):
        return services.maintenance_due()


def test_a_failed_maintenance_sync_leaves_its_events_unprocessed_and_the_merchant_due(w):
    active_in_period_with_an_old_event(w)
    w.provider.fetch_error = BillingProviderUnavailable()
    tasks.maintain_subscription(w.a.merchant.id)
    assert read_state(w.a.merchant).events == {"evt_old": False}
    assert still_due(w)


def test_an_unsettled_maintenance_sync_leaves_its_events_unprocessed_and_the_merchant_due(w):
    active_in_period_with_an_old_event(w)
    provider_period(w, w.a, invoices=False)  # a new period with no qualifying invoice
    tasks.maintain_subscription(w.a.merchant.id)
    state = read_state(w.a.merchant)
    assert state.events == {"evt_old": False} and state.status == "ACTIVE" and state.payments == []
    assert still_due(w)


def test_a_d1_blocked_maintenance_sync_leaves_its_events_unprocessed_and_the_merchant_due(w, monkeypatch):
    active_in_period_with_an_old_event(w)
    provider_period(w, w.a)
    monkeypatch.setattr(razorpay, "fetch_invoices", ORIGINAL_FETCH_INVOICES)
    with pytest.raises(NotImplementedError):
        tasks.maintain_subscription(w.a.merchant.id)
    assert read_state(w.a.merchant).events == {"evt_old": False}
    assert still_due(w)


def test_the_dunning_task_never_changes_processed_at(w):
    past_due(w, w.a, 8)
    make_event(w.a.merchant, w.ref(w.a), "evt_old")
    tasks.advance_subscription_dunning(w.a.merchant.id)
    assert read_state(w.a.merchant).events == {"evt_old": False}


# === recovery of webhook events =======================================================


def backdate_events(merchant, age=timedelta(minutes=3)):
    from billing.models import BillingEvent
    from core.tenancy import tenant_atomic

    with tenant_context(merchant.id), tenant_atomic():
        BillingEvent.objects.update(created_at=dj_timezone.now() - age)


@pytest.mark.usefixtures("webhook_secret")
def test_an_event_whose_enqueue_was_lost_is_recovered_by_the_sweep(
    webhook_setup, monkeypatch, delays, django_capture_on_commit_callbacks
):
    a = webhook_setup.a

    def lost(merchant_id):
        raise ConnectionError("broker down")

    monkeypatch.setattr(tasks.sync_subscription, "delay", lost)
    with django_capture_on_commit_callbacks(execute=True):
        assert deliver(event(), event_id="evt_lost").status_code == 200
    assert read_state(a.merchant).events == {"evt_lost": False}
    backdate_events(a.merchant)

    sweep(django_capture_on_commit_callbacks)
    assert [name for name, _ in enqueued(delays, a)] == ["maintain", "dunning"]
    tasks.maintain_subscription(a.merchant.id)  # what the worker does with that message
    state = read_state(a.merchant)
    assert state.events == {"evt_lost": True} and state.status == "ACTIVE"


@pytest.mark.usefixtures("webhook_secret")
def test_an_event_left_unprocessed_by_the_clock_skew_margin_is_recovered_by_the_sweep(
    webhook_setup, real_clock_skew, queued, delays, django_capture_on_commit_callbacks
):
    a = webhook_setup.a
    with django_capture_on_commit_callbacks(execute=True):
        assert deliver(event(), event_id="evt_margin").status_code == 200
    tasks.sync_subscription(a.merchant.id)  # the webhook's own task, moments later
    state = read_state(a.merchant)
    assert state.status == "ACTIVE"  # the sync settled...
    assert state.events == {"evt_margin": False}  # ...but the event is inside the margin

    backdate_events(a.merchant)  # it has now aged 2+ minutes
    sweep(django_capture_on_commit_callbacks)
    assert [name for name, _ in enqueued(delays, a)] == ["maintain", "dunning"]
    tasks.maintain_subscription(a.merchant.id)
    assert read_state(a.merchant).events == {"evt_margin": True}


# === D1 =================================================================================


def test_with_the_real_invoice_fetch_the_maintenance_sync_raises_and_grants_nothing(
    w, monkeypatch, delays, django_capture_on_commit_callbacks
):
    w.sub(w.a, "INCOMPLETE")
    make_event(w.a.merchant, w.ref(w.a), "evt_d1")
    provider_period(w, w.a)
    monkeypatch.setattr(razorpay, "fetch_invoices", ORIGINAL_FETCH_INVOICES)
    before = read_state(w.a.merchant)
    with pytest.raises(NotImplementedError):
        tasks.maintain_subscription(w.a.merchant.id)
    after = read_state(w.a.merchant)
    assert (after.status, after.audits, after.payments, after.usages, after.events) == (
        "INCOMPLETE",
        [],
        [],
        [],
        {"evt_d1": False},
    )
    assert after.period == (None, None) and after.period == before.period
    sweep(django_capture_on_commit_callbacks)  # still due: enqueued again on the next tick
    assert [name for name, _ in enqueued(delays, w.a)] == ["maintain", "dunning"]


# === logging ============================================================================


def test_no_task_logs_a_provider_body_reference_event_payment_or_invoice_id(
    w, monkeypatch, django_capture_on_commit_callbacks, caplog
):
    w.sub(w.a, "ACTIVE")
    ref = w.ref(w.a)
    make_event(w.a.merchant, ref, "evt_secret_77")
    w.provider.fetch_error = BillingProviderRejected("PLANTED_CODE")  # a provider error code

    def leaky_delay(merchant_id):
        raise ConnectionError(f"{ref} pay_1 inv_1 evt_secret_77")

    monkeypatch.setattr(tasks.maintain_subscription, "delay", leaky_delay)
    monkeypatch.setattr(tasks.advance_subscription_dunning, "delay", leaky_delay)
    with caplog.at_level(logging.DEBUG):
        sweep(django_capture_on_commit_callbacks)  # both enqueues fail
        tasks.maintain_subscription(w.a.merchant.id)  # the provider error is swallowed and logged
        tasks.advance_subscription_dunning(w.a.merchant.id)
    assert "ConnectionError" in caplog.text and "BillingProviderRejected" in caplog.text
    for secret in (ref, "evt_secret_77", "pay_1", "inv_1", "PLANTED_CODE"):
        assert secret not in caplog.text
