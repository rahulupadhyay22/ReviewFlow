"""Reconciliation, lifecycle, dunning checkpoints, payment ledger and the
paid-entitlement rule (spec 07 Definition of done: "Lifecycle", "Dunning
checkpoints", "Payment ledger", "Paid-entitlement rule").

Razorpay is mocked at the billing.razorpay module boundary: no network.
Developer checks for W4; the formal feature-test pass is /test-feature.

Not covered here on purpose: the real billing.razorpay.fetch_invoices, which
is an explicit stop marker until invoice-list pagination is decided. Every
test that needs invoices patches it."""
import logging
from datetime import timedelta

import pytest
from django.utils import timezone as dj_timezone

from auditlog.models import AuditLog
from billing import razorpay, services
from billing.exceptions import BillingProviderRejected, BillingProviderUnavailable
from billing.models import BillingEvent, PaymentAttempt, Subscription, UsageRecord
from billing.services import current_period_paid
from billing.tests.snapshot_helpers import END, NOW, REF, START, entity, invoice, ts
from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def rzp(monkeypatch):
    """Fake provider: records every call; queue results/exceptions on it."""

    class Fake:
        entity = None
        invoices = []
        fetch_error = None
        invoices_error = None
        cancel_error = None
        cancel_result = {"status": "cancelled"}
        calls = []

    fake = Fake()
    fake.calls = []

    def fetch_subscription(ref):
        fake.calls.append(("fetch_subscription", ref))
        if fake.fetch_error:
            raise fake.fetch_error
        return fake.entity

    def fetch_invoices(ref):
        fake.calls.append(("fetch_invoices", ref))
        if fake.invoices_error:
            raise fake.invoices_error
        return fake.invoices

    def cancel_subscription(ref, at_cycle_end):
        fake.calls.append(("cancel_subscription", ref, at_cycle_end))
        if fake.cancel_error:
            raise fake.cancel_error
        return fake.cancel_result

    monkeypatch.setattr(razorpay, "fetch_subscription", fetch_subscription)
    monkeypatch.setattr(razorpay, "fetch_invoices", fetch_invoices)
    monkeypatch.setattr(razorpay, "cancel_subscription", cancel_subscription)
    return fake


@pytest.fixture
def setup(make_merchant, make_plan, make_subscription):
    """One merchant, a small and a big plan, and a subscription builder."""
    owner = make_merchant("A")
    small = make_plan("Starter", quota=100, provider_plan_id="plan_small")
    big = make_plan("Growth", quota=500, price="1999.00", provider_plan_id="plan_big")

    class S:
        pass

    s = S()
    s.owner, s.small, s.big = owner, small, big

    def sub(status="ACTIVE", plan=small, **kwargs):
        kwargs.setdefault("ref", REF)
        if status != "INCOMPLETE":
            kwargs.setdefault("current_period_start", START)
            kwargs.setdefault("current_period_end", END)
        if status == "PAST_DUE":
            kwargs.setdefault("past_due_at", NOW)
            kwargs.setdefault("dunning_stage", 0)
            kwargs.setdefault("provider_status", "halted")
        return make_subscription(owner.merchant, plan, status=status, **kwargs)

    s.sub = sub
    return s


def apply(setup, sub, ent, invoices=None, started=None, actor=None):
    with tenant_context(setup.owner.merchant.id):
        return services._apply_snapshot(
            sub, ent, invoices, started or dj_timezone.now(), actor=actor
        )


def reload(setup):
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        return Subscription.objects.select_related("plan", "pending_plan").get()


def usages(setup):
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        return [(u.period_start, u.requests_used) for u in UsageRecord.objects.order_by("period_start")]


def audits(setup, action=None):
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        qs = AuditLog.objects.filter(action__startswith="billing.")
        if action:
            qs = qs.filter(action=action)
        return list(qs.order_by("created_at"))


def payments(setup):
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        return list(PaymentAttempt.objects.order_by("created_at"))


# --- current_period_paid --------------------------------------------------


def test_current_period_paid_returns_the_qualifying_invoice(setup):
    ent = entity(setup.small)
    inv = invoice()
    assert current_period_paid(ent, [inv]) is inv


@pytest.mark.parametrize(
    "override",
    [
        {"status": "issued"},
        {"status": "partially_paid"},
        {"status": "cancelled"},
        {"amount_due": 100},
        {"amount_due": None},
        {"amount_due": True},
        {"payment_id": None},
        {"payment_id": ""},
        {"subscription_id": "sub_other"},
        {"billing_start": ts(START + timedelta(days=30)), "billing_end": ts(START + timedelta(days=60))},
        {"billing_start": ts(START - timedelta(days=30)), "billing_end": ts(START)},  # end is exclusive
        {"billing_start": None},
    ],
)
def test_current_period_paid_fails_on_each_failing_condition(setup, override):
    assert current_period_paid(entity(setup.small), [invoice(**override)]) is None


def test_current_period_paid_empty_or_missing_invoices(setup):
    assert current_period_paid(entity(setup.small), []) is None
    assert current_period_paid(entity(setup.small), None) is None
    assert current_period_paid({"id": REF}, [invoice()]) is None  # no current_start


def test_current_period_paid_accepts_the_window_start_and_skips_non_qualifying_first(setup):
    paid = invoice(id="inv_2", payment_id="pay_2", billing_start=ts(START), billing_end=ts(END))
    assert current_period_paid(entity(setup.small), [invoice(status="issued"), paid]) is paid


# --- mapping table: statuses that never change anything ---------------------


ALL = ["INCOMPLETE", "ACTIVE", "PAST_DUE", "CANCELLED", "EXPIRED"]


