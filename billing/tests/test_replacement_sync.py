"""W3 of 07-plan-change-replacement: the sync and state-reconciliation changes.

What W3 changes in the merged sync (and nothing else):
- rule 5: while a committed downgrade replacement stands on an ACTIVE row (a
  replacement ref and replacement_committed_at set; pending_plan is NOT the
  discriminator), the old ref's terminal status does not move the row;
- per-ref event marking: a sync of the main ref leaves an event naming a live
  replacement ref unprocessed;
- maintenance_due is true for a row with a replacement or retired ref;
- `_apply_active` / `_audit_plan_change` take an optional `effective` (the audit
  row's effect); None keeps the merged behavior.

The rows are put into the replacement states by writing the columns directly
(replacement_helpers): the code that creates those states is a later phase.
Provider answers come from the FakeProvider. Nothing here claims any Razorpay
behavior; D1 is untouched (fetch_invoices is the fake)."""
from datetime import timedelta

import pytest

from auditlog.models import AuditLog
from billing import services
from billing.models import Subscription
from billing.tests.replacement_helpers import (
    REPL_REF,
    RETIRED_REF,
    clear_replacement,
    replacement_entity,
    set_replacement,
    set_retired,
)
from billing.tests.snapshot_helpers import END, START, entity, invoice
from billing.tests.state_helpers import make_event, read_state
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(lifecycle_setup):
    return lifecycle_setup


def sync(owner):
    # These tests cover the main-subscription step W3 owns. Since W4,
    # sync_subscription() also runs the replacement step first (test_replacement_switch
    # and test_replacement_abandon cover that), so the main step is driven alone here.
    with tenant_context(owner.merchant.id):
        services._sync_main()


def due(owner):
    with tenant_context(owner.merchant.id):
        return services.maintenance_due()


# --- rule 5: the terminal guard (discriminator: replacement_committed_at) ----------------


def test_a_committed_downgrade_replacement_defers_the_old_refs_terminal_status(w):
    """Rule 5: the commit scheduled the old subscription to end at its cycle end on
    purpose, so its `cancelled` does not move the row."""
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, committed=True, downgrade_to=w.small)
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="cancelled")
    make_event(w.a.merchant, w.ref(w.a))
    before = read_state(w.a.merchant)

    sync(w.a)

    after = read_state(w.a.merchant)
    assert after.status == "ACTIVE"
    assert after.provider_status == "cancelled"  # recorded, nothing else
    assert after.provider_synced_at is not None
    assert after.audits == before.audits == []
    assert (after.period, after.cancel_at_period_end) == (before.period, before.cancel_at_period_end)
    assert after.replacement_provider_ref == REPL_REF  # the replacement is untouched
    assert after.events == {"evt_1": True}  # the sync still settled


def test_the_commit_marker_alone_is_the_discriminator_not_pending_plan(w):
    """Committed with no pending_plan still defers: the discriminator is the marker."""
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, committed=True)  # no downgrade_to: pending_plan is NULL
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="cancelled")
    sync(w.a)
    assert read_state(w.a.merchant).status == "ACTIVE"


def test_a_replacement_with_pending_plan_but_no_commit_is_not_a_committed_downgrade(w):
    """The exact converse the spec does NOT support: replacement ref + pending_plan
    without replacement_committed_at must not be treated as a committed downgrade."""
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, committed=False, downgrade_to=w.small)
    w.provider.entities[REPL_REF] = replacement_entity(w.small)  # the row ends, so W4 re-reads it
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="cancelled")
    sync(w.a)
    assert read_state(w.a.merchant).status == "CANCELLED"


def test_an_upgrade_replacement_beside_a_provider_update_downgrade_is_not_deferred(w):
    """The edge case that ruled pending_plan out: a merged provider-update downgrade
    left pending_plan set, then an upgrade replacement exists (no commit point)."""
    w.sub(w.a, "ACTIVE", plan=w.big, pending_plan=w.small)
    set_replacement(w.a.merchant)  # an upgrade replacement: never committed
    w.provider.entities[REPL_REF] = replacement_entity(w.small)  # the row ends, so W4 re-reads it
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="cancelled")
    sync(w.a)
    assert read_state(w.a.merchant).status == "CANCELLED"


def test_an_upgrade_replacement_does_not_defer_it(w):
    w.sub(w.a, "ACTIVE")
    set_replacement(w.a.merchant)  # an upgrade replacement
    w.provider.entities[REPL_REF] = replacement_entity(w.small)  # the row ends, so W4 re-reads it
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="cancelled")
    sync(w.a)
    assert read_state(w.a.merchant).status == "CANCELLED"


