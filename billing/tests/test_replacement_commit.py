"""W6 of 07-plan-change-replacement: the downgrade commit point (T1, the provider
cancel of the old subscription, T2), the sweep's re-issue of an unconfirmed cancel,
and the verification in front of them.

The G-2 entity field names are SYNTHETIC here (no real Razorpay field is known;
none is asserted): the tests monkeypatch billing.razorpay.DOWNGRADE_START_FIELD and
DOWNGRADE_EXPIRY_FIELD and build entities that carry those keys. Razorpay is only the
FakeProvider. Whether a transaction or lock is held across the provider call is
proved in test_replacement_commit_concurrency.py (real transactions)."""
from datetime import datetime, timedelta, timezone

import pytest
from django.utils import timezone as dj_timezone

from auditlog.models import AuditLog
from billing import razorpay, services
from billing.exceptions import BillingProviderRejected, BillingProviderUnavailable
from billing.models import Subscription
from billing.tests.replacement_helpers import REPL_REF, provide_replacement, set_replacement
from billing.tests.snapshot_helpers import END, START, entity, ts
from billing.tests.state_helpers import read_state
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

MARGIN = 3600  # seconds
START_KEY, EXPIRY_KEY = "synthetic_start", "synthetic_expiry"
EXPIRES = int(END.timestamp()) - MARGIN - 1  # the commit cutoff minus exactly 1 second
COMMITTED, ABANDONED = "billing.replacement_committed", "billing.replacement_abandoned"


class Crash(BaseException):
    """A process dying between T1 and the provider call (not an Exception)."""


@pytest.fixture
def w(lifecycle_setup, settings, monkeypatch):
    """A is ACTIVE on the big plan with an authenticated downgrade replacement to
    the small plan, verifiable in every respect."""
    w = lifecycle_setup
    settings.BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN = MARGIN
    monkeypatch.setattr(razorpay, "DOWNGRADE_START_FIELD", START_KEY)
    monkeypatch.setattr(razorpay, "DOWNGRADE_EXPIRY_FIELD", EXPIRY_KEY)
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, downgrade_to=w.small)
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(replacement_expires_at=datetime.fromtimestamp(
            EXPIRES, tz=timezone.utc))
    provide_replacement(w.provider, w.small, status="authenticated", paid=False, start=END)
    w.provider.entities[REPL_REF].update({START_KEY: int(END.timestamp()), EXPIRY_KEY: EXPIRES})
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="active", start=START)
    return w


def sync_replacement(owner):
    with tenant_context(owner.merchant.id):
        services._sync_replacement()


def sub_row(owner):
    with tenant_context(owner.merchant.id), tenant_atomic():
        return Subscription.objects.select_related("plan", "pending_plan", "replacement_plan").get()


def cancels(w):
    return [c for c in w.provider.calls if c[0] == "cancel_subscription"]


def audit_rows(w, action):
    with tenant_context(w.a.merchant.id), tenant_atomic():
        return list(AuditLog.objects.filter(action=action).order_by("created_at"))


# --- the commit ----------------------------------------------------------------------------------


def test_a_verified_authenticated_downgrade_is_committed_and_the_old_cancel_confirmed(w):
    before = read_state(w.a.merchant)
    sync_replacement(w.a)

    row, state = sub_row(w.a), read_state(w.a.merchant)
    assert row.replacement_committed_at is not None and row.replacement_cancel_confirmed_at is not None
    assert cancels(w) == [("cancel_subscription", w.ref(w.a), True)]  # the OLD ref, at its cycle end
    # the old plan, period and entitlement are untouched until the switch
    assert (state.status, state.period, row.plan_id) == (before.status, before.period, w.big.pk)
    assert (row.replacement_provider_ref, row.pending_plan_id) == (REPL_REF, w.small.pk)
    assert state.audits == [COMMITTED]
    (committed,) = audit_rows(w, COMMITTED)
    assert committed.metadata_json == {
        "from_plan": w.big.name,
        "to_plan": w.small.name,
        "effective": "REPLACEMENT_SCHEDULED",
    }
    assert [c for c in w.provider.calls if c[0] == "fetch_invoices"] == []  # D1 is not involved


