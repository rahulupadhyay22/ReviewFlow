"""W7 gate, gap tests (spec 07 "Celery tasks", "W7 failure handling", "Expiry
versus sync: race rules", Definition of done "Tasks" and the lifecycle,
dunning and quota groups). Adds what test_tasks.py / test_expiry_race.py do
not already assert: the fan-out run twice (overlap), the sweep and the
dunning task never touching processed_at, the real 5-second clock-skew margin
end to end, SUSPENDED and subscription-less merchants, quota and UsageRecord
across an expiry and a reactivation, dunning audit counts over repeated runs,
and exception/log hygiene.

Razorpay is the FakeProvider (conftest). `.delay` of the per-merchant tasks is
recorded, then the recorded messages are executed in order by run_recorded(),
which is what a worker would do. Time is moved by shifting stored timestamps,
never by patching the global clock."""
import logging
from datetime import timedelta

import pytest
from django.utils import timezone as dj_timezone

from accounts.models import Merchant
from auditlog.models import AuditLog
from billing import razorpay, services, tasks
from billing.exceptions import BillingProviderRejected
from billing.models import BillingEvent, PaymentAttempt, Subscription, UsageRecord
from billing.services import ReservationResult as R
from billing.tests.snapshot_helpers import NOW, START, END, entity, invoice
from billing.tests.state_helpers import make_event, read_state
from billing.tests.webhook_helpers import ORIGINAL_FETCH_INVOICES, deliver, event
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

NEW_START = NOW - timedelta(days=1)
NEW_END = NEW_START + timedelta(days=30)
EXPIRED = "billing.subscription_expired"
ACTIVATED = "billing.subscription_activated"
CHECKPOINT = "billing.dunning_checkpoint"


@pytest.fixture
def w(lifecycle_setup):
    return lifecycle_setup


@pytest.fixture
def capture(django_capture_on_commit_callbacks):
    return django_capture_on_commit_callbacks


@pytest.fixture
def delays(monkeypatch):
    """Ordered recorder of (task name, args, kwargs) for both per-merchant tasks."""
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


def run_recorded(calls):
    """Execute every recorded message, in order, as a worker would."""
    runners = {"maintain": tasks.maintain_subscription, "dunning": tasks.advance_subscription_dunning}
    for name, args, kwargs in list(calls):
        runners[name](*args, **kwargs)


def pair(merchant):
    a = merchant.merchant.id
    return [("maintain", (a,), {}), ("dunning", (a,), {})]


def past_due(w, owner, days_ago, **kwargs):
    kwargs.setdefault("past_due_at", dj_timezone.now() - timedelta(days=days_ago))
    return w.sub(owner, "PAST_DUE", **kwargs)


def provider_period(w, owner, *, status="active", start=NEW_START, plan=None, invoices=True):
    ref = w.ref(owner)
    w.provider.entities[ref] = entity(plan or w.small, status=status, start=start, ref=ref)
    w.provider.invoices = [invoice(start=start, subscription_id=ref)] if invoices else []


def update_sub(owner, **fields):
    with tenant_context(owner.merchant.id), tenant_atomic():
        Subscription.objects.update(**fields)


def set_used(owner, n):
    with tenant_context(owner.merchant.id), tenant_atomic():
        UsageRecord.objects.update(requests_used=n)


def reserve(owner):
    with tenant_context(owner.merchant.id), tenant_atomic():
        return services.reserve_quota_unit()


def can_send(owner):
    with tenant_context(owner.merchant.id):
        return services.get_entitlement().can_send


def counts(owner):
    """Everything a billing task can write, readable for a merchant with no subscription."""
    with tenant_context(owner.merchant.id), tenant_atomic():
        return {
            "subs": Subscription.objects.count(),
            "usage": UsageRecord.objects.count(),
            "payments": PaymentAttempt.objects.count(),
            "audits": AuditLog.objects.filter(action__startswith="billing.").count(),
            "events": {e.provider_event_id: e.processed_at is not None for e in BillingEvent.objects.all()},
        }


def backdate(owner, event_id, age=timedelta(minutes=3)):
    with tenant_context(owner.merchant.id), tenant_atomic():
        BillingEvent.objects.filter(provider_event_id=event_id).update(created_at=dj_timezone.now() - age)