def test_without_a_replacement_the_terminal_status_still_ends_the_row(w):
    w.sub(w.a, "ACTIVE")
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="cancelled")
    sync(w.a)
    state = read_state(w.a.merchant)
    assert state.status == "CANCELLED"
    assert state.audits == ["billing.subscription_cancelled"]


def test_a_pending_downgrade_without_a_replacement_does_not_defer_it(w):
    """The provider-update downgrade sets pending_plan with no replacement ref: the
    merged behavior applies."""
    w.sub(w.a, "ACTIVE", plan=w.big, pending_plan=w.small)
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="cancelled")
    sync(w.a)
    assert read_state(w.a.merchant).status == "CANCELLED"


def test_the_guard_applies_only_to_an_active_row(w):
    """A PAST_DUE row never keeps a committed replacement; if the columns are set
    anyway, the merged terminal handling applies."""
    w.sub(w.a, "PAST_DUE", plan=w.big)
    set_replacement(w.a.merchant, committed=True, downgrade_to=w.small)
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="cancelled")
    sync(w.a)
    assert read_state(w.a.merchant).status == "CANCELLED"


def test_once_the_replacement_is_cleared_the_old_terminal_status_applies(w):
    """Rule 5, second half: if the replacement fails it is cleared, and the old
    `cancelled` applies as today."""
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, committed=True, downgrade_to=w.small)
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="cancelled")
    sync(w.a)
    assert read_state(w.a.merchant).status == "ACTIVE"

    clear_replacement(w.a.merchant)
    sync(w.a)
    assert read_state(w.a.merchant).status == "CANCELLED"


@pytest.mark.parametrize("status", ["pending", "halted"])
def test_the_guard_does_not_touch_the_payment_failure_path(w, status):
    """Only the terminal statuses are deferred; a failed charge on the old
    subscription still starts the grace period (merged behavior)."""
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, committed=True, downgrade_to=w.small)
    w.provider.entities[w.ref(w.a)] = entity(w.big, status=status)
    sync(w.a)
    assert read_state(w.a.merchant).status == "PAST_DUE"


# --- per-ref event marking ---------------------------------------------------------------


def test_a_main_ref_sync_leaves_an_event_naming_the_replacement_ref_unprocessed(w):
    w.sub(w.a, "ACTIVE")
    set_replacement(w.a.merchant)
    make_event(w.a.merchant, w.ref(w.a), "evt_main")
    make_event(w.a.merchant, REPL_REF, "evt_repl")
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START, end=END)

    sync(w.a)

    assert read_state(w.a.merchant).events == {"evt_main": True, "evt_repl": False}
    assert due(w.a) is True  # the unprocessed replacement event keeps it due


def test_the_marking_is_unscoped_when_there_is_no_replacement(w):
    """With no replacement ref the filter is exactly the merged one: every old
    unprocessed event is marked, whatever ref it names."""
    w.sub(w.a, "ACTIVE")
    make_event(w.a.merchant, w.ref(w.a), "evt_1")
    make_event(w.a.merchant, "sub_some_other_ref", "evt_2")
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START, end=END)
    sync(w.a)
    assert read_state(w.a.merchant).events == {"evt_1": True, "evt_2": True}


def test_the_departing_ref_events_are_marked_at_the_next_settle_after_the_columns_clear(w):
    w.sub(w.a, "ACTIVE")
    set_replacement(w.a.merchant)
    make_event(w.a.merchant, REPL_REF, "evt_repl")
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START, end=END)
    sync(w.a)
    assert read_state(w.a.merchant).events == {"evt_repl": False}

    clear_replacement(w.a.merchant)
    sync(w.a)
    assert read_state(w.a.merchant).events == {"evt_repl": True}


def test_an_unsettled_main_sync_marks_nothing_even_with_a_replacement(w):
    """Fail closed is unchanged: a period not proven paid settles nothing."""
    w.sub(w.a, "ACTIVE")
    set_replacement(w.a.merchant)
    make_event(w.a.merchant, w.ref(w.a), "evt_main")
    new_start = START + timedelta(days=30)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=new_start)
    w.provider.invoices = []  # no qualifying paid invoice
    sync(w.a)
    assert read_state(w.a.merchant).events == {"evt_main": False}


def test_marking_is_per_merchant(w):
    """B's events are never touched by A's sync, replacement or not."""
    w.sub(w.a, "ACTIVE")
    w.sub(w.b, "ACTIVE")
    set_replacement(w.a.merchant)
    make_event(w.b.merchant, w.ref(w.b), "evt_b")
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START, end=END)
    sync(w.a)
    assert read_state(w.b.merchant).events == {"evt_b": False}