def test_the_old_cancel_is_issued_only_after_the_replacement_is_reread_and_verified(w):
    sync_replacement(w.a)
    names = [(c[0], c[1]) for c in w.provider.calls]
    reads = [i for i, c in enumerate(names) if c == ("fetch_subscription", REPL_REF)]
    cancel_at = names.index(("cancel_subscription", w.ref(w.a)))
    assert len(reads) == 2 and reads[1] < cancel_at  # the sync's read, then the T1 re-read, then the cancel


def test_a_second_sweep_commits_nothing_more_and_cancels_nothing_more(w):
    sync_replacement(w.a)
    once = read_state(w.a.merchant)
    calls = len(w.provider.calls)
    sync_replacement(w.a)
    assert read_state(w.a.merchant).audits == once.audits == [COMMITTED]
    assert len(cancels(w)) == 1
    assert [c for c in w.provider.calls[calls:] if c[0] == "cancel_subscription"] == []


def test_a_committed_downgrade_defers_the_old_refs_cancelled_status(w):
    """Rule 5 (W3), now with a real commit: the scheduled cancel ends the old subscription
    at its cycle end on purpose, so its `cancelled` does not move the row."""
    sync_replacement(w.a)
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="cancelled", start=START)
    with tenant_context(w.a.merchant.id):
        services._sync_main()
    assert sub_row(w.a).status == "ACTIVE"


def test_a_paid_committed_replacement_then_switches_through_the_existing_path(w):
    sync_replacement(w.a)
    provide_replacement(w.provider, w.small, status="active", start=END)
    with tenant_context(w.a.merchant.id):
        services.sync_subscription()
    row = sub_row(w.a)
    assert (row.plan_id, row.payment_provider_ref) == (w.small.pk, REPL_REF)
    assert (row.replacement_committed_at, row.replacement_cancel_confirmed_at) == (None, None)
    # the same sweep's retired step cancelled the old subscription (still `active` at
    # the provider here) and, seeing it terminal, cleared the slot
    assert ("cancel_subscription", w.ref(w.a), False) in w.provider.calls
    assert (row.retired_provider_ref, row.retired_kind) == (None, None)


# --- verification: any failure means no commit, no old cancel, an abandon --------------------------


def _mutate(w, **changes):
    w.provider.entities[REPL_REF].update(changes)


FAILURES = {
    "wrong_plan": lambda w: _mutate(w, plan_id="plan_somebody_else"),
    "start_not_the_old_period_end": lambda w: _mutate(w, **{START_KEY: int(END.timestamp()) + 60}),
    "start_unreadable": lambda w: w.provider.entities[REPL_REF].pop(START_KEY),
    "start_not_an_int": lambda w: _mutate(w, **{START_KEY: "soon"}),
    "expiry_not_the_stored_value": lambda w: _mutate(w, **{EXPIRY_KEY: EXPIRES + 1}),
    "expiry_unreadable": lambda w: w.provider.entities[REPL_REF].pop(EXPIRY_KEY),
    "expiry_not_an_int": lambda w: _mutate(w, **{EXPIRY_KEY: None}),
}


@pytest.mark.parametrize("name", sorted(FAILURES))
def test_every_failed_or_unreadable_check_means_no_commit_and_an_abandon(w, name):
    FAILURES[name](w)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)

    row, state = sub_row(w.a), read_state(w.a.merchant)
    assert cancels(w) == []  # the old subscription is never scheduled to cancel
    assert row.replacement_committed_at is None
    assert (row.retired_provider_ref, row.retired_kind) == (REPL_REF, "ABANDONED_REPLACEMENT")
    assert row.replacement_provider_ref is None and row.pending_plan_id is None
    assert (state.status, state.period, row.plan_id) == (before.status, before.period, w.big.pk)
    assert state.audits == [ABANDONED]


