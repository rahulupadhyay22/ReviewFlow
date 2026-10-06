"""W4 of 07-plan-change-replacement: abandoning a replacement.

An abandon is a local state move (the replacement ref goes to the retired slot,
the replacement columns clear); it makes no provider call and never changes the
existing plan or entitlement. It never acts on a replacement the provider reports
`active`, never on a committed downgrade replacement (W6), and every decision
re-reads the replacement from the provider under the row lock.

Rows are put into place by writing the columns directly (checkout is W5);
Razorpay is only the FakeProvider."""
import logging
from datetime import timedelta

import pytest

from auditlog.models import AuditLog
from billing import razorpay, services
from billing.exceptions import (
    BillingProviderUnavailable,
    ProviderStateUnsupported,
    ReplacementActivating,
)
from billing.models import Subscription
from billing.tests.replacement_helpers import (
    REPL_REF,
    RETIRED_REF,
    provide_replacement,
    set_replacement,
    set_retired,
)
from billing.tests.snapshot_helpers import START, entity
from billing.tests.state_helpers import make_event, read_state
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

ABANDONED = "billing.replacement_abandoned"


@pytest.fixture
def w(lifecycle_setup):
    return lifecycle_setup


def sync_replacement(owner):
    with tenant_context(owner.merchant.id):
        services._sync_replacement()


def sync_main(owner):
    with tenant_context(owner.merchant.id):
        services._sync_main()


def sub_row(owner):
    with tenant_context(owner.merchant.id), tenant_atomic():
        return Subscription.objects.select_related("plan").get()


def abandon_meta(owner):
    with tenant_context(owner.merchant.id), tenant_atomic():
        return [a.metadata_json for a in AuditLog.objects.filter(action=ABANDONED).order_by("created_at")]


def provider_writes(w):
    return [
        c for c in w.provider.calls if c[0] in ("create_subscription", "update_subscription", "cancel_subscription")
    ]


def setup(w, *, status="ACTIVE", repl_status="halted", paid=False, **repl):
    """A on the small plan with an upgrade replacement the provider reports as
    `repl_status`."""
    w.sub(w.a, status, plan=w.small)
    set_replacement(w.a.merchant, **{"plan": w.big, **repl})
    provide_replacement(w.provider, w.big, status=repl_status, paid=paid)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START)


def assert_abandoned(w, before):
    row, state = sub_row(w.a), read_state(w.a.merchant)
    assert (row.retired_provider_ref, row.retired_kind) == (REPL_REF, "ABANDONED_REPLACEMENT")
    assert (
        row.replacement_provider_ref,
        row.replacement_expires_at,
        row.replacement_committed_at,
        row.replacement_cancel_confirmed_at,
    ) == (None, None, None, None)
    # the existing plan, status, period, ref and entitlement are exactly as they were
    assert (state.status, state.period, row.plan_id, row.payment_provider_ref) == (
        before.status,
        before.period,
        w.small.pk,
        w.ref(w.a),
    )
    assert (state.payments, state.usages) == (before.payments, before.usages)
    assert provider_writes(w) == []  # an abandon never calls the provider's write API


# --- a failed or lapsed replacement on an ACTIVE row --------------------------------------------


@pytest.mark.parametrize("status", ["pending", "halted", "cancelled", "expired"])
def test_a_failed_replacement_is_abandoned_and_the_plan_is_unchanged(w, status):
    setup(w, repl_status=status)
    make_event(w.a.merchant, REPL_REF, "evt_repl")
    before = read_state(w.a.merchant)

    sync_replacement(w.a)

    assert_abandoned(w, before)
    state = read_state(w.a.merchant)
    assert state.audits == [ABANDONED]
    assert abandon_meta(w.a) == [{"reason": "provider_failed", "plan_name": w.big.name}]
    assert state.events == {"evt_repl": True}  # nothing can match the departed ref again


def test_a_downgrade_replacement_abandon_clears_the_pending_plan(w):
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, downgrade_to=w.small)
    provide_replacement(w.provider, w.small, status="halted", paid=False)
    sync_replacement(w.a)
    row = sub_row(w.a)
    assert (row.pending_plan_id, row.plan_id, row.retired_provider_ref) == (None, w.big.pk, REPL_REF)


def test_an_earlier_provider_update_downgrade_survives_an_upgrade_replacement_abandon(w):
    """pending_plan from a provider-update downgrade is not the replacement's
    target, so abandoning an upgrade replacement leaves it in place."""
    w.sub(w.a, "ACTIVE", plan=w.small, pending_plan=w.small)
    set_replacement(w.a.merchant, plan=w.big)
    provide_replacement(w.provider, w.big, status="halted", paid=False)
    sync_replacement(w.a)
    assert sub_row(w.a).pending_plan_id == w.small.pk