@pytest.mark.parametrize("local", ALL)
@pytest.mark.parametrize("pstatus", ["created", "authenticated", "paused", "some_future_status"])
def test_created_authenticated_paused_and_unknown_change_nothing(setup, local, pstatus):
    sub = setup.sub(local)
    result = apply(setup, sub, entity(setup.small, status=pstatus))
    after = reload(setup)
    assert result.changed is False
    assert after.status == local
    assert after.provider_status == pstatus[:16]  # stored on every apply (column is 16 chars)
    assert audits(setup) == []


# --- mapping table: active -------------------------------------------------


@pytest.mark.parametrize("local", ["INCOMPLETE", "PAST_DUE", "CANCELLED", "EXPIRED"])
def test_active_with_a_paid_period_moves_every_non_active_state_to_active(setup, local):
    sub = setup.sub(local)
    new_start = NOW - timedelta(days=1)
    result = apply(setup, sub, entity(setup.small, start=new_start), [invoice(start=new_start)])
    after = reload(setup)
    assert result.changed is True and result.settled is True
    assert after.status == "ACTIVE"
    assert after.current_period_start == new_start
    assert (after.past_due_at, after.dunning_stage) == (None, None)
    assert (new_start, 0) in usages(setup)
    expected = "billing.subscription_recovered" if local == "PAST_DUE" else "billing.subscription_activated"
    assert [a.action for a in audits(setup)] == [expected]


@pytest.mark.parametrize("local", ["INCOMPLETE", "PAST_DUE", "CANCELLED", "EXPIRED"])
def test_active_without_a_proven_paid_period_changes_nothing_and_stays_unprocessed(setup, local):
    sub = setup.sub(local)
    before_usage = usages(setup)
    for invs in (None, [], [invoice(status="issued")]):
        result = apply(setup, sub, entity(setup.small), invs)
        assert result.changed is False and result.settled is False
    after = reload(setup)
    assert after.status == local
    assert usages(setup) == before_usage
    assert audits(setup) == []


def test_active_same_period_refreshes_the_plan_without_a_paid_proof(setup):
    sub = setup.sub("ACTIVE")
    result = apply(setup, sub, entity(setup.big, start=START, end=END), None)
    after = reload(setup)
    assert result.changed is True
    assert after.plan_id == setup.big.pk
    assert [u[1] for u in usages(setup)] == [0]  # no new UsageRecord
    [row] = audits(setup, "billing.plan_changed")
    assert row.metadata_json["effective"] == "IMMEDIATE"


def test_active_new_period_advances_only_with_a_paid_invoice_and_opens_a_usage_record(setup):
    sub = setup.sub("ACTIVE")
    new_start = END
    ent = entity(setup.small, start=new_start)

    unpaid = apply(setup, sub, ent, None)
    after = reload(setup)
    assert (unpaid.changed, unpaid.settled) == (False, False)
    assert after.current_period_start == START  # did not advance
    assert len(usages(setup)) == 1  # no UsageRecord

    paid = apply(setup, sub, ent, [invoice(start=new_start)])
    after = reload(setup)
    assert paid.changed is True
    assert after.current_period_start == new_start
    assert len(usages(setup)) == 2
    assert audits(setup) == []  # a routine period advance writes no audit row


def test_a_period_advance_leaves_the_old_usage_record_untouched_no_rollover(setup):
    sub = setup.sub("ACTIVE")
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        UsageRecord.objects.update(requests_used=42)
    apply(setup, sub, entity(setup.small, start=END), [invoice(start=END)])
    assert usages(setup) == [(START, 42), (END, 0)]


def test_a_scheduled_downgrade_is_applied_at_the_next_period_and_pending_plan_cleared(setup):
    sub = setup.sub("ACTIVE", plan=setup.big, pending_plan=setup.small)
    apply(setup, sub, entity(setup.small, start=END), [invoice(start=END)])
    after = reload(setup)
    assert after.plan_id == setup.small.pk and after.pending_plan_id is None
    [row] = audits(setup, "billing.plan_changed")
    assert row.metadata_json["effective"] == "APPLIED"


def test_an_owner_upgrade_audit_row_names_the_owner(setup):
    sub = setup.sub("ACTIVE")
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        from accounts.models import TeamMember

        owner_member = TeamMember.objects.get(user=setup.owner.user)
    apply(setup, sub, entity(setup.big, start=START, end=END), None, actor=owner_member)
    [row] = audits(setup, "billing.plan_changed")
    assert row.actor_user_id == setup.owner.user_id


def test_an_unknown_provider_plan_changes_nothing_and_leaves_events_unprocessed(setup):
    sub = setup.sub("PAST_DUE")
    result = apply(setup, sub, entity(setup.small, plan_id="plan_nobody_knows"), [invoice()])
    after = reload(setup)
    assert (result.changed, result.settled) == (False, False)
    assert after.status == "PAST_DUE" and after.provider_status == "halted"
    assert audits(setup) == []


# --- mapping table: pending / halted ----------------------------------------


@pytest.mark.parametrize("pstatus", ["pending", "halted"])
def test_pending_and_halted_move_active_to_past_due_with_stage_zero(setup, pstatus):
    sub = setup.sub("ACTIVE")
    result = apply(setup, sub, entity(setup.small, status=pstatus))
    after = reload(setup)
    assert result.changed is True
    assert after.status == "PAST_DUE"
    assert after.past_due_at is not None and after.dunning_stage == 0
    assert [a.action for a in audits(setup)] == ["billing.subscription_past_due"]
    assert payments(setup) == []  # the transition writes no PaymentAttempt


