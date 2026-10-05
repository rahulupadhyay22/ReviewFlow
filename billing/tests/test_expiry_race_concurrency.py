"""Expiry versus sync with real threads and real transactions (spec 07
"Expiry versus sync: race rules", rule 6).

Each thread runs a real production entry point on its own connection:
`tasks.advance_subscription_dunning` (the expiry) and
`tasks.maintain_subscription` (a real sync against the FakeProvider). The lock
wait is established and observed by thread_helpers.race (the first thread
holds the real Subscription row lock; the second is seen waiting on it in
pg_stat_activity before the first is released; no fixed sleeps).

All six combinations of order and snapshot are asserted in full (subscription
fields, audit actions, payment ledger, usage records, processed events)."""
from datetime import datetime, timedelta, timezone

import pytest
from django.utils import timezone as dj_timezone

from billing import tasks
from billing.models import PaymentAttempt
from billing.tests.snapshot_helpers import NOW, REF, entity, invoice, ts
from billing.tests.state_helpers import make_event, read_state
from billing.tests.thread_helpers import race

pytestmark = pytest.mark.django_db(transaction=True)

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


def expire(w):
    return lambda: tasks.advance_subscription_dunning(w.a.merchant.id)


def sync(w):
    return lambda: tasks.maintain_subscription(w.a.merchant.id)


def start_past_due(w):
    w.sub(w.a, "PAST_DUE", past_due_at=dj_timezone.now() - timedelta(days=8))
    make_event(w.a.merchant, REF, "evt_race")
    return read_state(w.a.merchant)


def provide(w, snapshot):
    if snapshot == "qualifying":
        w.provider.entities[REF] = entity(w.small, status="active", start=NEW_START)
        w.provider.invoices = [invoice(start=NEW_START)]
    elif snapshot == "active_unpaid":
        w.provider.entities[REF] = entity(w.small, status="active", start=NEW_START)
        w.provider.invoices = [invoice(start=NEW_START, status="issued")]
    else:  # halted
        w.provider.entities[REF] = entity(w.small, status="halted", start=NEW_START)
        w.provider.invoices = []


def expected_new_usage():
    return (unix(ts(NEW_START)), unix(ts(NEW_END)), 0)


@pytest.mark.parametrize("snapshot", ["qualifying", "active_unpaid", "halted"])
@pytest.mark.parametrize("order", ["expiry_first", "sync_first"])
def test_rule_6_real_threads_end_in_the_same_state_in_both_orders(w, monkeypatch, order, snapshot):
    initial = start_past_due(w)
    provide(w, snapshot)
    first, second = (expire(w), sync(w)) if order == "expiry_first" else (sync(w), expire(w))
    race(monkeypatch, first=first, second=second)
    state = read_state(w.a.merchant)

    if snapshot == "qualifying":
        # The payment is a fact: ACTIVE in either order, never an EXPIRED row
        # with a new period, never an ACTIVE row without the paid invoice.
        assert state.status == "ACTIVE"
        assert state.period == (unix(ts(NEW_START)), unix(ts(NEW_END)))
        assert state.usages == initial.usages + [expected_new_usage()]
        assert state.events == {"evt_race": True}
        assert (state.past_due_at, state.dunning_stage, state.cancel_at_period_end) == (None, None, False)
        assert len(state.payments) == 1
        payment_id, attempt_type, payment_status, attempted_at = state.payments[0]
        assert (payment_id, payment_status, attempted_at) == (
            "pay_1",
            PaymentAttempt.Status.SUCCEEDED,
            unix(ts(NEW_START) + 3600),  # the invoice's paid_at
        )
        if order == "expiry_first":
            assert state.audits == [EXPIRED, ACTIVATED]
            assert attempt_type == PaymentAttempt.AttemptType.RENEWAL
        else:
            # The recovery landed first; the expiry re-read the row, found it
            # no longer PAST_DUE and changed nothing.
            assert state.audits == [RECOVERED]
            assert attempt_type == PaymentAttempt.AttemptType.RETRY
    else:
        assert state.status == "EXPIRED"
        assert state.audits == [EXPIRED]
        assert state.payments == [] and state.usages == initial.usages
        assert state.period == initial.period
        assert (state.past_due_at, state.dunning_stage) == (None, None)
        # `active` without qualifying proof does not settle; `halted` does.
        assert state.events == {"evt_race": snapshot == "halted"}