def test_created_before_its_deadline_is_kept(w):
    setup(w, repl_status="created")
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


def test_created_past_its_deadline_is_abandoned(w):
    setup(w, repl_status="created", expires_in=timedelta(minutes=-1))
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert_abandoned(w, before)
    assert abandon_meta(w.a) == [{"reason": "deadline_passed", "plan_name": w.big.name}]


def test_authenticated_is_not_abandoned_by_the_deadline(w):
    """Only `created` lapses at the deadline; an authorized replacement awaits its charge."""
    setup(w, repl_status="authenticated", expires_in=timedelta(minutes=-1))
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


@pytest.mark.parametrize("status", ["paused", "completed", "something_new"])
def test_a_status_reviewflow_does_not_know_is_left_alone(w, status):
    setup(w, repl_status=status)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


# --- protected: active, committed, unreadable ----------------------------------------------------


def test_an_active_replacement_is_never_abandoned_even_unpaid(w):
    setup(w, repl_status="active", paid=False)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


def test_a_stale_first_read_cannot_abandon_a_replacement_that_became_active(w, monkeypatch):
    """The sync read `halted`; by the locked re-read the provider says `active`."""
    setup(w, repl_status="halted")
    reads = []
    real = w.provider.fetch_subscription

    def flipping(ref):
        entity_ = real(ref)
        if ref == REPL_REF:
            reads.append(ref)
            if len(reads) > 1:
                return {**entity_, "status": "active"}
        return entity_

    monkeypatch.setattr(razorpay, "fetch_subscription", flipping)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert len(reads) == 2  # the sync's read, then the re-read under the lock
    assert read_state(w.a.merchant) == before


def test_an_unreadable_replacement_under_the_lock_is_left_as_it_is(w, monkeypatch):
    setup(w, repl_status="halted")
    real = w.provider.fetch_subscription
    calls = []

    def second_read_fails(ref):
        if ref == REPL_REF:
            calls.append(ref)
            if len(calls) > 1:
                raise BillingProviderUnavailable()
        return real(ref)

    monkeypatch.setattr(razorpay, "fetch_subscription", second_read_fails)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


@pytest.mark.parametrize("status", ["created", "authenticated"])
def test_a_committed_downgrade_replacement_is_not_abandoned_here(w, status):
    """A committed downgrade replacement is never abandoned to keep the old plan. (One
    that has FAILED is handled separately, W6: test_replacement_commit.py.) The old
    cancel is already confirmed here, so the sweep has nothing to re-issue either."""
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, committed=True, downgrade_to=w.small, expires_in=timedelta(minutes=-1))
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(replacement_cancel_confirmed_at=services.dj_timezone.now())
    provide_replacement(w.provider, w.small, status=status, paid=False)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


def test_an_occupied_retired_slot_blocks_the_abandon_rather_than_losing_the_ref(w):
    setup(w, repl_status="halted")
    set_retired(w.a.merchant)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before
    assert read_state(w.a.merchant).retired_provider_ref == RETIRED_REF


def test_a_second_run_does_nothing(w):
    setup(w, repl_status="halted")
    sync_replacement(w.a)
    once = read_state(w.a.merchant)
    sync_replacement(w.a)
    sync_main(w.a)
    assert read_state(w.a.merchant).audits == once.audits == [ABANDONED]


# --- the PAST_DUE transition ---------------------------------------------------------------------


@pytest.mark.parametrize("repl_status", ["created", "authenticated", "pending", "halted"])
def test_the_past_due_transition_abandons_a_non_active_uncommitted_replacement(w, repl_status):
    setup(w, repl_status=repl_status)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="halted")
    sync_main(w.a)
    row, state = sub_row(w.a), read_state(w.a.merchant)
    assert (row.status, row.plan_id) == ("PAST_DUE", w.small.pk)  # the existing plan is preserved
    assert (row.retired_provider_ref, row.retired_kind) == (REPL_REF, "ABANDONED_REPLACEMENT")
    assert row.replacement_provider_ref is None
    assert state.audits == ["billing.subscription_past_due", ABANDONED]
    assert abandon_meta(w.a) == [{"reason": "past_due", "plan_name": w.big.name}]
    assert provider_writes(w) == []


def test_the_past_due_transition_never_cancels_an_active_replacement(w):
    setup(w, repl_status="active", paid=False)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="halted")
    sync_main(w.a)
    row = sub_row(w.a)
    assert (row.status, row.replacement_provider_ref, row.retired_provider_ref) == ("PAST_DUE", REPL_REF, None)
    assert read_state(w.a.merchant).audits == ["billing.subscription_past_due"]
    assert provider_writes(w) == []