@pytest.mark.parametrize("local", ["INCOMPLETE", "PAST_DUE", "CANCELLED", "EXPIRED"])
@pytest.mark.parametrize("pstatus", ["pending", "halted"])
def test_pending_and_halted_change_no_other_state(setup, local, pstatus):
    sub = setup.sub(local)
    before = reload(setup)
    apply(setup, sub, entity(setup.small, status=pstatus))
    after = reload(setup)
    assert after.status == local
    assert after.past_due_at == before.past_due_at  # the grace clock keeps running
    assert audits(setup) == []


def test_halted_never_produces_expired_on_a_past_due_row(setup):
    sub = setup.sub("PAST_DUE", past_due_at=NOW - timedelta(days=30))  # long overdue
    apply(setup, sub, entity(setup.small, status="halted"))
    assert reload(setup).status == "PAST_DUE"  # only ReviewFlow's own 7-day clock expires it


# --- mapping table: cancelled / completed / expired ------------------------------


@pytest.mark.parametrize("pstatus", ["cancelled", "completed", "expired"])
def test_provider_terminal_states_cancel_an_active_row(setup, pstatus):
    sub = setup.sub("ACTIVE")
    apply(setup, sub, entity(setup.small, status=pstatus))
    assert reload(setup).status == "CANCELLED"
    assert [a.action for a in audits(setup)] == ["billing.subscription_cancelled"]


@pytest.mark.parametrize("pstatus", ["cancelled", "completed", "expired"])
def test_provider_terminal_state_inside_grace_cancels_past_due_and_clears_the_episode(setup, pstatus):
    sub = setup.sub("PAST_DUE", past_due_at=dj_timezone.now() - timedelta(days=2))
    apply(setup, sub, entity(setup.small, status=pstatus))
    after = reload(setup)
    assert after.status == "CANCELLED"
    assert (after.past_due_at, after.dunning_stage) == (None, None)


def test_provider_terminal_state_after_grace_expires_past_due(setup):
    sub = setup.sub("PAST_DUE", past_due_at=dj_timezone.now() - timedelta(days=8))
    apply(setup, sub, entity(setup.small, status="cancelled"))
    assert reload(setup).status == "EXPIRED"
    assert [a.action for a in audits(setup)] == ["billing.subscription_expired"]


@pytest.mark.parametrize("local", ["INCOMPLETE", "CANCELLED", "EXPIRED"])
def test_provider_terminal_state_changes_no_settled_row(setup, local):
    sub = setup.sub(local)
    apply(setup, sub, entity(setup.small, status="cancelled"))
    assert reload(setup).status == local
    assert audits(setup) == []


# --- lapsed period without a webhook ---------------------------------------


def test_a_lapsed_active_row_stays_active_not_past_due_and_is_not_entitled(setup):
    lapsed_start = NOW - timedelta(days=31)
    lapsed_end = NOW - timedelta(days=1)
    sub = setup.sub("ACTIVE", current_period_start=lapsed_start, current_period_end=lapsed_end)
    # the provider still says active on the old cycle, and no invoice is proven
    result = apply(setup, sub, entity(setup.small, start=lapsed_start, end=lapsed_end), None)
    after = reload(setup)
    assert result.changed is False
    assert after.status == "ACTIVE"
    with tenant_context(setup.owner.merchant.id):
        assert services.get_entitlement().can_send is False


# --- stale-apply guard ---------------------------------------------------------


def test_a_stale_fetch_or_a_replaced_ref_changes_nothing(setup):
    sub = setup.sub("ACTIVE")
    newer = dj_timezone.now()
    apply(setup, sub, entity(setup.small, status="halted"), started=newer)
    assert reload(setup).status == "PAST_DUE"

    # an older fetch that says "active" must not undo the newer one
    stale = apply(setup, sub, entity(setup.small, start=END), [invoice(start=END)], started=newer - timedelta(seconds=5))
    assert (stale.changed, stale.settled) == (False, False)
    assert reload(setup).status == "PAST_DUE"

    other_ref = apply(setup, sub, entity(setup.small, ref="sub_replaced"), [invoice()], started=newer + timedelta(seconds=5))
    assert (other_ref.changed, other_ref.settled) == (False, False)
    assert reload(setup).status == "PAST_DUE"


def test_an_equal_fetch_time_is_stale(setup):
    sub = setup.sub("ACTIVE")
    t = dj_timezone.now()
    apply(setup, sub, entity(setup.small, status="created"), started=t)
    again = apply(setup, sub, entity(setup.small, status="halted"), started=t)
    assert again.changed is False and reload(setup).status == "ACTIVE"


def test_a_snapshot_for_another_merchants_subscription_is_refused(setup, make_merchant, make_plan, make_subscription):
    other = make_merchant("B")
    other_sub = make_subscription(other.merchant, make_plan("Pro"), ref="sub_b")
    with tenant_context(setup.owner.merchant.id):
        with pytest.raises(TenantContextError):
            services._apply_snapshot(other_sub, entity(setup.small, ref="sub_b"), None, dj_timezone.now())


# --- payment ledger --------------------------------------------------------------


@pytest.mark.parametrize("local", ["INCOMPLETE", "CANCELLED", "EXPIRED"])
def test_a_stale_cancellation_flag_does_not_survive_a_paid_reactivation(setup, local):
    """Decision 2 (2026-09-30). The 409 subscription_cancelling response and
    the CANCELLATION next_action read this flag on an ACTIVE row; their
    response-level tests belong to W5, which is not started."""
    sub = setup.sub(local, cancel_at_period_end=True)
    new_start = NOW - timedelta(days=1)
    apply(setup, sub, entity(setup.small, start=new_start), [invoice(start=new_start)])
    after = reload(setup)
    assert after.status == "ACTIVE"
    assert after.cancel_at_period_end is False