def audit_metadata(owner, action):
    with tenant_context(owner.merchant.id), tenant_atomic():
        return [a.metadata_json for a in AuditLog.objects.filter(action=action).order_by("created_at", "id")]


def due(owner):
    with tenant_context(owner.merchant.id):
        return services.maintenance_due()


# === fan-out: overlap and idempotency =====================================================


def test_two_sweeps_enqueue_one_pair_each_and_the_sweep_itself_writes_nothing(w, delays, capture):
    past_due(w, w.a, 1)
    make_event(w.a.merchant, w.ref(w.a), "evt_1")
    before = read_state(w.a.merchant)
    sweep(capture)
    sweep(capture)
    assert delays == pair(w.a) * 2  # one pair per tick, no dedupe, no extras, B (no row) none
    assert read_state(w.a.merchant) == before  # no audit row, nothing processed, no state change
    assert w.provider.calls == []


def test_overlapping_ticks_executed_twice_leave_the_state_of_one_run_and_no_extra_rows(w, delays, capture):
    """Two ticks enqueued the same merchant before either ran: all four messages
    run and the outcome is that of one."""
    w.sub(w.a, "INCOMPLETE")
    make_event(w.a.merchant, w.ref(w.a), "evt_1")
    provider_period(w, w.a)
    sweep(capture)
    sweep(capture)
    assert delays == pair(w.a) * 2
    run_recorded(delays)
    state = read_state(w.a.merchant)
    assert state.status == "ACTIVE" and state.period == (NEW_START, NEW_END)
    assert state.audits == [ACTIVATED]
    assert len(state.payments) == 1
    assert state.usages == [(NEW_START, NEW_END, 0)]
    assert state.events == {"evt_1": True}


def test_a_merchant_due_for_several_reasons_still_gets_exactly_one_pair(w, delays, capture):
    past_due(w, w.a, 1)
    make_event(w.a.merchant, w.ref(w.a), "evt_1")
    update_sub(w.a, current_period_end=dj_timezone.now() - timedelta(days=1))
    sweep(capture)
    assert delays == pair(w.a)


def test_a_due_merchant_is_swept_every_tick_but_never_costs_more_than_one_pair_per_tick(
    w, monkeypatch, delays, capture
):
    """D1 open: the maintenance sync raises every tick. Three ticks, six
    messages, no retry inside a tick."""
    w.sub(w.a, "INCOMPLETE")
    provider_period(w, w.a)
    monkeypatch.setattr(razorpay, "fetch_invoices", ORIGINAL_FETCH_INVOICES)
    for _ in range(3):
        delays.clear()
        sweep(capture)
        assert delays == pair(w.a)
        with pytest.raises(NotImplementedError):
            tasks.maintain_subscription(w.a.merchant.id)
        tasks.advance_subscription_dunning(w.a.merchant.id)
    assert w.provider.count("fetch_invoices") == 0 and w.provider.count("fetch_subscription") == 3


def test_a_full_sweep_touches_only_due_merchants_and_only_their_provider_reference(
    w, make_merchant, delays, capture
):
    c = make_merchant("C")  # no subscription at all
    past_due(w, w.a, 8)
    w.sub(w.b, "ACTIVE")  # settled, inside its period: not due
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="halted", start=START, ref=w.ref(w.a))
    before_b, before_c = read_state(w.b.merchant), counts(c)
    sweep(capture)
    assert delays == pair(w.a)
    run_recorded(delays)
    assert read_state(w.a.merchant).status == "EXPIRED"
    assert read_state(w.b.merchant) == before_b
    assert counts(c) == before_c
    assert {call[1] for call in w.provider.calls} == {w.ref(w.a)}


# === processed_at moves only through a settled sync ========================================


def test_the_sweep_alone_never_marks_an_event_processed(w, delays, capture):
    w.sub(w.a, "ACTIVE")
    for n in range(3):
        make_event(w.a.merchant, w.ref(w.a), f"evt_{n}")
    sweep(capture)
    assert delays == pair(w.a)  # the sweep did run and did find the merchant due
    assert read_state(w.a.merchant).events == {"evt_0": False, "evt_1": False, "evt_2": False}


def test_a_day_4_checkpoint_run_never_marks_an_event_processed(w):
    past_due(w, w.a, 4)
    make_event(w.a.merchant, w.ref(w.a), "evt_old")
    tasks.advance_subscription_dunning(w.a.merchant.id)
    state = read_state(w.a.merchant)
    assert state.dunning_stage == 3 and state.events == {"evt_old": False}


