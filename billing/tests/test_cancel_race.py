"""A merchant's local-first PAST_DUE cancel versus a provider snapshot (spec 07
"Expiry versus sync: race rules": the rules cover "any other local transition:
a merchant's PAST_DUE cancel"; "Merchant cancellation of a PAST_DUE
subscription"; the mapping table). Sequential and deterministic.

SCOPE: these prove the OUTCOMES of each serial order, forced through the
production path: the cancel is the real `POST /billing/subscription/cancel`
(session OWNER, CSRF, request transaction) and the sync is the real
`maintain_subscription` task. Order A runs the cancel from a hook on the
FakeProvider call that produced the snapshot, so the snapshot was fetched before
the cancel and is applied after it commits. Order B applies the snapshot first.
Lock waiting under real concurrency is proven in test_cancel_race_concurrency.py.

The cancel is local and never stamps provider_synced_at, so a snapshot fetched
before it is not stale and is judged by the mapping table alone: provider status
alone never moves a CANCELLED row, and only a qualifying paid invoice for the
snapshot's own period reactivates it (the late-charge rule)."""
import contextvars
from datetime import datetime, timedelta, timezone

import pytest
from django.utils import timezone as dj_timezone

from billing import razorpay, tasks
from billing.models import PaymentAttempt
from billing.tests.snapshot_helpers import NOW, REF, entity, invoice, ts
from billing.tests.state_helpers import make_event, read_state

pytestmark = pytest.mark.django_db

CANCEL = "/api/v1/billing/subscription/cancel"
NEW_START = NOW - timedelta(days=1)
NEW_END = NEW_START + timedelta(days=30)
REQUESTED = "billing.cancellation_requested"
ACTIVATED = "billing.subscription_activated"
RECOVERED = "billing.subscription_recovered"
PROVIDER_CANCELLED = "billing.subscription_cancelled"


def unix(value) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


@pytest.fixture
def w(lifecycle_setup, session_client):
    """A is PAST_DUE inside its grace (provider `halted`, so the cancel also
    asks Razorpay to cancel), with one unprocessed event, and an OWNER session."""
    w = lifecycle_setup
    w.sub(w.a, "PAST_DUE", past_due_at=dj_timezone.now() - timedelta(days=1))
    make_event(w.a.merchant, REF, "evt_race")
    w.client = session_client(w.a.user.email)
    w.initial = read_state(w.a.merchant)
    return w


def cancel(w):
    """The real endpoint, run with no tenant context, as a request is."""
    return contextvars.Context().run(
        lambda: w.client.post(CANCEL, {}, format="json", HTTP_X_CSRFTOKEN=w.client.csrf)
    )


def sync(w):
    tasks.maintain_subscription(w.a.merchant.id)


def provide(w, status, paid=False):
    w.provider.entities[REF] = entity(w.small, status=status, start=NEW_START)
    w.provider.invoices = [invoice(start=NEW_START)] if paid else [invoice(start=NEW_START, status="issued")]


def after_fetch(monkeypatch, w, hook, status):
    """Run `hook` once, after the provider call that completes the snapshot."""
    method = "fetch_invoices" if status == "active" else "fetch_subscription"
    real = getattr(w.provider, method)
    done = []

    def wrapped(ref):
        result = real(ref)
        if not done:
            done.append(True)
            hook()
        return result

    monkeypatch.setattr(razorpay, method, wrapped)


NO_TRANSITION = {
    "pending": "pending",
    "halted": "halted",
    "provider_cancelled": "cancelled",
    "provider_completed": "completed",
    "provider_expired": "expired",
    "created": "created",
    "authenticated": "authenticated",
    "paused": "paused",
    "unknown": "weird_status",
}
TERMINAL = {"cancelled", "completed", "expired"}


def new_usage():
    return (unix(ts(NEW_START)), unix(ts(NEW_END)), 0)


# === order A: the cancel commits, then a snapshot fetched before it is applied ====


@pytest.mark.parametrize("status", list(NO_TRANSITION.values()), ids=list(NO_TRANSITION))
def test_order_a_provider_status_alone_never_moves_a_cancelled_row(w, monkeypatch, status):
    provide(w, status)
    seen = {}

    def merchant_cancels():
        seen["code"] = cancel(w).status_code
        seen["after_cancel"] = read_state(w.a.merchant)

    after_fetch(monkeypatch, w, merchant_cancels, status)
    sync(w)
    assert seen["code"] == 200
    assert seen["after_cancel"].status == "CANCELLED"
    assert seen["after_cancel"].provider_synced_at is None  # the cancel stamps nothing
    assert seen["after_cancel"].provider_status == "cancelled"  # what the cancel call answered

    state = read_state(w.a.merchant)
    assert state.status == "CANCELLED"  # never back to PAST_DUE, never ACTIVE
    assert state.audits == [REQUESTED]
    assert (state.past_due_at, state.dunning_stage, state.cancel_at_period_end) == (None, None, False)
    assert state.payments == [] and state.usages == w.initial.usages
    assert state.period == w.initial.period
    # The snapshot was fetched first, so it is not stale: its status is recorded
    # over the cancel's answer (the documented reconciliation consequence).
    assert state.provider_status == status[:16] and state.provider_synced_at is not None
    assert state.events == {"evt_race": True}  # a no-change cell settles