def test_an_unpaid_active_snapshot_does_not_reset_the_flag(setup):
    sub = setup.sub("CANCELLED", cancel_at_period_end=True)
    apply(setup, sub, entity(setup.small, start=NOW), [invoice(start=NOW, status="issued")])
    after = reload(setup)
    assert after.status == "CANCELLED" and after.cancel_at_period_end is True


def test_an_active_row_awaiting_its_period_end_cancellation_keeps_the_flag_through_a_sync(setup, rzp):
    setup.sub("ACTIVE", cancel_at_period_end=True)
    rzp.entity = entity(setup.small, start=START, end=END)
    sync(setup)
    after = reload(setup)
    assert after.status == "ACTIVE" and after.cancel_at_period_end is True


def test_the_paid_invoice_is_recorded_as_a_renewal_payment(setup):
    sub = setup.sub("INCOMPLETE")
    apply(setup, sub, entity(setup.small), [invoice(payment_id="pay_first")])
    [row] = payments(setup)
    assert (row.provider_attempt_id, row.attempt_type, row.status) == ("pay_first", "RENEWAL", "SUCCEEDED")
    assert row.provider == "razorpay"


def test_attempted_at_is_the_qualifying_invoices_paid_at(setup):
    """Signed off 2026-09-30: attempted_at = invoice.paid_at, never
    ReviewFlow's clock and never a created_at."""
    sub = setup.sub("INCOMPLETE")
    paid_at = ts(START) + 7200
    decoy = ts(START) + 99999  # other timestamps on the invoice must be ignored
    apply(
        setup,
        sub,
        entity(setup.small),
        [invoice(payment_id="pay_time", paid_at=paid_at, created_at=decoy, issued_at=decoy)],
    )
    [row] = payments(setup)
    assert row.attempted_at.timestamp() == paid_at
    assert row.attempted_at < dj_timezone.now() - timedelta(days=1)  # not the recording time


@pytest.mark.parametrize("paid_at", [None, "missing", "1790000000", 1.5, True, -(10**30), 10**30])
def test_an_invoice_without_a_usable_paid_at_still_reconciles_but_records_no_payment(setup, paid_at, caplog):
    """paid_at is not a condition of current_period_paid(): the invoice still
    qualifies and the subscription activates. Only the ledger row fails
    closed, and no other timestamp is substituted."""
    sub = setup.sub("INCOMPLETE")
    inv = invoice(payment_id="pay_nopaidat", created_at=ts(START) + 5, issued_at=ts(START) + 6)
    if paid_at == "missing":
        del inv["paid_at"]
    else:
        inv["paid_at"] = paid_at
    assert current_period_paid(entity(setup.small), [inv]) is inv  # still qualifies

    with caplog.at_level("WARNING", logger="billing.services"):
        result = apply(setup, sub, entity(setup.small), [inv])
    assert result.changed is True and result.settled is True
    assert reload(setup).status == "ACTIVE"
    assert len(usages(setup)) == 1  # the UsageRecord is still opened
    assert payments(setup) == []  # nothing written, nothing substituted
    assert "no usable paid_at" in caplog.text
    assert "pay_nopaidat" not in caplog.text  # ids only, never a payment id


def test_a_missing_paid_at_makes_no_provider_call(setup, rzp):
    setup.sub("INCOMPLETE")
    inv = invoice(payment_id="pay_x")
    del inv["paid_at"]
    rzp.entity, rzp.invoices = entity(setup.small), [inv]
    sync(setup)
    assert reload(setup).status == "ACTIVE"
    assert [c[0] for c in rzp.calls] == ["fetch_subscription", "fetch_invoices"]  # no payment fetch
    assert payments(setup) == []


def test_a_payment_recorded_while_past_due_is_a_retry(setup):
    sub = setup.sub("PAST_DUE")
    apply(setup, sub, entity(setup.small, start=NOW), [invoice(start=NOW, payment_id="pay_late")])
    [row] = payments(setup)
    assert row.attempt_type == "RETRY" and row.status == "SUCCEEDED"


def test_the_same_payment_reported_twice_leaves_one_row(setup):
    sub = setup.sub("INCOMPLETE")
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        # as if the charged webhook had already recorded it
        PaymentAttempt.objects.create(
            merchant=setup.owner.merchant,
            subscription=Subscription.objects.get(),
            provider="razorpay",
            provider_attempt_id="pay_1",
            attempt_type="RENEWAL",
            status="SUCCEEDED",
            attempted_at=dj_timezone.now(),
        )
    apply(setup, sub, entity(setup.small), [invoice(payment_id="pay_1")])
    assert reload(setup).status == "ACTIVE"
    assert len(payments(setup)) == 1


def test_a_sync_creates_the_ledger_row_when_the_charged_webhook_never_arrived(setup):
    sub = setup.sub("INCOMPLETE")
    apply(setup, sub, entity(setup.small), [invoice(payment_id="pay_repaired")])
    assert [p.provider_attempt_id for p in payments(setup)] == ["pay_repaired"]


def test_an_unpaid_active_period_records_no_payment(setup):
    sub = setup.sub("PAST_DUE")
    apply(setup, sub, entity(setup.small), [invoice(status="issued", payment_id=None)])
    assert payments(setup) == []


# --- sync_subscription -------------------------------------------------------------


def _event(setup, event_id="evt_1"):
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        return BillingEvent.objects.create(
            merchant=setup.owner.merchant,
            provider="razorpay",
            provider_event_id=event_id,
            event_type="subscription.charged",
            provider_ref=REF,
        )


