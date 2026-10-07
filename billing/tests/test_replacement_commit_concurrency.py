"""W6 of 07-plan-change-replacement: the downgrade commit with REAL transactions
(django_db(transaction=True)) and real threads.

1. The provider cancel of the old subscription runs with no transaction open and no
   row lock held, and T1 is already committed when it runs (proved from a second
   connection).
2. The intent survives a crash between T1 and the provider call, and the sweep
   finishes it.
3. The commit versus another sweep, and the commit versus a merchant cancel, each end
   in one consistent state (thread_helpers.race: the first thread holds the real
   Subscription row lock, the second is observed waiting on it, no sleeps).

The G-2 entity field names are SYNTHETIC; nothing here claims any Razorpay behavior."""
import threading
from datetime import datetime, timezone

import pytest
from django.db import connection

from billing import razorpay, services, tasks
from billing.models import Subscription
from billing.tests.replacement_helpers import REPL_REF, provide_replacement, set_replacement
from billing.tests.snapshot_helpers import END, START, entity
from billing.tests.state_helpers import read_state
from billing.tests.thread_helpers import race
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db(transaction=True)

MARGIN = 3600
START_KEY, EXPIRY_KEY = "synthetic_start", "synthetic_expiry"
EXPIRES = int(END.timestamp()) - MARGIN - 1


class Crash(BaseException):
    """A process dying between T1 and the provider call."""


@pytest.fixture
def w(lifecycle_setup, settings, monkeypatch):
    w = lifecycle_setup
    settings.BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN = MARGIN
    monkeypatch.setattr(razorpay, "DOWNGRADE_START_FIELD", START_KEY)
    monkeypatch.setattr(razorpay, "DOWNGRADE_EXPIRY_FIELD", EXPIRY_KEY)
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, downgrade_to=w.small)
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(replacement_expires_at=datetime.fromtimestamp(EXPIRES, tz=timezone.utc))
    provide_replacement(w.provider, w.small, status="authenticated", paid=False, start=END)
    w.provider.entities[REPL_REF].update({START_KEY: int(END.timestamp()), EXPIRY_KEY: EXPIRES})
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="active", start=START)
    return w


def sweep(w):
    return lambda: tasks.maintain_subscription(w.a.merchant.id)


def row(w):
    with tenant_context(w.a.merchant.id), tenant_atomic():
        return Subscription.objects.get()


def old_cancels(w):
    return [c for c in w.provider.calls if c[0] == "cancel_subscription" and c[1] == w.ref(w.a)]


# --- 1. no transaction and no row lock across the provider call -----------------------------------------


def test_the_old_cancel_runs_with_no_transaction_and_no_row_lock_and_t1_already_committed(w, monkeypatch):
    seen = {}
    real = w.provider.cancel_subscription
    merchant_id = w.a.merchant.id

    def from_a_second_connection():
        try:
            with tenant_context(merchant_id), tenant_atomic():
                locked = Subscription.objects.select_for_update(nowait=True).get()
                seen["lock"] = "acquired"
                seen["t1_visible"] = locked.replacement_committed_at is not None
        except Exception as exc:  # recorded, asserted below
            seen["lock"] = f"blocked: {type(exc).__name__}"
        finally:
            connection.close()

    def probe(ref, at_cycle_end):
        seen["in_atomic_block"] = connection.in_atomic_block
        seen["args"] = (ref, at_cycle_end)
        other = threading.Thread(target=from_a_second_connection)
        other.start()
        other.join(30)
        return real(ref, at_cycle_end)

    monkeypatch.setattr(razorpay, "cancel_subscription", probe)
    with tenant_context(merchant_id):
        services._sync_replacement()

    assert seen["in_atomic_block"] is False  # no transaction of ours is open
    assert seen["lock"] == "acquired"  # NOWAIT would fail if the row lock were still held
    assert seen["t1_visible"] is True  # T1 had committed before the provider was called
    assert seen["args"] == (w.ref(w.a), True)
    assert row(w).replacement_cancel_confirmed_at is not None  # T2 recorded the outcome


# --- 2. a crash between T1 and the provider call ----------------------------------------------------------------


def test_the_intent_survives_a_crash_between_t1_and_the_provider_call(w, monkeypatch):
    real = w.provider.cancel_subscription

    def die(ref, at_cycle_end):
        raise Crash()

    monkeypatch.setattr(razorpay, "cancel_subscription", die)
    with pytest.raises(Crash), tenant_context(w.a.merchant.id):
        services._sync_replacement()

    r = row(w)  # a fresh read: T1 really committed
    assert r.replacement_committed_at is not None and r.replacement_cancel_confirmed_at is None
    assert r.replacement_provider_ref == REPL_REF
    assert old_cancels(w) == []  # the call never reached the provider

    monkeypatch.setattr(razorpay, "cancel_subscription", real)
    with tenant_context(w.a.merchant.id):
        services._sync_replacement()  # the sweep re-issues it
    assert row(w).replacement_cancel_confirmed_at is not None
    assert len(old_cancels(w)) == 1


# --- 3. races ---------------------------------------------------------------------------------------------------------


def test_two_sweeps_race_one_commit(w, monkeypatch):
    race(monkeypatch, first=sweep(w), second=sweep(w))
    state = read_state(w.a.merchant)
    r = row(w)
    assert state.audits == ["billing.replacement_committed"]  # one commit
    assert r.replacement_committed_at is not None and r.replacement_cancel_confirmed_at is not None
    assert r.replacement_provider_ref == REPL_REF and r.retired_provider_ref is None
    assert old_cancels(w) and all(c[2] is True for c in old_cancels(w))  # only ever at cycle end, only the old ref
    assert r.plan_id == w.big.pk  # the plan is unchanged until the switch


def test_the_commit_versus_a_merchant_cancel_ends_in_one_consistent_state(w, monkeypatch):
    def merchant_cancel():
        with tenant_context(w.a.merchant.id), tenant_atomic():
            services.cancel_subscription(actor=w.a)

    race(monkeypatch, first=sweep(w), second=merchant_cancel)
    r = row(w)
    # whichever won, the replacement is cancelled and retired, nothing is left half-committed,
    # and the subscription is cancelling at its period end
    assert r.replacement_provider_ref is None and r.replacement_plan_id is None
    assert (r.replacement_committed_at, r.replacement_cancel_confirmed_at) == (None, None)
    # retired, or already cleared by the sweep's retired step once the provider reported it terminal
    assert (r.retired_provider_ref, r.retired_kind) in ((REPL_REF, "ABANDONED_REPLACEMENT"), (None, None))
    assert r.cancel_at_period_end is True and r.status == "ACTIVE" and r.plan_id == w.big.pk
    assert ("cancel_subscription", REPL_REF, False) in w.provider.calls
    assert all(c[2] is True for c in old_cancels(w))