def test_order_a_active_without_a_qualifying_invoice_never_reactivates(w, monkeypatch):
    provide(w, "active", paid=False)
    after_fetch(monkeypatch, w, lambda: cancel(w), "active")
    sync(w)
    state = read_state(w.a.merchant)
    assert state.status == "CANCELLED"
    assert state.audits == [REQUESTED]
    assert state.payments == [] and state.usages == w.initial.usages and state.period == w.initial.period
    assert state.provider_status == "active"
    assert state.events == {"evt_race": False}  # unpaid `active` does not settle


def test_order_a_a_qualifying_paid_invoice_reactivates_through_the_late_charge_rule(w, monkeypatch):
    provide(w, "active", paid=True)
    after_fetch(monkeypatch, w, lambda: cancel(w), "active")
    sync(w)
    state = read_state(w.a.merchant)
    assert state.status == "ACTIVE"
    assert state.audits == [REQUESTED, ACTIVATED]
    assert state.payments == [
        ("pay_1", PaymentAttempt.AttemptType.RENEWAL, PaymentAttempt.Status.SUCCEEDED, unix(ts(NEW_START) + 3600))
    ]
    assert state.usages == w.initial.usages + [new_usage()]
    assert state.period == (unix(ts(NEW_START)), unix(ts(NEW_END)))
    assert (state.past_due_at, state.dunning_stage, state.cancel_at_period_end) == (None, None, False)
    assert state.provider_status == "active"
    assert state.events == {"evt_race": True}

    # Razorpay itself was cancelled by the request; the next sync sees it and
    # the row converges to CANCELLED by the mapping table (no new rule).
    assert w.provider.entities[REF]["status"] == "cancelled"
    sync(w)
    converged = read_state(w.a.merchant)
    assert converged.status == "CANCELLED"
    assert converged.audits == [REQUESTED, ACTIVATED, PROVIDER_CANCELLED]
    assert converged.payments == state.payments and converged.usages == state.usages


# === order B: the snapshot is applied, then the cancel ===========================


@pytest.mark.parametrize("status", list(NO_TRANSITION.values()), ids=list(NO_TRANSITION))
def test_order_b_the_cancel_follows_whatever_state_the_snapshot_left(w, status):
    provide(w, status)
    sync(w)
    after_sync = read_state(w.a.merchant)
    code = cancel(w).status_code
    state = read_state(w.a.merchant)
    assert state.provider_synced_at == after_sync.provider_synced_at  # the cancel stamps nothing
    assert state.payments == [] and state.usages == w.initial.usages
    assert state.events == {"evt_race": True}

    if status in TERMINAL:
        # PAST_DUE + provider terminal inside grace -> CANCELLED by the sync;
        # the merchant's cancel then has nothing to cancel.
        assert after_sync.status == "CANCELLED" and after_sync.audits == [PROVIDER_CANCELLED]
        assert code == 409
        assert state == after_sync
        return

    assert after_sync.status == "PAST_DUE" and after_sync.audits == []  # no transition
    assert code == 200
    assert state.status == "CANCELLED"
    assert state.audits == [REQUESTED]
    assert (state.past_due_at, state.dunning_stage, state.cancel_at_period_end) == (None, None, False)
    # The cancel asks Razorpay only where the shared rule allows touching it.
    touches_provider = status in ("pending", "halted", "created")
    assert state.provider_status == ("cancelled" if touches_provider else status[:16])
    assert ("cancel_subscription" in w.provider.names()) is touches_provider


def test_order_b_active_without_a_qualifying_invoice_then_cancel(w):
    provide(w, "active", paid=False)
    sync(w)
    assert read_state(w.a.merchant).status == "PAST_DUE"  # unpaid `active` changes nothing
    assert cancel(w).status_code == 200
    state = read_state(w.a.merchant)
    assert state.status == "CANCELLED" and state.audits == [REQUESTED]
    assert state.payments == [] and state.usages == w.initial.usages
    assert state.provider_status == "cancelled"
    assert state.events == {"evt_race": False}


def test_order_b_a_recovery_first_turns_the_cancel_into_cancel_at_period_end(w):
    provide(w, "active", paid=True)
    sync(w)
    recovered = read_state(w.a.merchant)
    assert recovered.status == "ACTIVE" and recovered.audits == [RECOVERED]
    assert recovered.payments[0][1] == PaymentAttempt.AttemptType.RETRY

    resp = cancel(w)  # the row is ACTIVE now: reconcile, then cancel at period end
    assert resp.status_code == 200
    state = read_state(w.a.merchant)
    assert state.status == "ACTIVE" and state.cancel_at_period_end is True
    assert state.audits == [RECOVERED, REQUESTED]
    assert state.payments == recovered.payments
    assert state.usages == w.initial.usages + [new_usage()]
    assert ("cancel_subscription", REF, True) in w.provider.calls  # at cycle end, not now
    assert state.events == {"evt_race": True}
