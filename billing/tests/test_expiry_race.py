"""Expiry versus sync, race rules 1-5 (spec 07 "Expiry versus sync: race
rules"), sequential and deterministic.

SCOPE: these tests prove the OUTCOMES of the rules when an interleaving is
forced. They do not prove production concurrency or lock behavior; that is the
job of test_expiry_race_concurrency.py (real threads). The interleaving is
forced through the production path: a hook on the FakeProvider method that
returns the snapshot runs the real expiry (or a complete newer sync) after the
fetch and before the sync's own apply step, so the real
sync_subscription -> _apply_snapshot code is what runs second.

Every case asserts the whole outcome via read_state: subscription fields, the
exact billing audit actions, the payment ledger, the usage records and which
BillingEvent rows are processed."""
from datetime import datetime, timedelta, timezone

import pytest
from django.utils import timezone as dj_timezone

from billing import razorpay, tasks
from billing.models import PaymentAttempt
from billing.tests.snapshot_helpers import NOW, REF, START, entity, invoice, ts
from billing.tests.state_helpers import make_event, read_state

pytestmark = pytest.mark.django_db

NEW_START = NOW - timedelta(days=1)
NEW_END = NEW_START + timedelta(days=30)
EXPIRED = "billing.subscription_expired"
ACTIVATED = "billing.subscription_activated"
RECOVERED = "billing.subscription_recovered"


@pytest.fixture
def w(lifecycle_setup):
    return lifecycle_setup


def unix(value) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def a_id(w):
    return w.a.merchant.id


def expire(w):
    tasks.advance_subscription_dunning(a_id(w))


def sync(w):
    tasks.maintain_subscription(a_id(w))


def start_past_due(w):
    """PAST_DUE for 8 days (the expiry is due) with one old unprocessed event."""
    w.sub(w.a, "PAST_DUE", past_due_at=dj_timezone.now() - timedelta(days=8))
    make_event(w.a.merchant, REF, "evt_race")
    return read_state(w.a.merchant)


def provide(w, *, status="active", invoices=None):
    """Razorpay (fake) now reports `status` for the new period; `invoices`
    None means the one qualifying paid invoice."""
    w.provider.entities[REF] = entity(w.small, status=status, start=NEW_START)
    w.provider.invoices = [invoice(start=NEW_START)] if invoices is None else invoices


def after_fetch(monkeypatch, w, hook, *, method):
    """Run `hook` once, right after the FakeProvider's `method` has produced
    its answer: the answer is already captured, so the sync that asked applies
    it after whatever the hook did."""
    real = getattr(w.provider, method)
    done = []

    def wrapped(ref):
        result = real(ref)
        if not done:
            done.append(True)  # before the hook: a nested sync must not re-trigger it
            hook()
        return result

    monkeypatch.setattr(razorpay, method, wrapped)


def hook_method(status):
    """The last provider call a sync makes before it applies the snapshot."""
    return "fetch_invoices" if status == "active" else "fetch_subscription"


# === rule 2: expiry first, non-qualifying snapshot after ===============================

NON_QUALIFYING = {
    "pending": ("pending", []),
    "halted": ("halted", []),
    "provider_cancelled": ("cancelled", []),
    "provider_completed": ("completed", []),
    "provider_expired": ("expired", []),
    "paused": ("paused", []),
    "created": ("created", []),
    "authenticated": ("authenticated", []),
    "unrecognized": ("weird_status", []),
    "active_without_any_invoice": ("active", []),
    "active_invoice_not_paid": ("active", [invoice(start=NEW_START, status="issued")]),
    "active_amount_still_due": ("active", [invoice(start=NEW_START, amount_due=100)]),
    "active_other_subscriptions_invoice": ("active", [invoice(start=NEW_START, subscription_id="sub_other")]),
    "active_empty_payment_id": ("active", [invoice(start=NEW_START, payment_id="")]),
    "active_window_excludes_current_start": (
        "active",
        [invoice(start=NEW_START + timedelta(days=30))],
    ),
}


@pytest.mark.parametrize("fetched", ["before_the_expiry", "after_the_expiry"])
@pytest.mark.parametrize("case", list(NON_QUALIFYING.values()), ids=list(NON_QUALIFYING))
def test_rule_2_a_non_qualifying_snapshot_never_leaves_expired(w, monkeypatch, case, fetched):
    status, invoices = case
    initial = start_past_due(w)
    if fetched == "before_the_expiry":
        provide(w, status=status, invoices=invoices)
        after_fetch(monkeypatch, w, lambda: expire(w), method=hook_method(status))
        sync(w)
    else:
        expire(w)
        provide(w, status=status, invoices=invoices)  # configured after the expiry's owed cancel
        sync(w)
    state = read_state(w.a.merchant)
    assert state.status == "EXPIRED"
    assert state.audits == [EXPIRED]
    assert state.payments == [] and state.usages == initial.usages  # no new UsageRecord
    assert state.period == initial.period  # nothing taken from the snapshot
    assert (state.past_due_at, state.dunning_stage, state.cancel_at_period_end) == (None, None, False)
    assert state.provider_status == status[:16] and state.provider_synced_at is not None
    settled = status != "active"  # active without qualifying proof does not settle
    assert state.events == {"evt_race": settled}