def test_the_owed_provider_cancel_path_never_marks_an_event_processed(w):
    w.sub(w.a, "EXPIRED", provider_status="active")
    make_event(w.a.merchant, w.ref(w.a), "evt_old")
    tasks.advance_subscription_dunning(w.a.merchant.id)
    state = read_state(w.a.merchant)
    assert w.provider.calls == [("cancel_subscription", w.ref(w.a), False)]
    assert state.provider_status == "cancelled" and state.status == "EXPIRED"
    assert state.events == {"evt_old": False}


@pytest.mark.parametrize("case", ["stale_snapshot", "unknown_plan"])
def test_a_maintenance_sync_that_drops_its_snapshot_leaves_events_unprocessed_and_the_merchant_due(w, case):
    w.sub(w.a, "ACTIVE")
    make_event(w.a.merchant, w.ref(w.a), "evt_old")
    ref = w.ref(w.a)
    w.provider.entities[ref] = entity(
        w.small, start=START, ref=ref, plan_id="plan_unknown" if case == "unknown_plan" else "__plan__"
    )
    if case == "stale_snapshot":
        update_sub(w.a, provider_synced_at=dj_timezone.now() + timedelta(hours=1))
    before = read_state(w.a.merchant)
    tasks.maintain_subscription(w.a.merchant.id)
    after = read_state(w.a.merchant)
    assert after.events == {"evt_old": False} and after.audits == []
    assert (after.status, after.period, after.provider_synced_at) == (
        before.status,
        before.period,
        before.provider_synced_at,
    )
    assert due(w.a)


def test_a_local_expiry_does_not_stamp_provider_synced_at(w):
    past_due(w, w.a, 8)
    before = read_state(w.a.merchant)
    tasks.advance_subscription_dunning(w.a.merchant.id)
    after = read_state(w.a.merchant)
    assert after.status == "EXPIRED" and after.provider_synced_at == before.provider_synced_at is None


# === the real 5-second clock-skew margin, end to end ========================================


def test_the_production_margin_is_five_seconds(real_clock_skew):
    assert real_clock_skew == timedelta(seconds=5)


def test_with_the_real_margin_the_sweep_marks_old_events_leaves_a_fresh_one_and_does_not_spin(
    w, real_clock_skew, delays, capture
):
    w.sub(w.a, "ACTIVE")
    ref = w.ref(w.a)
    make_event(w.a.merchant, ref, "evt_old", age=timedelta(minutes=3))
    make_event(w.a.merchant, ref, "evt_fresh", age=timedelta(seconds=1))
    w.provider.entities[ref] = entity(w.small, start=START, ref=ref)

    sweep(capture)
    assert delays == pair(w.a)
    run_recorded(delays)
    assert read_state(w.a.merchant).events == {"evt_old": True, "evt_fresh": False}  # fresh is inside the margin

    # Only a sub-2-minute event remains: not due, so no further enqueue (no tight loop).
    assert not due(w.a)
    delays.clear()
    sweep(capture)
    assert delays == []

    backdate(w.a, "evt_fresh")
    assert due(w.a)
    sweep(capture)
    assert delays == pair(w.a)
    run_recorded(delays)
    assert read_state(w.a.merchant).events == {"evt_old": True, "evt_fresh": True}
    assert not due(w.a)


@pytest.mark.usefixtures("webhook_secret")
def test_a_lost_enqueue_is_recovered_by_the_sweep_with_the_real_margin(
    webhook_setup, real_clock_skew, monkeypatch, delays, capture
):
    a = webhook_setup.a

    def lost(merchant_id):
        raise ConnectionError("broker down")

    monkeypatch.setattr(tasks.sync_subscription, "delay", lost)
    with capture(execute=True):
        assert deliver(event(), event_id="evt_lost").status_code == 200
    assert read_state(a.merchant).events == {"evt_lost": False}

    backdate(a, "evt_lost")
    sweep(capture)
    assert delays == pair(a)  # B is settled and inside its period: not enqueued
    run_recorded(delays)
    state = read_state(a.merchant)
    assert state.events == {"evt_lost": True} and state.status == "ACTIVE"