def test_an_expired_expiry_means_no_commit(w, settings):
    """Expiry equals the stored value but is already past: not committed, abandoned."""
    past = int((dj_timezone.now() - timedelta(minutes=1)).timestamp())
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(replacement_expires_at=datetime.fromtimestamp(
            past, tz=timezone.utc))
    _mutate(w, **{EXPIRY_KEY: past})
    sync_replacement(w.a)
    assert cancels(w) == [] and sub_row(w.a).retired_provider_ref == REPL_REF


def test_past_the_commit_cutoff_means_no_commit(w, settings):
    remaining = int((END - dj_timezone.now()).total_seconds())
    settings.BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN = remaining + 60  # the cutoff has passed
    sync_replacement(w.a)
    assert cancels(w) == [] and sub_row(w.a).retired_provider_ref == REPL_REF
    assert read_state(w.a.merchant).audits == [ABANDONED]


@pytest.mark.parametrize("which", ["start", "expiry", "both"])
def test_until_the_g2_fields_are_recorded_nothing_commits(w, monkeypatch, which):
    """The shipped state of the gate (None) fails closed: no commit, no cancel."""
    if which in ("start", "both"):
        monkeypatch.setattr(razorpay, "DOWNGRADE_START_FIELD", None)
    if which in ("expiry", "both"):
        monkeypatch.setattr(razorpay, "DOWNGRADE_EXPIRY_FIELD", None)
    sync_replacement(w.a)
    assert cancels(w) == [] and sub_row(w.a).replacement_committed_at is None
    assert sub_row(w.a).retired_provider_ref == REPL_REF


def test_an_unset_margin_cannot_verify_and_commits_nothing(w, settings):
    settings.BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN = None
    sync_replacement(w.a)
    assert cancels(w) == [] and sub_row(w.a).replacement_committed_at is None


def test_the_shipped_defaults_commit_nothing(lifecycle_setup):
    """No monkeypatch: both gate constants are None as shipped."""
    w = lifecycle_setup
    assert (razorpay.DOWNGRADE_START_FIELD, razorpay.DOWNGRADE_EXPIRY_FIELD) == (None, None)
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, downgrade_to=w.small)
    provide_replacement(w.provider, w.small, status="authenticated", paid=False, start=END)
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="active", start=START)
    sync_replacement(w.a)
    assert cancels(w) == [] and sub_row(w.a).replacement_committed_at is None


# --- states that are not this step's to decide ------------------------------------------------------


def test_an_unreadable_replacement_at_the_locked_reread_changes_nothing(w, monkeypatch):
    reads = []
    real = w.provider.fetch_subscription

    def second_read_fails(ref):
        if ref == REPL_REF:
            reads.append(ref)
            if len(reads) > 1:
                raise BillingProviderUnavailable()
        return real(ref)

    monkeypatch.setattr(razorpay, "fetch_subscription", second_read_fails)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before and cancels(w) == []


@pytest.mark.parametrize("status", ["created", "halted", "active"])
def test_a_replacement_that_is_no_longer_authenticated_at_the_reread_is_not_committed(w, monkeypatch, status):
    """The sync read `authenticated`; the locked re-read says otherwise. Not this
    step's state: no commit, no old cancel, and no abandon from here."""
    reads = []
    real = w.provider.fetch_subscription

    def flips(ref):
        entity_ = real(ref)
        if ref == REPL_REF:
            reads.append(ref)
            if len(reads) > 1:
                return {**entity_, "status": status}
        return entity_

    monkeypatch.setattr(razorpay, "fetch_subscription", flips)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before and cancels(w) == []


def test_an_upgrade_replacement_is_never_committed(w):
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(plan=w.small, replacement_plan=w.big, pending_plan=None)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START)
    w.provider.entities[REPL_REF]["plan_id"] = w.big.provider_plan_id
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before and cancels(w) == []


