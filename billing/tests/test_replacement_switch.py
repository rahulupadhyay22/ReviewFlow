"""W4 of 07-plan-change-replacement: the switch (`switch_to_replacement`, reached
only through the replacement sync step) and the paid-proof gate in front of it.

The replacement rows are put into place by writing the columns directly
(replacement_helpers: checkout is W5); Razorpay is only the FakeProvider. Nothing
here claims any Razorpay behavior, and D1 is not worked around: the one test that
uses the real fetch_invoices proves it still stops everything."""
from datetime import datetime, timedelta, timezone

import pytest
from django.utils import timezone as dj_timezone

from auditlog.models import AuditLog
from billing import razorpay, services
from billing.exceptions import BillingProviderUnavailable
from billing.models import PaymentAttempt, Subscription
from billing.tests.replacement_helpers import (
    NEW_START,
    REPL_REF,
    RETIRED_REF,
    provide_replacement,
    set_replacement,
    set_retired,
)
from billing.tests.snapshot_helpers import START, entity, ts
from billing.tests.state_helpers import make_event, read_state
from billing.tests.webhook_helpers import ORIGINAL_FETCH_INVOICES
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

NEW_END = NEW_START + timedelta(days=30)


@pytest.fixture
def w(lifecycle_setup):
    return lifecycle_setup


def unix(value) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def sync(owner):
    with tenant_context(owner.merchant.id):
        services.sync_subscription()


def sync_replacement(owner):
    with tenant_context(owner.merchant.id):
        services._sync_replacement()


def sub_row(owner):
    with tenant_context(owner.merchant.id), tenant_atomic():
        return Subscription.objects.select_related("plan", "pending_plan").get()


def plan_changed_rows(owner):
    with tenant_context(owner.merchant.id), tenant_atomic():
        return [a.metadata_json for a in AuditLog.objects.filter(action="billing.plan_changed")]


def upgrade_setup(w, *, status="ACTIVE", **sub_kwargs):
    """A on the small plan with an upgrade replacement to the big plan, paid."""
    w.sub(w.a, status, plan=w.small, **sub_kwargs)
    set_replacement(w.a.merchant)
    provide_replacement(w.provider, w.big)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START)


# --- the switch -------------------------------------------------------------------------------


def test_a_paid_upgrade_replacement_switches_the_row(w):
    upgrade_setup(w, cancel_at_period_end=True)
    old_ref = w.ref(w.a)
    make_event(w.a.merchant, old_ref, "evt_old")
    make_event(w.a.merchant, REPL_REF, "evt_new")
    before = read_state(w.a.merchant)

    sync_replacement(w.a)

    row, state = sub_row(w.a), read_state(w.a.merchant)
    assert (row.plan_id, row.status) == (w.big.pk, "ACTIVE")
    assert state.period == (unix(ts(NEW_START)), unix(ts(NEW_END)))
    assert row.payment_provider_ref == REPL_REF
    assert (row.retired_provider_ref, row.retired_kind) == (old_ref, "SWITCHED_OLD")
    assert (
        row.replacement_provider_ref,
        row.replacement_expires_at,
        row.replacement_committed_at,
        row.replacement_cancel_confirmed_at,
        row.pending_plan_id,
        row.cancel_at_period_end,
    ) == (None, None, None, None, None, False)
    assert (row.provider_status, row.provider_synced_at is not None) == ("active", True)
    # a fresh UsageRecord at 0; the old period's record is untouched (no rollover)
    assert state.usages == before.usages + [(unix(ts(NEW_START)), unix(ts(NEW_END)), 0)]
    # one ledger entry for the replacement's payment, the existing activation rule
    assert state.payments == [("pay_1", PaymentAttempt.AttemptType.RENEWAL, "SUCCEEDED", unix(ts(NEW_START) + 3600))]
    assert state.audits == ["billing.plan_changed"]
    (meta,) = plan_changed_rows(w.a)
    assert meta == {
        "effective": "REPLACEMENT_IMMEDIATE",
        "from_plan": w.small.name,
        "to_plan": w.big.name,
        "plan_id": str(w.big.pk),
        "acknowledged_no_credit": True,
    }
    # the departed ref's events are processed (nothing can match them again); the
    # replacement's own event is settled by the same step
    assert state.events == {"evt_old": True, "evt_new": True}


def test_a_paid_downgrade_replacement_switches_and_is_scheduled(w):
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, committed=True, downgrade_to=w.small)
    provide_replacement(w.provider, w.small)
    sync_replacement(w.a)

    row = sub_row(w.a)
    assert (row.plan_id, row.pending_plan_id, row.replacement_committed_at) == (w.small.pk, None, None)
    (meta,) = plan_changed_rows(w.a)
    assert meta["effective"] == "REPLACEMENT_SCHEDULED"
    assert "acknowledged_no_credit" not in meta  # a downgrade never needs it
    assert row.retired_kind == "SWITCHED_OLD"


