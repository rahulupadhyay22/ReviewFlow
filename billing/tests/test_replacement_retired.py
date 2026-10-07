"""W6 of 07-plan-change-replacement: the retired subscription (`_sync_retired`) and
the complete replacement -> main -> retired wiring of `sync_subscription`.

The retired slot holds one subscription awaiting confirmed termination: the old
subscription after a switch (SWITCHED_OLD, cancelled in any non-terminal state,
`active` included) or an abandoned replacement (ABANDONED_REPLACEMENT, cancelled only
while `created` or `authenticated`, never when `active`). The slot clears only on a
confirmed terminal status. Razorpay is only the FakeProvider; nothing here claims any
Razorpay behavior."""
import logging

import pytest

from billing import razorpay, services
from billing.exceptions import BillingProviderRejected, BillingProviderUnavailable
from billing.models import Subscription
from billing.tests.replacement_helpers import RETIRED_REF, set_replacement, set_retired
from billing.tests.snapshot_helpers import START, entity
from billing.tests.state_helpers import make_event, read_state
from billing.tests.webhook_helpers import ORIGINAL_FETCH_INVOICES
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

SWITCHED = Subscription.RetiredKind.SWITCHED_OLD
ABANDONED = Subscription.RetiredKind.ABANDONED_REPLACEMENT


@pytest.fixture
def w(lifecycle_setup):
    return lifecycle_setup


def retired_step(owner):
    with tenant_context(owner.merchant.id):
        services._sync_retired()


def slot(w, owner=None):
    with tenant_context((owner or w.a).merchant.id), tenant_atomic():
        row = Subscription.objects.get()
        return row.retired_provider_ref, row.retired_kind


def provider_cancels(w):
    return [c for c in w.provider.calls if c[0] == "cancel_subscription"]


def retire(w, status, *, kind=SWITCHED, ref=RETIRED_REF):
    w.sub(w.a, "ACTIVE")
    set_retired(w.a.merchant, ref=ref, kind=kind)
    w.provider.add(ref, status=status, plan_id="plan_small")


# --- SWITCHED_OLD: cancelled in any non-terminal state --------------------------------------------


@pytest.mark.parametrize("status", ["active", "created", "authenticated", "pending", "halted", "paused"])
def test_a_switched_old_subscription_is_cancelled_at_once_in_any_non_terminal_state(w, status):
    retire(w, status)
    retired_step(w.a)
    assert provider_cancels(w) == [("cancel_subscription", RETIRED_REF, False)]  # immediate, not at cycle end
    assert slot(w) == (None, None)  # the provider answered terminal: confirmed, cleared


@pytest.mark.parametrize("status", ["cancelled", "completed", "expired"])
@pytest.mark.parametrize("kind", [SWITCHED, ABANDONED])
def test_a_terminal_retired_subscription_clears_the_slot_without_a_cancel(w, status, kind):
    retire(w, status, kind=kind)
    retired_step(w.a)
    assert provider_cancels(w) == []
    assert slot(w) == (None, None)


# --- ABANDONED_REPLACEMENT: created/authenticated only, never active --------------------------------


@pytest.mark.parametrize("status", ["created", "authenticated"])
def test_an_abandoned_created_or_authenticated_replacement_is_cancelled(w, status):
    retire(w, status, kind=ABANDONED)
    retired_step(w.a)
    assert provider_cancels(w) == [("cancel_subscription", RETIRED_REF, False)]
    assert slot(w) == (None, None)


def test_an_abandoned_replacement_the_provider_reports_active_is_never_cancelled(w):
    retire(w, "active", kind=ABANDONED)
    before = read_state(w.a.merchant)
    retired_step(w.a)
    assert provider_cancels(w) == []
    assert read_state(w.a.merchant) == before
    assert slot(w) == (RETIRED_REF, "ABANDONED_REPLACEMENT")  # protected, kept


@pytest.mark.parametrize("status", ["pending", "halted", "paused", "something_new"])
def test_an_abandoned_replacement_in_any_other_state_is_left_alone(w, status):
    retire(w, status, kind=ABANDONED)
    retired_step(w.a)
    assert provider_cancels(w) == []
    assert slot(w) == (RETIRED_REF, "ABANDONED_REPLACEMENT")


# --- the slot clears only on a confirmed terminal status ------------------------------------------------


def test_a_cancel_answer_that_is_not_terminal_keeps_the_slot_for_the_next_sweep(w, monkeypatch):
    retire(w, "active")
    real = w.provider.cancel_subscription
    monkeypatch.setattr(razorpay, "cancel_subscription", lambda ref, at_cycle_end: {"id": ref, "status": "pending"})
    retired_step(w.a)
    assert slot(w) == (RETIRED_REF, "SWITCHED_OLD")
    monkeypatch.setattr(razorpay, "cancel_subscription", real)
    retired_step(w.a)  # the next sweep confirms
    assert slot(w) == (None, None)


@pytest.mark.parametrize("answer", [None, "not a dict", {"id": "sub_x", "status": 5}, {"id": "sub_x"}])
def test_a_malformed_cancel_answer_keeps_the_slot(w, answer, monkeypatch):
    retire(w, "active")
    monkeypatch.setattr(razorpay, "cancel_subscription", lambda ref, at_cycle_end: answer)
    retired_step(w.a)
    assert slot(w) == (RETIRED_REF, "SWITCHED_OLD")


@pytest.mark.parametrize(
    "error", [BillingProviderUnavailable(), BillingProviderRejected("BAD_REQUEST_ERROR", status=400)]
)
def test_a_failed_cancel_keeps_the_slot_and_is_retried(w, error):
    retire(w, "active")
    w.provider.cancel_errors[RETIRED_REF] = error
    retired_step(w.a)
    assert slot(w) == (RETIRED_REF, "SWITCHED_OLD")
    del w.provider.cancel_errors[RETIRED_REF]
    retired_step(w.a)
    assert slot(w) == (None, None)
    assert len(provider_cancels(w)) == 2