@pytest.mark.parametrize("status", ["PAST_DUE", "CANCELLED", "EXPIRED"])
def test_only_an_active_row_commits(w, status):
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(
            status=status,
            **({"past_due_at": dj_timezone.now(), "dunning_stage": 0} if status == "PAST_DUE" else {}),
        )
    with tenant_context(w.a.merchant.id):
        assert services.commit_downgrade_replacement() is False
    assert cancels(w) == [] and sub_row(w.a).replacement_committed_at is None


def test_no_replacement_and_no_subscription_are_no_ops(w):
    with tenant_context(w.b.merchant.id):
        assert services.commit_downgrade_replacement() is False  # B has no subscription
    w.sub(w.b, "ACTIVE")
    with tenant_context(w.b.merchant.id):
        assert services.commit_downgrade_replacement() is False
    assert cancels(w) == []


# --- T2: the outcome of the old cancel ------------------------------------------------------------------


def test_a_definite_refusal_clears_the_intent_and_abandons_leaving_the_old_plan(w):
    w.provider.cancel_errors[w.ref(w.a)] = BillingProviderRejected("BAD_REQUEST_ERROR", status=400)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)

    row, state = sub_row(w.a), read_state(w.a.merchant)
    assert row.replacement_committed_at is None and row.replacement_cancel_confirmed_at is None
    assert (row.retired_provider_ref, row.retired_kind) == (REPL_REF, "ABANDONED_REPLACEMENT")
    assert row.replacement_provider_ref is None and row.pending_plan_id is None
    assert (state.status, state.period, row.plan_id) == (before.status, before.period, w.big.pk)
    assert state.audits == [COMMITTED, ABANDONED]
    assert audit_rows(w, ABANDONED)[0].metadata_json["reason"] == "cancel_refused"
    assert len(cancels(w)) == 1


def test_a_refusal_never_abandons_a_replacement_that_has_become_active(w, monkeypatch):
    w.provider.cancel_errors[w.ref(w.a)] = BillingProviderRejected("BAD_REQUEST_ERROR", status=400)
    reads = []
    real = w.provider.fetch_subscription

    def active_from_the_third_read(ref):
        entity_ = real(ref)
        if ref == REPL_REF:
            reads.append(ref)
            if len(reads) >= 3:
                return {**entity_, "status": "active"}
        return entity_

    monkeypatch.setattr(razorpay, "fetch_subscription", active_from_the_third_read)
    sync_replacement(w.a)
    row = sub_row(w.a)
    assert row.replacement_provider_ref == REPL_REF and row.replacement_committed_at is None
    assert row.retired_provider_ref is None


def test_an_unavailable_provider_is_an_unknown_outcome_treated_as_committed(w):
    w.provider.cancel_errors[w.ref(w.a)] = BillingProviderUnavailable()
    sync_replacement(w.a)
    row = sub_row(w.a)
    assert row.replacement_committed_at is not None  # the intent stands
    assert row.replacement_cancel_confirmed_at is None  # but is unconfirmed
    assert row.replacement_provider_ref == REPL_REF and row.retired_provider_ref is None  # kept
    assert read_state(w.a.merchant).audits == [COMMITTED]


def test_the_sweep_reissues_an_unconfirmed_cancel_until_it_is_confirmed(w):
    w.provider.cancel_errors[w.ref(w.a)] = BillingProviderUnavailable()
    sync_replacement(w.a)
    sync_replacement(w.a)  # still failing: re-issued, still unconfirmed
    assert len(cancels(w)) == 2 and sub_row(w.a).replacement_cancel_confirmed_at is None

    del w.provider.cancel_errors[w.ref(w.a)]
    sync_replacement(w.a)
    row = sub_row(w.a)
    assert row.replacement_cancel_confirmed_at is not None and row.replacement_committed_at is not None
    assert len(cancels(w)) == 3 and all(c == ("cancel_subscription", w.ref(w.a), True) for c in cancels(w))
    sync_replacement(w.a)
    assert len(cancels(w)) == 3  # confirmed: nothing more is owed
    assert read_state(w.a.merchant).audits == [COMMITTED]  # still one commit