def test_the_switch_clears_a_committed_and_confirmed_marker(w):
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, committed=True, downgrade_to=w.small)
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(replacement_cancel_confirmed_at=dj_timezone.now())
    provide_replacement(w.provider, w.small)
    sync_replacement(w.a)
    row = sub_row(w.a)
    assert (row.replacement_committed_at, row.replacement_cancel_confirmed_at) == (None, None)


def test_a_second_run_changes_nothing(w):
    upgrade_setup(w)
    sync_replacement(w.a)
    once = read_state(w.a.merchant)
    sync_replacement(w.a)
    sync(w.a)
    twice = read_state(w.a.merchant)
    assert (twice.audits, twice.payments, twice.usages, twice.period) == (
        once.audits,
        once.payments,
        once.usages,
        once.period,
    )
    assert twice.retired_provider_ref == once.retired_provider_ref


def test_an_older_main_snapshot_fetched_before_the_switch_is_dropped(w):
    """The main step fetched the old ref, then the switch landed: the stale-ref
    guard drops it, so it can neither end nor re-activate the row."""
    upgrade_setup(w)
    old_ref = w.ref(w.a)
    stale_row = sub_row(w.a)
    sync_replacement(w.a)
    before = read_state(w.a.merchant)
    with tenant_context(w.a.merchant.id):
        result = services._apply_snapshot(
            stale_row, entity(w.small, status="cancelled", ref=old_ref), None, dj_timezone.now()
        )
    assert (result.changed, result.settled) == (False, False)
    assert read_state(w.a.merchant) == before


# --- the paid-proof gate: `active` alone grants nothing -------------------------------------


def test_active_without_a_qualifying_invoice_switches_nothing(w):
    upgrade_setup(w)
    w.provider.invoices_by_ref[REPL_REF] = []
    make_event(w.a.merchant, REPL_REF, "evt_new")
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    after = read_state(w.a.merchant)
    assert after == before  # no write at all, events unprocessed
    assert after.events == {"evt_new": False}
    assert after.replacement_provider_ref == REPL_REF


@pytest.mark.parametrize(
    "override",
    [
        {"status": "issued"},
        {"amount_due": 100},
        {"payment_id": ""},
        {"subscription_id": "sub_someone_else"},
        {"billing_start": ts(NEW_START) + 10 * 86400, "billing_end": ts(NEW_START) + 20 * 86400},
    ],
)
def test_every_non_qualifying_invoice_switches_nothing(w, override):
    upgrade_setup(w)
    provide_replacement(w.provider, w.big, **override)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


def test_the_main_subscriptions_invoice_is_not_proof_for_the_replacement(w):
    upgrade_setup(w)
    w.provider.invoices_by_ref[REPL_REF] = [
        {
            "id": "inv_x",
            "subscription_id": w.ref(w.a),  # the OLD subscription's invoice
            "status": "paid",
            "amount_due": 0,
            "payment_id": "pay_x",
            "billing_start": ts(NEW_START),
            "billing_end": ts(NEW_END),
            "paid_at": ts(NEW_START),
        }
    ]
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


def test_an_unknown_plan_is_never_guessed(w):
    upgrade_setup(w)
    w.provider.entities[REPL_REF]["plan_id"] = "plan_nobody_offers"
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


def test_a_replacement_in_the_same_period_as_the_row_does_not_switch(w):
    upgrade_setup(w)
    provide_replacement(w.provider, w.big, start=START)  # the old period's start
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


def test_an_occupied_retired_slot_blocks_the_switch_rather_than_losing_it(w):
    upgrade_setup(w)
    set_retired(w.a.merchant)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before
    assert read_state(w.a.merchant).retired_provider_ref == RETIRED_REF


def test_d1_with_the_real_fetch_invoices_nothing_switches_and_nothing_is_written(w, monkeypatch):
    """D1 stays fail-closed: the real fetch_invoices raises NotImplementedError,
    which is caught nowhere. The main subscription still gets its own sync."""
    monkeypatch.setattr(razorpay, "fetch_invoices", ORIGINAL_FETCH_INVOICES)
    upgrade_setup(w)
    before = read_state(w.a.merchant)
    with pytest.raises(NotImplementedError):
        sync(w.a)
    after = read_state(w.a.merchant)
    assert after.replacement_provider_ref == REPL_REF
    assert (after.status, after.period, after.payments, after.usages, after.audits) == (
        before.status,
        before.period,
        before.payments,
        before.usages,
        before.audits,
    )
    assert ("fetch_subscription", w.ref(w.a)) in w.provider.calls  # the main step ran anyway