def _processed(setup):
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        return {e.provider_event_id: e.processed_at is not None for e in BillingEvent.objects.all()}


def sync(setup):
    with tenant_context(setup.owner.merchant.id):
        services.sync_subscription()


def test_sync_activates_from_the_fetched_state_and_marks_events_processed(setup, rzp):
    setup.sub("INCOMPLETE")
    _event(setup)
    new_start = NOW - timedelta(days=1)
    rzp.entity = entity(setup.small, start=new_start)
    rzp.invoices = [invoice(start=new_start)]
    sync(setup)
    assert reload(setup).status == "ACTIVE"
    assert _processed(setup) == {"evt_1": True}
    assert [c[0] for c in rzp.calls] == ["fetch_subscription", "fetch_invoices"]


MARGIN_CASES = [
    ("older_than_the_margin", -6, True),
    ("inside_the_margin", -2, False),
    ("exactly_on_the_cutoff", -5, False),
    ("at_fetch_start", 0, False),
    ("received_during_the_fetch", 1, False),
]


@pytest.mark.parametrize(
    "seconds, marked", [c[1:] for c in MARGIN_CASES], ids=[c[0] for c in MARGIN_CASES]
)
def test_a_sync_marks_only_events_received_more_than_the_skew_margin_before_its_fetch(
    setup, rzp, monkeypatch, real_clock_skew, seconds, marked
):
    """The cutoff is fetch start minus the 5 s clock-skew margin, strict: an
    event exactly on it, inside the margin, at the fetch start or received
    during the fetch is left for a later sync. created_at is pinned relative
    to the fetch start the sync itself recorded, from inside the fake fetch,
    so no wall-clock timing is involved."""
    assert real_clock_skew == timedelta(seconds=5)
    setup.sub("INCOMPLETE")
    _event(setup, "evt_x")
    clock_offset = [timedelta(0)]
    seen_now = []

    def clock_now():
        # Strictly increasing: two calls never return the same instant, so a
        # sync that read the clock twice for one write would show it.
        seen_now.append(dj_timezone.now() + clock_offset[0] + timedelta(microseconds=len(seen_now)))
        return seen_now[-1]

    monkeypatch.setattr(services, "dj_timezone", type("Clock", (), {"now": staticmethod(clock_now)}))
    new_start = NOW - timedelta(days=1)
    rzp.entity = entity(setup.small, start=new_start)
    rzp.invoices = [invoice(start=new_start)]
    fetch_started = {}

    def fetch_with_event_at_offset(ref):
        fetch_started["at"] = seen_now[-1]  # the sync's own fetch_started_at
        with tenant_context(setup.owner.merchant.id), tenant_atomic():
            BillingEvent.objects.filter(provider_event_id="evt_x").update(
                created_at=fetch_started["at"] + timedelta(seconds=seconds)
            )
        rzp.calls.append(("fetch_subscription", ref))
        return rzp.entity

    monkeypatch.setattr(razorpay, "fetch_subscription", fetch_with_event_at_offset)
    sync(setup)
    assert reload(setup).status == "ACTIVE"  # the sync itself settled
    assert reload(setup).provider_synced_at == fetch_started["at"]
    assert _processed(setup) == {"evt_x": marked}
    if marked:
        with tenant_context(setup.owner.merchant.id), tenant_atomic():
            event_row = BillingEvent.objects.get(provider_event_id="evt_x")
        assert event_row.processed_at == event_row.updated_at  # one timestamp for both

    # A later sync, started more than the margin after the event, marks it.
    clock_offset[0] = timedelta(seconds=10)
    monkeypatch.setattr(razorpay, "fetch_subscription", lambda ref: rzp.entity)
    sync(setup)
    assert _processed(setup) == {"evt_x": True}


def test_only_a_settled_sync_marks_events_even_when_they_are_older_than_the_margin(
    setup, rzp, real_clock_skew
):
    setup.sub("INCOMPLETE")
    _event(setup, "evt_old")
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        BillingEvent.objects.update(created_at=dj_timezone.now() - timedelta(minutes=10))
    rzp.entity, rzp.invoices = entity(setup.small), []  # no qualifying invoice: not settled
    sync(setup)
    assert _processed(setup) == {"evt_old": False}


def test_an_unknown_provider_status_is_logged_escaped(setup, rzp, caplog):
    setup.sub("INCOMPLETE")
    _event(setup)
    rzp.entity = entity(setup.small, status="weird" + chr(10) + "WARNING forged")
    with caplog.at_level(logging.WARNING):
        sync(setup)
    messages = [r.getMessage() for r in caplog.records if r.name.startswith("billing")]
    assert any("has status" in m for m in messages)
    assert not any(chr(10) in m or chr(13) in m for m in messages)


def test_sync_keeps_events_unprocessed_when_the_period_is_not_proven_paid(setup, rzp):
    setup.sub("INCOMPLETE")
    _event(setup)
    rzp.entity, rzp.invoices = entity(setup.small), []
    sync(setup)
    assert reload(setup).status == "INCOMPLETE"
    assert _processed(setup) == {"evt_1": False}


@pytest.mark.parametrize(
    "error", [BillingProviderUnavailable(), BillingProviderRejected("X")]
)
def test_a_failed_subscription_fetch_applies_nothing_and_raises_nothing(setup, rzp, error):
    setup.sub("ACTIVE")
    _event(setup)
    rzp.fetch_error = error
    sync(setup)
    assert reload(setup).provider_synced_at is None
    assert _processed(setup) == {"evt_1": False}