def test_a_rejection_on_a_reissue_never_abandons(w):
    w.provider.cancel_errors[w.ref(w.a)] = BillingProviderUnavailable()
    sync_replacement(w.a)
    w.provider.cancel_errors[w.ref(w.a)] = BillingProviderRejected("BAD_REQUEST_ERROR", status=400)
    sync_replacement(w.a)
    row = sub_row(w.a)
    assert row.replacement_provider_ref == REPL_REF and row.replacement_committed_at is not None
    assert row.replacement_cancel_confirmed_at is None and row.retired_provider_ref is None
    assert read_state(w.a.merchant).audits == [COMMITTED]


def test_a_crash_after_t1_leaves_the_intent_and_the_sweep_finishes_it(w):
    def die(ref, at_cycle_end):
        raise Crash()

    original = w.provider.cancel_subscription
    razorpay.cancel_subscription = die  # the provider fixture's monkeypatch restores this
    with pytest.raises(Crash):
        sync_replacement(w.a)
    razorpay.cancel_subscription = original
    row = sub_row(w.a)
    assert row.replacement_committed_at is not None and row.replacement_cancel_confirmed_at is None
    sync_replacement(w.a)
    assert sub_row(w.a).replacement_cancel_confirmed_at is not None
    assert len(cancels(w)) == 1  # the crashed call never reached the provider


def test_the_reissue_does_not_run_for_a_replacement_that_is_not_authenticated(w):
    """A committed replacement in a state this step does not know is left alone."""
    w.provider.cancel_errors[w.ref(w.a)] = BillingProviderUnavailable()
    sync_replacement(w.a)
    calls = len(cancels(w))
    provide_replacement(w.provider, w.small, status="paused", paid=False, start=END)
    sync_replacement(w.a)
    assert len(cancels(w)) == calls and sub_row(w.a).replacement_provider_ref == REPL_REF


# --- isolation and hygiene ----------------------------------------------------------------------------------


def test_a_commit_never_touches_another_merchant(w):
    w.sub(w.b, "ACTIVE", plan=w.big)
    b_before = read_state(w.b.merchant)
    sync_replacement(w.a)
    assert read_state(w.b.merchant) == b_before
    assert all(c[1] == w.ref(w.a) for c in cancels(w))


def test_nothing_is_logged_or_audited_with_a_ref(w, caplog):
    import logging

    w.provider.cancel_errors[w.ref(w.a)] = BillingProviderUnavailable()
    with caplog.at_level(logging.DEBUG):
        sync_replacement(w.a)
        sync_replacement(w.a)
    text = caplog.text + str([a.metadata_json for a in audit_rows(w, COMMITTED)])
    assert REPL_REF not in text and w.ref(w.a) not in text


# --- a committed downgrade whose first charge fails (review point 1) ---------------------------------------


def committed_and_confirmed(w):
    """A is committed to the downgrade and the old cancel was confirmed."""
    sync_replacement(w.a)
    assert sub_row(w.a).replacement_cancel_confirmed_at is not None


@pytest.mark.parametrize("status", ["pending", "halted", "cancelled", "expired"])
def test_a_failed_committed_replacement_lets_the_old_cancelled_status_end_the_row(w, status):
    """Spec: after the commit point the downgrade cannot be abandoned to keep the old
    plan; if the replacement's first charge fails, the old subscription's `cancelled`
    applies, the merchant ends at the old period end and checks out again, and nothing
    is granted. Without clearing the failed committed replacement, rule 5 would defer
    the old `cancelled` forever."""
    committed_and_confirmed(w)
    provide_replacement(w.provider, w.small, status=status, paid=False, start=END)
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="cancelled", start=START)
    before = read_state(w.a.merchant)

    with tenant_context(w.a.merchant.id):
        services.sync_subscription()  # replacement step, then main step

    row, state = sub_row(w.a), read_state(w.a.merchant)
    assert row.status == "CANCELLED"  # the old `cancelled` applied
    assert (row.replacement_provider_ref, row.replacement_plan_id, row.replacement_committed_at) == (None, None, None)
    assert row.replacement_cancel_confirmed_at is None and row.pending_plan_id is None
    assert row.plan_id == w.big.pk  # nothing was switched or granted
    assert (state.period, state.payments, state.usages) == (before.period, before.payments, before.usages)
    assert state.audits == [COMMITTED, ABANDONED, "billing.subscription_cancelled"]
    assert audit_rows(w, ABANDONED)[0].metadata_json["reason"] == "committed_replacement_failed"
    assert not any(c[1] == REPL_REF for c in cancels(w))  # nothing cancels the failed replacement here
    # and the merchant can check out again on the ended row
    with tenant_context(w.a.merchant.id), tenant_atomic():
        result = services.start_checkout(actor=w.a, plan_id=w.small.id)
    assert result.created is True and sub_row(w.a).status == "INCOMPLETE"