# --- maintenance_due ------------------------------------------------------------------------


def test_a_healthy_active_row_with_neither_ref_is_not_due(w):
    w.sub(w.a, "ACTIVE")
    assert due(w.a) is False


def test_a_replacement_ref_makes_the_row_due(w):
    w.sub(w.a, "ACTIVE")
    set_replacement(w.a.merchant)
    assert due(w.a) is True


def test_a_retired_ref_makes_the_row_due(w):
    w.sub(w.a, "ACTIVE")
    set_retired(w.a.merchant)
    assert due(w.a) is True


def test_due_is_per_merchant(w):
    w.sub(w.a, "ACTIVE")
    w.sub(w.b, "ACTIVE")
    set_replacement(w.a.merchant)
    set_retired(w.a.merchant, ref=RETIRED_REF)
    assert due(w.a) is True
    assert due(w.b) is False


# --- _apply_active(effective=...) -----------------------------------------------------------


def _apply(w, owner, *, entity_, invoices, plan, effective=None):
    """Run _apply_active under the row lock, as _apply_snapshot does, and save."""
    with tenant_context(owner.merchant.id), tenant_atomic():
        row = Subscription.objects.get()
        sub = services._lock_subscription(row.pk)
        fields = set()
        services._apply_active(sub, entity_, invoices, plan, fields, None, effective=effective)
        sub.save(update_fields=sorted(fields | {"updated_at"}))


def _plan_changed_rows(owner):
    with tenant_context(owner.merchant.id), tenant_atomic():
        return [
            a.metadata_json
            for a in AuditLog.objects.filter(action="billing.plan_changed").order_by("created_at")
        ]


def test_without_effective_the_plan_change_audit_is_unchanged(w):
    w.sub(w.a, "ACTIVE")
    new_start = END
    _apply(
        w,
        w.a,
        entity_=entity(w.big, status="active", start=new_start),
        invoices=[invoice(start=new_start)],
        plan=w.big,
    )
    rows = _plan_changed_rows(w.a)
    assert len(rows) == 1 and rows[0]["effective"] == "IMMEDIATE"


@pytest.mark.parametrize("effective", ["REPLACEMENT_IMMEDIATE", "REPLACEMENT_SCHEDULED"])
def test_effective_names_the_plan_change_audit_row(w, effective):
    w.sub(w.a, "ACTIVE")
    new_start = END
    _apply(
        w,
        w.a,
        entity_=entity(w.big, status="active", start=new_start),
        invoices=[invoice(start=new_start)],
        plan=w.big,
        effective=effective,
    )
    rows = _plan_changed_rows(w.a)
    assert len(rows) == 1 and rows[0]["effective"] == effective
    assert (rows[0]["from_plan"], rows[0]["to_plan"]) == (w.small.name, w.big.name)


def test_effective_on_a_past_due_row_writes_recovered_and_plan_changed(w):
    w.sub(w.a, "PAST_DUE")
    new_start = END
    _apply(
        w,
        w.a,
        entity_=entity(w.big, status="active", start=new_start),
        invoices=[invoice(start=new_start)],
        plan=w.big,
        effective="REPLACEMENT_IMMEDIATE",
    )
    state = read_state(w.a.merchant)
    assert state.status == "ACTIVE"
    assert state.audits == ["billing.subscription_recovered", "billing.plan_changed"]
    assert _plan_changed_rows(w.a)[0]["effective"] == "REPLACEMENT_IMMEDIATE"


def test_without_effective_a_past_due_recovery_writes_only_recovered(w):
    """The merged behavior: no plan_changed row on a PAST_DUE recovery."""
    w.sub(w.a, "PAST_DUE")
    new_start = END
    _apply(
        w,
        w.a,
        entity_=entity(w.big, status="active", start=new_start),
        invoices=[invoice(start=new_start)],
        plan=w.big,
    )
    assert read_state(w.a.merchant).audits == ["billing.subscription_recovered"]


def test_effective_without_a_plan_change_writes_no_plan_changed_row(w):
    w.sub(w.a, "PAST_DUE")
    new_start = END
    _apply(
        w,
        w.a,
        entity_=entity(w.small, status="active", start=new_start),
        invoices=[invoice(start=new_start)],
        plan=w.small,
        effective="REPLACEMENT_SCHEDULED",
    )
    assert read_state(w.a.merchant).audits == ["billing.subscription_recovered"]