def test_a_failed_invoice_fetch_applies_nothing_and_leaves_events_unprocessed(setup, rzp):
    setup.sub("INCOMPLETE")
    _event(setup)
    rzp.entity = entity(setup.small)
    rzp.invoices_error = BillingProviderUnavailable()
    sync(setup)
    after = reload(setup)
    assert after.status == "INCOMPLETE" and after.provider_synced_at is None
    assert _processed(setup) == {"evt_1": False}
    assert usages(setup) == []


def test_sync_does_not_fetch_invoices_inside_an_unchanged_active_period(setup, rzp):
    setup.sub("ACTIVE")
    rzp.entity = entity(setup.small, start=START, end=END)
    sync(setup)
    assert [c[0] for c in rzp.calls] == ["fetch_subscription"]


def test_sync_fetches_invoices_for_a_new_period_on_an_active_row(setup, rzp):
    setup.sub("ACTIVE")
    rzp.entity, rzp.invoices = entity(setup.small, start=END), [invoice(start=END)]
    sync(setup)
    assert [c[0] for c in rzp.calls] == ["fetch_subscription", "fetch_invoices"]
    assert reload(setup).current_period_start == END


@pytest.mark.parametrize(
    "local, pstatus",
    [
        ("ACTIVE", "paused"),
        ("PAST_DUE", "paused"),
        ("INCOMPLETE", "paused"),
        ("CANCELLED", "some_future_status"),
        ("EXPIRED", "paused"),
        ("INCOMPLETE", "created"),
        ("INCOMPLETE", "authenticated"),
        ("INCOMPLETE", "pending"),
        ("CANCELLED", "halted"),
        ("EXPIRED", "cancelled"),
        ("INCOMPLETE", "cancelled"),
    ],
)
def test_a_successfully_applied_no_change_snapshot_marks_events_processed(setup, rzp, local, pstatus):
    """Decision 1 (2026-09-30): "processed" means the snapshot was reconciled
    and persisted. It does not mean ACTIVE, entitled or paid."""
    setup.sub(local)
    _event(setup)
    rzp.entity = entity(setup.small, status=pstatus, start=START, end=END)
    sync(setup)
    after = reload(setup)
    assert after.status == local  # the spec's "no change" cell
    assert after.provider_status == pstatus[:16]  # stored
    assert _processed(setup) == {"evt_1": True}
    assert audits(setup) == []
    with tenant_context(setup.owner.merchant.id):
        assert services.get_entitlement().can_send is (local == "ACTIVE")  # nothing was granted


def test_a_paused_snapshot_never_grants_entitlement_even_though_its_events_are_processed(setup, rzp):
    setup.sub("EXPIRED")
    _event(setup)
    rzp.entity = entity(setup.small, status="paused")
    sync(setup)
    assert _processed(setup) == {"evt_1": True}
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        assert services.reserve_quota_unit() is services.ReservationResult.NOT_ENTITLED


def test_an_unknown_plan_leaves_events_unprocessed_at_the_sync_level(setup, rzp):
    setup.sub("ACTIVE")
    _event(setup)
    rzp.entity = entity(setup.small, plan_id="plan_nobody_knows", start=START, end=END)
    sync(setup)
    assert _processed(setup) == {"evt_1": False}
    assert reload(setup).provider_synced_at is None


def test_a_stale_fetch_leaves_events_unprocessed_at_the_sync_level(setup, rzp):
    setup.sub("ACTIVE", provider_synced_at=dj_timezone.now() + timedelta(hours=1))
    _event(setup)
    rzp.entity = entity(setup.small, start=START, end=END)
    sync(setup)
    assert _processed(setup) == {"evt_1": False}


def test_out_of_order_pending_after_charged_changes_nothing(setup, rzp):
    """A stale subscription.pending arrives after subscription.charged: the
    sync fetches the current state (active, same period) and changes nothing."""
    setup.sub("ACTIVE")
    _event(setup, "evt_charged")
    _event(setup, "evt_pending")
    rzp.entity = entity(setup.small, start=START, end=END)
    sync(setup)
    assert reload(setup).status == "ACTIVE"
    assert audits(setup) == []
    assert _processed(setup) == {"evt_charged": True, "evt_pending": True}


def test_a_sync_that_changes_nothing_writes_no_audit_row_and_twice_is_the_same(setup, rzp):
    setup.sub("ACTIVE")
    rzp.entity = entity(setup.small, start=START, end=END)
    sync(setup)
    first = reload(setup)
    sync(setup)
    second = reload(setup)
    assert audits(setup) == []
    assert (first.status, first.plan_id, first.current_period_start) == (
        second.status,
        second.plan_id,
        second.current_period_start,
    )


def test_sync_does_not_touch_updated_at_when_nothing_changed(setup, rzp):
    """maintenance_due() relies on updated_at for the INCOMPLETE window."""
    setup.sub("INCOMPLETE")
    rzp.entity = entity(setup.small, status="created")
    before = reload(setup).updated_at
    sync(setup)
    sync(setup)
    assert reload(setup).updated_at == before


def test_sync_with_no_row_or_no_ref_makes_no_provider_call(setup, rzp):
    sync(setup)
    setup.sub("INCOMPLETE", ref=None)
    sync(setup)
    assert rzp.calls == []


# --- paid-entitlement scenarios (O17) -------------------------------------------------