def test_a_failed_committed_replacement_with_the_old_still_active_leaves_the_old_cancel_to_end_it(w):
    committed_and_confirmed(w)
    provide_replacement(w.provider, w.small, status="halted", paid=False, start=END)
    with tenant_context(w.a.merchant.id):
        services.sync_subscription()
    row = sub_row(w.a)
    assert row.status == "ACTIVE" and row.replacement_provider_ref is None  # nothing is granted or extended
    assert (row.retired_provider_ref, row.retired_kind) == (REPL_REF, "ABANDONED_REPLACEMENT")


@pytest.mark.parametrize("status", ["created", "authenticated", "active", "paused"])
def test_a_committed_replacement_that_has_not_failed_is_left_alone(w, status):
    committed_and_confirmed(w)
    provide_replacement(w.provider, w.small, status=status, paid=False, start=END)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


def test_a_stale_failed_read_cannot_clear_a_committed_replacement_that_recovered(w, monkeypatch):
    committed_and_confirmed(w)
    provide_replacement(w.provider, w.small, status="halted", paid=False, start=END)
    reads = []
    real = w.provider.fetch_subscription

    def recovers(ref):
        entity_ = real(ref)
        if ref == REPL_REF:
            reads.append(ref)
            if len(reads) > 1:
                return {**entity_, "status": "authenticated"}
        return entity_

    monkeypatch.setattr(razorpay, "fetch_subscription", recovers)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


def test_an_unreadable_failed_committed_replacement_is_left_as_it_is(w, monkeypatch):
    committed_and_confirmed(w)
    provide_replacement(w.provider, w.small, status="halted", paid=False, start=END)
    reads = []
    real = w.provider.fetch_subscription

    def second_read_fails(ref):
        if ref == REPL_REF:
            reads.append(ref)
            if len(reads) > 1:
                raise BillingProviderUnavailable()
        return real(ref)

    monkeypatch.setattr(razorpay, "fetch_subscription", second_read_fails)
    before = read_state(w.a.merchant)
    sync_replacement(w.a)
    assert read_state(w.a.merchant) == before


# --- the re-issued cancel targets the OLD subscription whatever its status (review point 2) -------------------


@pytest.mark.parametrize("old_status", ["active", "halted", "pending", "created", "authenticated"])
def test_the_reissue_cancels_the_old_ref_at_cycle_end_in_any_state_and_never_the_replacement(w, old_status):
    """The abandoned-replacement rule (created/authenticated only, never active) must
    not leak onto this: the committed downgrade's old subscription is the one being
    ended, `active` included, and the replacement is never cancelled here."""
    w.provider.cancel_errors[w.ref(w.a)] = BillingProviderUnavailable()
    sync_replacement(w.a)  # T1 committed, outcome unknown
    del w.provider.cancel_errors[w.ref(w.a)]
    w.provider.entities[w.ref(w.a)] = entity(w.big, status=old_status, start=START)
    w.provider.calls.clear()

    sync_replacement(w.a)

    assert cancels(w) == [("cancel_subscription", w.ref(w.a), True)]  # the OLD ref, at cycle end
    row = sub_row(w.a)
    assert row.replacement_cancel_confirmed_at is not None
    assert row.replacement_provider_ref == REPL_REF and row.retired_provider_ref is None