def test_a_suspended_merchant_is_swept_and_expired_end_to_end(w, delays, capture):
    past_due(w, w.a, 8)
    Merchant.objects.filter(pk=w.a.merchant.id).update(status=Merchant.Status.SUSPENDED)
    ref = w.ref(w.a)
    w.provider.entities[ref] = entity(w.small, status="halted", start=START, ref=ref)
    sweep(capture)
    assert delays == pair(w.a)
    run_recorded(delays)
    state = read_state(w.a.merchant)
    assert state.status == "EXPIRED" and state.audits == [EXPIRED]
    assert state.past_due_at is None and state.dunning_stage is None
    assert ("cancel_subscription", ref, False) in w.provider.calls


# === a merchant with no subscription ========================================================


def test_a_merchant_with_no_subscription_and_no_events_is_not_enqueued(w, delays, capture):
    sweep(capture)
    assert delays == []


def test_every_task_is_a_no_op_for_a_merchant_with_no_subscription(w):
    before = counts(w.a)
    for task in (tasks.sync_subscription, tasks.maintain_subscription, tasks.advance_subscription_dunning):
        assert task(w.a.merchant.id) is None
    assert counts(w.a) == before == {"subs": 0, "usage": 0, "payments": 0, "audits": 0, "events": {}}
    assert w.provider.calls == []


def test_an_old_event_with_no_subscription_is_swept_each_tick_and_never_marked(w, delays, capture):
    make_event(w.a.merchant, "sub_ghost", "evt_ghost")
    sweep(capture)
    assert delays == pair(w.a)
    run_recorded(delays)
    sweep(capture)
    assert delays == pair(w.a) * 2
    run_recorded(delays[2:])
    assert counts(w.a)["events"] == {"evt_ghost": False}
    assert counts(w.a)["audits"] == 0 and w.provider.calls == []


# === quota and UsageRecord across expiry and reactivation ===================================


def test_expiry_keeps_quota_closed_and_a_late_paid_period_opens_a_fresh_usage_record(w):
    past_due(w, w.a, 8)
    set_used(w.a, 7)
    assert reserve(w.a) is R.NOT_ENTITLED  # PAST_DUE

    tasks.advance_subscription_dunning(w.a.merchant.id)
    assert read_state(w.a.merchant).status == "EXPIRED"
    assert reserve(w.a) is R.NOT_ENTITLED
    assert not can_send(w.a)
    assert read_state(w.a.merchant).usages == [(START, END, 7)]

    provider_period(w, w.a)  # the late charge: active + a paid invoice for the new period
    tasks.maintain_subscription(w.a.merchant.id)
    state = read_state(w.a.merchant)
    assert state.status == "ACTIVE" and state.audits == [EXPIRED, ACTIVATED]
    assert state.usages == [(START, END, 7), (NEW_START, NEW_END, 0)]  # no rollover, old untouched
    assert can_send(w.a)

    assert reserve(w.a) is R.RESERVED
    assert read_state(w.a.merchant).usages == [(START, END, 7), (NEW_START, NEW_END, 1)]


def test_reactivation_inside_the_stored_period_keeps_that_periods_usage(w):
    """Spec "Reactivation inside the stored period does not reset quota"
    (clarified 2026-10-03). It follows from three rules together:
    reset_usage_period() is insert-or-ignore under UNIQUE(merchant,
    period_start, period_end), so the paid period that is already stored
    keeps its one UsageRecord; no rollover leaves that record as it is; and
    only a new, qualifying billing period opens a fresh record at 0 (see the
    previous test). Same-period reactivation is not a new period."""
    w.sub(w.a, "EXPIRED", provider_status="halted")
    set_used(w.a, 7)
    provider_period(w, w.a, start=START)  # paid invoice for the period already stored
    tasks.maintain_subscription(w.a.merchant.id)
    state = read_state(w.a.merchant)
    assert state.status == "ACTIVE" and state.usages == [(START, END, 7)]
    assert reserve(w.a) is R.RESERVED
    assert read_state(w.a.merchant).usages == [(START, END, 8)]


def test_a_non_qualifying_snapshot_never_reopens_quota_for_an_expired_row(w):
    past_due(w, w.a, 8)
    set_used(w.a, 7)
    tasks.advance_subscription_dunning(w.a.merchant.id)
    provider_period(w, w.a, invoices=False)  # active, new period, no paid invoice
    tasks.maintain_subscription(w.a.merchant.id)
    state = read_state(w.a.merchant)
    assert state.status == "EXPIRED" and state.payments == []
    assert state.usages == [(START, END, 7)]
    assert reserve(w.a) is R.NOT_ENTITLED and not can_send(w.a)