# === rule 3: expiry first, qualifying late payment after ===============================


@pytest.mark.parametrize("fetched", ["before_the_expiry", "after_the_expiry"])
def test_rule_3_a_qualifying_late_payment_reactivates_whenever_it_was_fetched(w, monkeypatch, fetched):
    initial = start_past_due(w)
    if fetched == "before_the_expiry":
        provide(w)
        after_fetch(monkeypatch, w, lambda: expire(w), method="fetch_invoices")
        sync(w)
    else:
        expire(w)
        provide(w)
        sync(w)
    state = read_state(w.a.merchant)
    assert state.status == "ACTIVE"
    assert state.audits == [EXPIRED, ACTIVATED]
    assert state.payments == [
        ("pay_1", PaymentAttempt.AttemptType.RENEWAL, PaymentAttempt.Status.SUCCEEDED, unix(ts(NEW_START) + 3600))
    ]
    assert state.usages == initial.usages + [(unix(ts(NEW_START)), unix(ts(NEW_END)), 0)]
    assert state.period == (unix(ts(NEW_START)), unix(ts(NEW_END)))
    assert (state.past_due_at, state.dunning_stage, state.cancel_at_period_end) == (None, None, False)
    assert state.events == {"evt_race": True}


# === rule 4: recovery first, expiry after ==============================================


def test_rule_4_a_recovery_is_never_undone_by_a_later_expiry_run(w):
    initial = start_past_due(w)
    provide(w)
    sync(w)  # PAST_DUE -> ACTIVE, before the expiry runs
    recovered = read_state(w.a.merchant)
    assert recovered.status == "ACTIVE" and recovered.audits == [RECOVERED]
    assert recovered.payments[0][1] == PaymentAttempt.AttemptType.RETRY
    assert recovered.usages == initial.usages + [(unix(ts(NEW_START)), unix(ts(NEW_END)), 0)]

    expire(w)  # the expiry finds the row no longer PAST_DUE
    assert read_state(w.a.merchant) == recovered
    assert EXPIRED not in read_state(w.a.merchant).audits


# === rule 5: repeats are no-ops ========================================================


def test_rule_5_expiry_twice_changes_nothing_the_second_time(w):
    start_past_due(w)
    expire(w)
    first = read_state(w.a.merchant)
    expire(w)
    assert read_state(w.a.merchant) == first
    assert first.audits == [EXPIRED]


def test_rule_5_the_same_snapshot_twice_changes_nothing_the_second_time(w):
    start_past_due(w)
    expire(w)
    provide(w)
    sync(w)
    first = read_state(w.a.merchant)
    sync(w)
    second = read_state(w.a.merchant)
    for state in (first, second):
        assert state.audits == [EXPIRED, ACTIVATED] and len(state.payments) == 1 and len(state.usages) == 2
    assert {k: v for k, v in vars(second).items() if k != "provider_synced_at"} == {
        k: v for k, v in vars(first).items() if k != "provider_synced_at"
    }


# === rule 1: an older snapshot never overwrites a newer one ============================


def run_newer_sync_inside_the_older_ones_fetch(monkeypatch, w):
    """The older sync has fetched `halted`/`pending`; before it applies, a
    complete newer sync (qualifying paid snapshot) runs and finishes. Returns
    the state captured right after the newer sync."""
    captured = {}

    def newer():
        provide(w)  # the provider now reports the paid period
        sync(w)  # a complete newer sync: its fetch started later
        captured["state"] = read_state(w.a.merchant)

    after_fetch(monkeypatch, w, newer, method="fetch_subscription")
    return captured


def test_rule_1_an_older_snapshot_after_a_newer_recovery_is_dropped(w, monkeypatch):
    start_past_due(w)
    w.provider.entities[REF] = entity(w.small, status="halted", start=START)  # the older snapshot
    captured = run_newer_sync_inside_the_older_ones_fetch(monkeypatch, w)
    sync(w)  # the older sync: fetches `halted`, then the newer one runs, then it applies
    final = read_state(w.a.merchant)
    assert captured["state"].status == "ACTIVE" and captured["state"].audits == [RECOVERED]
    # Applied, `halted` on an ACTIVE row would have moved it back to PAST_DUE.
    assert final == captured["state"]


def test_rule_1_an_older_snapshot_after_a_newer_late_charge_reactivation_is_dropped(w, monkeypatch):
    w.sub(w.a, "EXPIRED", provider_status="halted")
    make_event(w.a.merchant, REF, "evt_race")
    w.provider.entities[REF] = entity(w.small, status="pending", start=START)
    captured = run_newer_sync_inside_the_older_ones_fetch(monkeypatch, w)
    sync(w)
    final = read_state(w.a.merchant)
    assert captured["state"].status == "ACTIVE" and captured["state"].audits == [ACTIVATED]
    assert final == captured["state"]  # `pending` applied would have made it PAST_DUE