@pytest.mark.parametrize("error", [BillingProviderUnavailable(), BillingProviderRejected("X", status=404)])
def test_a_failed_fetch_changes_nothing_and_cancels_nothing(w, error):
    retire(w, "active")
    w.provider.fetch_errors[RETIRED_REF] = error
    before = read_state(w.a.merchant)
    retired_step(w.a)
    assert read_state(w.a.merchant) == before and provider_cancels(w) == []


@pytest.mark.parametrize("answer", [{"id": "sub_other", "status": "active"}, {"id": RETIRED_REF}, {"id": RETIRED_REF, "status": 3}])
def test_a_malformed_or_mismatched_entity_is_ignored(w, answer):
    retire(w, "active")
    w.provider.entities[RETIRED_REF] = answer
    retired_step(w.a)
    assert provider_cancels(w) == [] and slot(w)[0] == RETIRED_REF


def test_nothing_happens_without_a_retired_ref_or_a_subscription(w):
    w.sub(w.a, "ACTIVE")
    retired_step(w.a)
    retired_step(w.b)  # B has no subscription at all
    assert w.provider.calls == []


def test_clearing_the_slot_marks_that_refs_events_processed(w):
    retire(w, "cancelled")
    make_event(w.a.merchant, RETIRED_REF, "evt_old")
    retired_step(w.a)
    assert read_state(w.a.merchant).events == {"evt_old": True}


def test_the_slot_cleared_can_take_a_new_replacement_afterwards(w):
    retire(w, "active")
    retired_step(w.a)
    set_replacement(w.a.merchant, ref="sub_next_repl")  # the unique constraints and the slot are free again
    assert slot(w) == (None, None)


# --- the retired step never needs the invoices (D1 stays fail-closed) -------------------------------------


def test_the_retired_step_never_fetches_invoices(w):
    retire(w, "active")
    retired_step(w.a)
    assert [c for c in w.provider.calls if c[0] == "fetch_invoices"] == []


# --- the wiring: replacement -> main -> retired -------------------------------------------------------------


def test_sync_subscription_runs_all_three_steps_in_order(w):
    w.sub(w.a, "ACTIVE")
    old = w.ref(w.a)
    w.provider.entities[old] = entity(w.small, status="active", start=START)
    set_replacement(w.a.merchant, plan=w.big)
    w.provider.add("sub_test_repl", status="authenticated", plan_id=w.big.provider_plan_id)
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(retired_provider_ref=RETIRED_REF, retired_kind=ABANDONED)
    w.provider.add(RETIRED_REF, status="created", plan_id="plan_small")
    with tenant_context(w.a.merchant.id):
        services.sync_subscription()
    fetched = [c[1] for c in w.provider.calls if c[0] == "fetch_subscription"]
    assert fetched == ["sub_test_repl", old, RETIRED_REF]


def test_a_failing_replacement_step_does_not_stop_the_main_and_retired_steps(w, monkeypatch):
    """The D1 stop in the replacement step (an `active` replacement needs the invoices)
    still propagates, after the main and retired steps have run."""
    monkeypatch.setattr(razorpay, "fetch_invoices", ORIGINAL_FETCH_INVOICES)
    w.sub(w.a, "ACTIVE")
    old = w.ref(w.a)
    w.provider.entities[old] = entity(w.small, status="active", start=START)
    set_replacement(w.a.merchant, plan=w.big)
    w.provider.add("sub_test_repl", status="active", plan_id=w.big.provider_plan_id, current_start=1, current_end=2)
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(retired_provider_ref=RETIRED_REF, retired_kind=SWITCHED)
    w.provider.add(RETIRED_REF, status="active", plan_id="plan_small")
    with tenant_context(w.a.merchant.id):
        with pytest.raises(NotImplementedError):
            services.sync_subscription()
    fetched = [c[1] for c in w.provider.calls if c[0] == "fetch_subscription"]
    assert fetched[-2:] == [old, RETIRED_REF]
    assert provider_cancels(w) == [("cancel_subscription", RETIRED_REF, False)]  # the retired step ran


def test_a_switch_and_the_old_cancel_happen_in_the_same_sweep(w):
    from billing.tests.replacement_helpers import provide_replacement

    w.sub(w.a, "ACTIVE", plan=w.small)
    old = w.ref(w.a)
    w.provider.entities[old] = entity(w.small, status="active", start=START)
    set_replacement(w.a.merchant, plan=w.big)
    provide_replacement(w.provider, w.big)
    with tenant_context(w.a.merchant.id):
        services.sync_subscription()
    assert ("cancel_subscription", old, False) in w.provider.calls  # only after the switch
    assert slot(w) == (None, None)


# --- tenant isolation and hygiene ------------------------------------------------------------------------------


def test_a_retired_step_never_touches_another_merchant(w):
    retire(w, "active")
    w.sub(w.b, "ACTIVE")
    set_retired(w.b.merchant, ref="sub_B_retired")
    w.provider.add("sub_B_retired", status="active", plan_id="plan_small")
    retired_step(w.a)
    assert provider_cancels(w) == [("cancel_subscription", RETIRED_REF, False)]
    assert slot(w, w.b) == ("sub_B_retired", "SWITCHED_OLD")


def test_nothing_is_logged_with_a_ref(w, caplog):
    retire(w, "active")
    w.provider.cancel_errors[RETIRED_REF] = BillingProviderUnavailable()
    with caplog.at_level(logging.DEBUG):
        retired_step(w.a)
    assert RETIRED_REF not in caplog.text and w.ref(w.a) not in caplog.text