def test_a_provider_halt_found_by_the_sweep_closes_quota_without_touching_usage(w):
    w.sub(w.a, "ACTIVE")
    set_used(w.a, 7)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="halted", start=START, ref=w.ref(w.a))
    tasks.maintain_subscription(w.a.merchant.id)
    state = read_state(w.a.merchant)
    assert state.status == "PAST_DUE" and state.audits == ["billing.subscription_past_due"]
    assert reserve(w.a) is R.NOT_ENTITLED and not can_send(w.a)
    assert state.usages == [(START, END, 7)]


# === dunning checkpoints: audit counts over repeated task runs ==============================


def test_repeated_dunning_runs_write_one_audit_row_per_checkpoint_and_one_for_expiry(w):
    owner = w.a
    past_due(w, owner, 4)
    for _ in range(2):
        tasks.advance_subscription_dunning(owner.merchant.id)
    state = read_state(owner.merchant)
    assert (state.dunning_stage, state.audits) == (3, [CHECKPOINT])

    update_sub(owner, past_due_at=dj_timezone.now() - timedelta(days=6, hours=12))
    for _ in range(2):
        tasks.advance_subscription_dunning(owner.merchant.id)
    state = read_state(owner.merchant)
    assert (state.dunning_stage, state.audits) == (6, [CHECKPOINT, CHECKPOINT])
    assert [m["stage"] for m in audit_metadata(owner, CHECKPOINT)] == [3, 6]

    update_sub(owner, past_due_at=dj_timezone.now() - timedelta(days=8))
    for _ in range(2):
        tasks.advance_subscription_dunning(owner.merchant.id)
    state = read_state(owner.merchant)
    assert state.status == "EXPIRED"
    assert (state.past_due_at, state.dunning_stage) == (None, None)
    assert state.audits == [CHECKPOINT, CHECKPOINT, EXPIRED]
    assert state.payments == []
    # Local only: no fetch, one provider cancel for the whole episode.
    assert w.provider.count("fetch_subscription") == 0 and w.provider.count("fetch_invoices") == 0
    assert w.provider.count("cancel_subscription") == 1
    assert state.provider_synced_at is None


def test_the_dunning_audit_rows_carry_no_provider_identifier(w):
    past_due(w, w.a, 8)
    tasks.advance_subscription_dunning(w.a.merchant.id)
    for metadata in audit_metadata(w.a, EXPIRED):
        text = str(metadata)
        assert w.ref(w.a) not in text and "pay_" not in text and "inv_" not in text and "http" not in text


# === exception and log hygiene ===============================================================


def test_the_d1_exception_carries_no_reference_event_or_payment_data(w, monkeypatch):
    w.sub(w.a, "INCOMPLETE")
    make_event(w.a.merchant, w.ref(w.a), "evt_secret_77")
    provider_period(w, w.a)
    monkeypatch.setattr(razorpay, "fetch_invoices", ORIGINAL_FETCH_INVOICES)
    with pytest.raises(NotImplementedError) as exc:
        tasks.maintain_subscription(w.a.merchant.id)
    text = f"{exc.value!s} {exc.value!r}"
    for secret in (w.ref(w.a), "evt_secret_77", "pay_", "inv_"):
        assert secret not in text


def test_a_refused_owed_cancel_is_logged_by_class_only_and_retried_on_the_next_run(w, caplog):
    w.sub(w.a, "EXPIRED", provider_status="active")
    ref = w.ref(w.a)
    w.provider.cancel_error = BillingProviderRejected("PLANTED_CODE")
    with caplog.at_level(logging.DEBUG):
        tasks.advance_subscription_dunning(w.a.merchant.id)
    assert "BillingProviderRejected" in caplog.text
    for secret in (ref, "PLANTED_CODE"):
        assert secret not in caplog.text
    state = read_state(w.a.merchant)
    assert state.status == "EXPIRED" and state.provider_status == "active"  # still owed
    assert due(w.a)

    w.provider.cancel_error = None
    tasks.advance_subscription_dunning(w.a.merchant.id)
    state = read_state(w.a.merchant)
    assert w.provider.count("cancel_subscription") == 2
    assert state.provider_status == "cancelled" and state.audits == []
    assert not due(w.a)