def test_halted_recovery_without_payment_stays_past_due_and_the_clock_keeps_running(setup, rzp):
    sub = setup.sub("PAST_DUE", past_due_at=dj_timezone.now() - timedelta(days=2), dunning_stage=0)
    before = reload(setup)
    new_start = NOW - timedelta(days=1)
    rzp.entity = entity(setup.small, start=new_start)
    rzp.invoices = [invoice(start=new_start, status="issued", payment_id=None, amount_due=99900)]
    sync(setup)
    after = reload(setup)
    assert after.status == "PAST_DUE" and after.past_due_at == before.past_due_at
    with tenant_context(setup.owner.merchant.id):
        assert services.get_entitlement().can_send is False
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        assert services.reserve_quota_unit() is services.ReservationResult.NOT_ENTITLED
    assert len(usages(setup)) == 1  # no new UsageRecord
    assert audits(setup, "billing.subscription_recovered") == []
    assert sub


def test_the_same_unpaid_row_expires_at_day_seven_and_the_provider_subscription_is_cancelled(setup, rzp):
    past_due_at = dj_timezone.now() - timedelta(days=7, minutes=1)
    setup.sub("PAST_DUE", past_due_at=past_due_at, dunning_stage=6)
    rzp.cancel_result = {"status": "cancelled"}
    with tenant_context(setup.owner.merchant.id):
        services.advance_dunning()
    after = reload(setup)
    assert after.status == "EXPIRED" and (after.past_due_at, after.dunning_stage) == (None, None)
    assert ("cancel_subscription", REF, False) in rzp.calls
    assert after.provider_status == "cancelled"
    assert [a.action for a in audits(setup)] == ["billing.subscription_expired"]


@pytest.mark.parametrize("pstatus", ["created", "pending", "halted", "active"])
@pytest.mark.parametrize("local", ["CANCELLED", "EXPIRED"])
def test_the_sweep_cancels_an_owed_provider_subscription_in_a_cancellable_state(setup, rzp, local, pstatus):
    setup.sub(local, provider_status=pstatus)
    advance(setup, NOW)
    assert ("cancel_subscription", REF, False) in rzp.calls
    assert reload(setup).provider_status == "cancelled"


@pytest.mark.parametrize(
    "pstatus",
    ["authenticated", "paused", "future_status", None, "cancelled", "completed", "expired"],
)
@pytest.mark.parametrize("local", ["CANCELLED", "EXPIRED"])
def test_the_sweep_never_touches_authenticated_paused_unknown_missing_or_terminal(setup, rzp, local, pstatus):
    """Consistency with checkout (2026-09-30): the provider is left exactly
    as it is, and the row is not changed."""
    setup.sub(local, provider_status=pstatus)
    advance(setup, NOW)
    assert rzp.calls == []
    after = reload(setup)
    assert after.status == local and after.provider_status == pstatus


def test_the_sweep_and_checkout_share_one_provider_touch_predicate():
    assert services.provider_may_be_cancelled("halted") is True
    for status in ("authenticated", "paused", "cancelled", "completed", "expired", "weird", "", None):
        assert services.provider_may_be_cancelled(status) is False


def test_a_provider_cancel_that_fails_leaves_the_row_expired_and_is_retried(setup, rzp):
    setup.sub("PAST_DUE", past_due_at=dj_timezone.now() - timedelta(days=8), dunning_stage=6)
    rzp.cancel_error = BillingProviderUnavailable()
    with tenant_context(setup.owner.merchant.id):
        services.advance_dunning()
    after = reload(setup)
    assert after.status == "EXPIRED" and after.provider_status == "halted"  # still owed

    rzp.cancel_error = BillingProviderRejected("BAD_REQUEST_ERROR")
    with tenant_context(setup.owner.merchant.id):
        services.advance_dunning()
    assert reload(setup).status == "EXPIRED"

    rzp.cancel_error = None
    with tenant_context(setup.owner.merchant.id):
        services.advance_dunning()
    assert reload(setup).provider_status == "cancelled"
    attempts = [c for c in rzp.calls if c[0] == "cancel_subscription"]
    assert len(attempts) == 3

    with tenant_context(setup.owner.merchant.id):
        services.advance_dunning()  # terminal now: nothing further is owed
    assert len([c for c in rzp.calls if c[0] == "cancel_subscription"]) == 3


def test_a_late_charge_after_expiry_converges_back_to_active(setup, rzp):
    setup.sub("EXPIRED", provider_status="active")
    new_start = NOW - timedelta(days=1)
    rzp.entity, rzp.invoices = entity(setup.small, start=new_start), [invoice(start=new_start)]
    sync(setup)
    assert reload(setup).status == "ACTIVE"
    assert [a.action for a in audits(setup)] == ["billing.subscription_activated"]


# --- dunning checkpoints (Change 3) ----------------------------------------------------


def _episode(setup, days_ago, stage=0):
    return setup.sub("PAST_DUE", past_due_at=NOW - timedelta(days=days_ago), dunning_stage=stage)


def advance(setup, at):
    with tenant_context(setup.owner.merchant.id):
        services.advance_dunning(at)


def test_checkpoints_advance_to_three_and_six_with_one_audit_row_each(setup, rzp):
    _episode(setup, 0)
    advance(setup, NOW + timedelta(days=2, hours=23))
    assert reload(setup).dunning_stage == 0 and audits(setup) == []

    advance(setup, NOW + timedelta(days=3))
    assert reload(setup).dunning_stage == 3
    advance(setup, NOW + timedelta(days=6))
    assert reload(setup).dunning_stage == 6
    rows = audits(setup, "billing.dunning_checkpoint")
    assert [r.metadata_json["stage"] for r in rows] == [3, 6]


def test_repeating_a_checkpoint_at_the_same_instant_changes_it_once(setup, rzp):
    _episode(setup, 0)
    for _ in range(4):
        advance(setup, NOW + timedelta(days=3, seconds=1))
    assert reload(setup).dunning_stage == 3
    assert len(audits(setup, "billing.dunning_checkpoint")) == 1