# --- PAST_DUE recovery ------------------------------------------------------------------------


def test_a_paid_replacement_recovers_a_past_due_row_on_the_new_plan(w):
    upgrade_setup(w, status="PAST_DUE")
    sync_replacement(w.a)
    row, state = sub_row(w.a), read_state(w.a.merchant)
    assert (row.status, row.plan_id) == ("ACTIVE", w.big.pk)
    assert (row.past_due_at, row.dunning_stage) == (None, None)
    assert state.audits == ["billing.subscription_recovered", "billing.plan_changed"]
    assert state.payments[0][1] == PaymentAttempt.AttemptType.RETRY
    assert state.usages[-1] == (unix(ts(NEW_START)), unix(ts(NEW_END)), 0)
    assert row.payment_provider_ref == REPL_REF


def test_without_proof_a_past_due_row_stays_as_it_is(w):
    upgrade_setup(w, status="PAST_DUE")
    w.provider.invoices_by_ref[REPL_REF] = []
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


# --- an ended row: no switch, an audit for staff ----------------------------------------------


@pytest.mark.parametrize("status", ["CANCELLED", "EXPIRED"])
def test_a_paid_replacement_on_an_ended_row_does_not_switch_and_tells_staff_once(w, status):
    upgrade_setup(w, status=status)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    sync_replacement(w.a)  # a second sweep must not repeat the audit event
    after = read_state(w.a.merchant)
    assert after.audits == ["billing.replacement_paid_after_end"]
    assert (after.status, after.period, after.payments, after.usages) == (
        status,
        before.period,
        [],
        before.usages,
    )
    row = sub_row(w.a)
    assert (row.replacement_provider_ref, row.plan_id) == (REPL_REF, w.small.pk)  # untouched
    with tenant_context(w.a.merchant.id), tenant_atomic():
        meta = AuditLog.objects.get(action="billing.replacement_paid_after_end").metadata_json
    assert meta == {"row_status": status, "plan_name": w.big.name}  # names only, no refs


def test_an_unpaid_active_replacement_on_an_ended_row_writes_nothing(w):
    upgrade_setup(w, status="EXPIRED")
    w.provider.invoices_by_ref[REPL_REF] = []
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


# --- the replacement step inside sync_subscription ----------------------------------------------


def test_sync_subscription_switches_then_the_main_step_syncs_the_new_ref(w):
    upgrade_setup(w)
    sync(w.a)
    fetched = [c[1] for c in w.provider.calls if c[0] == "fetch_subscription"]
    assert fetched[0] == REPL_REF  # the replacement first
    assert fetched[-1] == REPL_REF  # then the main step, which now follows the new ref
    row = sub_row(w.a)
    assert (row.plan_id, row.payment_provider_ref) == (w.big.pk, REPL_REF)
    assert read_state(w.a.merchant).audits == ["billing.plan_changed"]


def test_a_failed_replacement_fetch_changes_nothing_and_the_main_step_still_runs(w):
    upgrade_setup(w)
    w.provider.fetch_errors[REPL_REF] = BillingProviderUnavailable()
    make_event(w.a.merchant, REPL_REF, "evt_new")
    sync(w.a)
    state = read_state(w.a.merchant)
    assert state.replacement_provider_ref == REPL_REF
    assert state.events == {"evt_new": False}
    assert ("fetch_subscription", w.ref(w.a)) in w.provider.calls


def test_the_replacement_status_is_never_recorded_on_the_row(w):
    """provider_status and provider_synced_at describe the main ref only."""
    w.sub(w.a, "ACTIVE", provider_status="active")
    set_replacement(w.a.merchant)
    provide_replacement(w.provider, w.big, status="authenticated", paid=False)
    before = sub_row(w.a)
    sync_replacement(w.a)
    after = sub_row(w.a)
    assert (after.provider_status, after.provider_synced_at) == (before.provider_status, before.provider_synced_at)


# --- tenant safety -----------------------------------------------------------------------------


def test_a_switch_never_touches_another_merchant(w):
    upgrade_setup(w)
    w.sub(w.b, "ACTIVE", plan=w.small)
    b_before = read_state(w.b.merchant)
    sync_replacement(w.a)
    assert read_state(w.b.merchant) == b_before
    assert sub_row(w.b).plan_id == w.small.pk


def test_the_replacement_step_reads_only_the_current_merchants_row(w):
    w.sub(w.a, "ACTIVE")
    w.sub(w.b, "ACTIVE")
    set_replacement(w.b.merchant)  # only B has a replacement
    provide_replacement(w.provider, w.big)
    sync_replacement(w.a)
    assert ("fetch_subscription", REPL_REF) not in w.provider.calls