def test_the_past_due_transition_leaves_a_committed_replacement(w):
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, committed=True, downgrade_to=w.small)
    provide_replacement(w.provider, w.small, status="authenticated", paid=False)
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="halted")
    sync_main(w.a)
    row = sub_row(w.a)
    assert (row.status, row.replacement_provider_ref) == ("PAST_DUE", REPL_REF)


def test_an_unreadable_replacement_does_not_stop_the_past_due_transition_or_get_abandoned(w):
    setup(w, repl_status="authenticated")
    w.provider.fetch_errors[REPL_REF] = BillingProviderUnavailable()
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="halted")
    sync_main(w.a)
    row = sub_row(w.a)
    assert (row.status, row.replacement_provider_ref) == ("PAST_DUE", REPL_REF)  # retried by the sweep


def test_the_sweep_abandons_a_replacement_the_transition_could_not_read(w):
    setup(w, repl_status="authenticated")
    w.provider.fetch_errors[REPL_REF] = BillingProviderUnavailable()
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="halted")
    sync_main(w.a)
    del w.provider.fetch_errors[REPL_REF]
    sync_replacement(w.a)
    row = sub_row(w.a)
    assert (row.status, row.retired_provider_ref, row.replacement_provider_ref) == ("PAST_DUE", REPL_REF, None)
    assert abandon_meta(w.a) == [{"reason": "past_due", "plan_name": w.big.name}]


# --- the row ends ----------------------------------------------------------------------------------


def test_the_row_ending_abandons_an_uncommitted_non_active_replacement(w):
    setup(w, repl_status="authenticated")
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="cancelled")
    sync_main(w.a)
    row, state = sub_row(w.a), read_state(w.a.merchant)
    assert (row.status, row.retired_provider_ref, row.replacement_provider_ref) == ("CANCELLED", REPL_REF, None)
    assert state.audits == ["billing.subscription_cancelled", ABANDONED]
    assert abandon_meta(w.a) == [{"reason": "row_ended", "plan_name": w.big.name}]


def test_a_grace_expiry_by_the_provider_terminal_status_abandons_it_too(w):
    setup(w, status="PAST_DUE", repl_status="authenticated")
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(past_due_at=services.dj_timezone.now() - timedelta(days=8))
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="expired")
    sync_main(w.a)
    row = sub_row(w.a)
    assert (row.status, row.retired_provider_ref, row.replacement_provider_ref) == ("EXPIRED", REPL_REF, None)


def test_a_row_ending_never_cancels_an_active_replacement(w):
    setup(w, repl_status="active", paid=False)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="cancelled")
    sync_main(w.a)
    row = sub_row(w.a)
    assert (row.status, row.replacement_provider_ref, row.retired_provider_ref) == ("CANCELLED", REPL_REF, None)
    assert provider_writes(w) == []


def test_a_merchant_cancel_of_a_past_due_row_abandons_a_non_active_replacement(w):
    setup(w, status="PAST_DUE", repl_status="created")
    with tenant_context(w.a.merchant.id), tenant_atomic():
        services.cancel_subscription(actor=w.a)
    row = sub_row(w.a)
    assert (row.status, row.retired_provider_ref, row.replacement_provider_ref) == ("CANCELLED", REPL_REF, None)
    assert read_state(w.a.merchant).audits == [ABANDONED, "billing.cancellation_requested"]  # the replacement goes first


def test_a_merchant_cancel_of_a_past_due_row_with_an_active_replacement_is_409_and_changes_nothing(w):
    """W6: an active replacement is never cancelled, not even by the merchant."""
    setup(w, status="PAST_DUE", repl_status="active", paid=False)
    before = read_state(w.a.merchant)
    with pytest.raises(ReplacementActivating):
        with tenant_context(w.a.merchant.id), tenant_atomic():
            services.cancel_subscription(actor=w.a)
    assert read_state(w.a.merchant) == before
    assert provider_writes(w) == []


@pytest.mark.parametrize("status", ["PAST_DUE", "CANCELLED", "EXPIRED"])
def test_the_sweep_abandons_on_a_row_that_is_not_active_whatever_the_non_active_status(w, status):
    setup(w, status=status, repl_status="authenticated")
    sync_replacement(w.a)
    assert sub_row(w.a).retired_provider_ref == REPL_REF
    assert abandon_meta(w.a)[0]["reason"] == ("past_due" if status == "PAST_DUE" else "row_ended")