def test_a_sweep_that_was_down_goes_straight_to_the_highest_due_stage(setup, rzp):
    _episode(setup, 0)
    advance(setup, NOW + timedelta(days=6, hours=12))
    assert reload(setup).dunning_stage == 6
    rows = audits(setup, "billing.dunning_checkpoint")
    assert [r.metadata_json["stage"] for r in rows] == [6]


def test_a_checkpoint_never_goes_backwards(setup, rzp):
    _episode(setup, 0, stage=6)
    advance(setup, NOW + timedelta(days=3, seconds=1))
    assert reload(setup).dunning_stage == 6
    assert audits(setup) == []


def test_a_checkpoint_run_with_a_sync_only_fetches_and_never_charges_retries_or_notifies(setup, rzp):
    _episode(setup, 0)
    rzp.entity = entity(setup.small, status="halted")
    sync(setup)
    advance(setup, NOW + timedelta(days=3, seconds=1))
    assert {c[0] for c in rzp.calls} <= {"fetch_subscription", "fetch_invoices"}
    assert reload(setup).dunning_stage == 3


def test_a_full_grace_episode_ending_in_expiry_writes_no_payment_attempt(setup, rzp):
    _episode(setup, 0)
    for days in (3, 6):
        advance(setup, NOW + timedelta(days=days, seconds=1))
    advance(setup, NOW + timedelta(days=7, seconds=1))
    assert reload(setup).status == "EXPIRED"
    assert payments(setup) == []


def test_a_second_grace_episode_starts_again_at_stage_zero(setup, rzp):
    sub = setup.sub("ACTIVE")
    apply(setup, sub, entity(setup.small, status="halted"), started=dj_timezone.now())
    first = reload(setup)
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        Subscription.objects.update(dunning_stage=6)

    new_start = NOW - timedelta(days=1)
    apply(setup, sub, entity(setup.small, start=new_start), [invoice(start=new_start)],
          started=dj_timezone.now() + timedelta(seconds=1))
    recovered = reload(setup)
    assert recovered.status == "ACTIVE" and recovered.dunning_stage is None

    apply(setup, sub, entity(setup.small, start=new_start, status="pending"),
          started=dj_timezone.now() + timedelta(seconds=2))
    second = reload(setup)
    assert second.status == "PAST_DUE" and second.dunning_stage == 0
    assert second.past_due_at > first.past_due_at


def test_advance_dunning_touches_nothing_outside_past_due(setup, rzp):
    setup.sub("ACTIVE")
    advance(setup, NOW + timedelta(days=30))
    assert reload(setup).status == "ACTIVE" and audits(setup) == []
    assert rzp.calls == []


def test_advance_dunning_with_no_subscription_is_a_no_op(setup, rzp):
    advance(setup, NOW)
    assert rzp.calls == []


# --- maintenance_due ------------------------------------------------------------------------


def due(setup, at=None):
    with tenant_context(setup.owner.merchant.id):
        return services.maintenance_due(at)


def test_maintenance_due_is_false_for_a_settled_merchant(setup):
    assert due(setup) is False  # no row
    setup.sub("ACTIVE")
    assert due(setup, NOW) is False


def test_maintenance_due_for_an_old_unprocessed_event_only(setup):
    setup.sub("ACTIVE")
    _event(setup)
    assert due(setup, dj_timezone.now() + timedelta(seconds=30)) is False
    assert due(setup, dj_timezone.now() + timedelta(minutes=3)) is True


def test_maintenance_due_counts_an_event_of_exactly_two_minutes_as_due(setup):
    """The threshold is "at least 2 minutes old" (created_at <= now - 2 min):
    exactly on it is due, one microsecond short is not."""
    setup.sub("ACTIVE")
    _event(setup)
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        created_at = BillingEvent.objects.get().created_at
    two_minutes = timedelta(minutes=2)
    assert due(setup, created_at + two_minutes) is True
    assert due(setup, created_at + two_minutes - timedelta(microseconds=1)) is False


def test_maintenance_due_for_a_recent_incomplete_row_with_a_ref_only(setup):
    setup.sub("INCOMPLETE")
    assert due(setup) is True
    assert due(setup, dj_timezone.now() + timedelta(days=8)) is False


def test_maintenance_due_for_a_lapsed_active_period(setup):
    setup.sub("ACTIVE")
    assert due(setup, END - timedelta(seconds=1)) is False
    assert due(setup, END) is True


def test_a_lapsed_active_row_with_no_provider_reference_is_not_swept(setup, rzp):
    """Decided 2026-10-01: nothing to sync, so not enqueued every tick. The
    row is still not entitled once its period has ended."""
    setup.sub("ACTIVE", ref=None)
    assert due(setup, END + timedelta(days=1)) is False
    with tenant_context(setup.owner.merchant.id):
        services.sync_subscription()  # still a no-op with no provider call
    assert rzp.calls == []


def test_maintenance_due_for_any_past_due_row(setup):
    setup.sub("PAST_DUE")
    assert due(setup) is True


@pytest.mark.parametrize("local", ["CANCELLED", "EXPIRED"])
def test_maintenance_due_for_an_owed_provider_cancel_until_the_provider_status_is_terminal(setup, local):
    setup.sub(local, provider_status="halted")
    assert due(setup) is True
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        Subscription.objects.update(provider_status="cancelled")
    assert due(setup) is False
    with tenant_context(setup.owner.merchant.id), tenant_atomic():
        Subscription.objects.update(provider_status=None)
    assert due(setup) is True  # never seen terminal: still owed