def test_the_day_seven_expiry_makes_no_provider_call_and_the_next_sweep_abandons(w):
    """advance_dunning is unchanged (its locked expiry never waits on a provider
    call): the replacement is abandoned by the next maintenance sync instead."""
    setup(w, status="PAST_DUE", repl_status="authenticated")
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(past_due_at=services.dj_timezone.now() - timedelta(days=8))
    w.provider.calls.clear()
    with tenant_context(w.a.merchant.id):
        services.advance_dunning()
    assert sub_row(w.a).status == "EXPIRED"
    assert ("fetch_subscription", REPL_REF) not in w.provider.calls
    assert sub_row(w.a).replacement_provider_ref == REPL_REF

    sync_replacement(w.a)
    assert sub_row(w.a).retired_provider_ref == REPL_REF


# --- the D2 guard on a new checkout -------------------------------------------------------------


def as_owner(w, fn, **kwargs):
    with tenant_context(w.a.merchant.id), tenant_atomic():
        return fn(actor=w.a, **kwargs)


def ended_with_replacement(w, *, repl_status):
    w.sub(w.a, "CANCELLED", plan=w.small, provider_status="cancelled")
    set_replacement(w.a.merchant)
    provide_replacement(w.provider, w.big, status=repl_status, paid=False)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="cancelled", start=START)


def test_a_checkout_on_an_ended_row_abandons_a_non_active_replacement_first(w):
    ended_with_replacement(w, repl_status="authenticated")
    result = as_owner(w, services.start_checkout, plan_id=w.small.id)
    row = sub_row(w.a)
    assert result.created is True
    assert (row.status, row.replacement_provider_ref) == ("INCOMPLETE", None)
    assert (row.retired_provider_ref, row.retired_kind) == (REPL_REF, "ABANDONED_REPLACEMENT")  # survives the reset
    assert read_state(w.a.merchant).audits == [ABANDONED, "billing.checkout_started"]
    assert ("cancel_subscription", REPL_REF, False) not in w.provider.calls  # the sweep owns that


def test_a_checkout_on_a_row_holding_an_active_replacement_is_refused(w):
    ended_with_replacement(w, repl_status="active")
    before = read_state(w.a.merchant)
    with pytest.raises(ReplacementActivating) as caught:
        as_owner(w, services.start_checkout, plan_id=w.small.id)
    assert (caught.value.http_status, caught.value.code) == (409, "replacement_activating")
    assert read_state(w.a.merchant) == before
    assert provider_writes(w) == []


def test_a_checkout_with_an_unreadable_replacement_fails_closed(w):
    ended_with_replacement(w, repl_status="authenticated")
    w.provider.fetch_errors[REPL_REF] = BillingProviderUnavailable()
    before = read_state(w.a.merchant)
    with pytest.raises(BillingProviderUnavailable):
        as_owner(w, services.start_checkout, plan_id=w.small.id)
    assert read_state(w.a.merchant) == before
    assert provider_writes(w) == []


def test_a_checkout_with_a_replacement_it_may_not_touch_is_refused(w):
    ended_with_replacement(w, repl_status="paused")
    before = read_state(w.a.merchant)
    with pytest.raises(ProviderStateUnsupported):
        as_owner(w, services.start_checkout, plan_id=w.small.id)
    assert read_state(w.a.merchant) == before


def test_a_failed_new_checkout_rolls_the_abandon_back_with_it(w):
    ended_with_replacement(w, repl_status="authenticated")
    w.provider.create_error = BillingProviderUnavailable()
    before = read_state(w.a.merchant)
    with pytest.raises(BillingProviderUnavailable):
        as_owner(w, services.start_checkout, plan_id=w.small.id)
    assert read_state(w.a.merchant) == before  # nothing half-done


# --- scope, tenant safety, hygiene ----------------------------------------------------------------


def test_an_abandon_never_touches_another_merchant(w):
    setup(w, repl_status="halted")
    w.sub(w.b, "ACTIVE", plan=w.small)
    b_before = read_state(w.b.merchant)
    sync_replacement(w.a)
    assert read_state(w.b.merchant) == b_before


def test_the_maintenance_check_stays_due_while_the_abandoned_ref_awaits_its_cancel(w):
    setup(w, repl_status="halted")
    sync_replacement(w.a)
    with tenant_context(w.a.merchant.id):
        assert services.maintenance_due() is True  # the retired ref is owed (W6 sweeps it)


def test_nothing_is_logged_or_audited_with_a_ref(w, caplog):
    setup(w, repl_status="halted")
    make_event(w.a.merchant, REPL_REF, "evt_repl")
    with caplog.at_level(logging.DEBUG):
        sync_replacement(w.a)
    text = caplog.text + str(abandon_meta(w.a))
    assert REPL_REF not in text and w.ref(w.a) not in text and "evt_repl" not in text
