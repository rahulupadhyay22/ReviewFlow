"""A merchant's PAST_DUE cancel versus a provider sync, with real threads and
real PostgreSQL transactions (spec 07 "Expiry versus sync: race rules", which
covers a merchant's PAST_DUE cancel as a local transition).

Each thread runs a real production entry point on its own connection: the real
`POST /billing/subscription/cancel` (session OWNER, request transaction, row
lock held until the request commits) and the real `maintain_subscription` task
against the FakeProvider. thread_helpers.race makes the first thread hold the
Subscription row lock and observes the second thread's own backend waiting on
it in pg_stat_activity before releasing the first; no fixed sleeps.

With the cancel first, the sync's snapshot is fetched while the cancel holds
the lock (before Razorpay is told to cancel) and is applied after the cancel
commits: it is judged by the mapping table alone. With the sync first, the
cancel re-reads the row the sync left."""
from datetime import datetime, timedelta, timezone

import pytest
from django.utils import timezone as dj_timezone

from billing import tasks
from billing.models import PaymentAttempt
from billing.tests.snapshot_helpers import NOW, REF, entity, invoice, ts
from billing.tests.state_helpers import make_event, read_state
from billing.tests.thread_helpers import race

pytestmark = pytest.mark.django_db(transaction=True)

CANCEL = "/api/v1/billing/subscription/cancel"
NEW_START = NOW - timedelta(days=1)
NEW_END = NEW_START + timedelta(days=30)
REQUESTED = "billing.cancellation_requested"
ACTIVATED = "billing.subscription_activated"
RECOVERED = "billing.subscription_recovered"


def unix(value) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


@pytest.fixture
def w(lifecycle_setup, session_client):
    w = lifecycle_setup
    w.sub(w.a, "PAST_DUE", past_due_at=dj_timezone.now() - timedelta(days=1))  # provider `halted`
    make_event(w.a.merchant, REF, "evt_race")
    w.client = session_client(w.a.user.email)
    w.initial = read_state(w.a.merchant)
    return w


def provide(w, snapshot):
    status = "halted" if snapshot == "halted" else "active"
    w.provider.entities[REF] = entity(w.small, status=status, start=NEW_START)
    w.provider.invoices = [invoice(start=NEW_START, status="paid" if snapshot == "qualifying" else "issued")]


@pytest.mark.parametrize("snapshot", ["qualifying", "active_unpaid", "halted"])
@pytest.mark.parametrize("order", ["cancel_first", "sync_first"])
def test_cancel_and_sync_in_real_threads_end_in_the_specified_state(w, monkeypatch, order, snapshot):
    provide(w, snapshot)
    codes = []

    def do_cancel():
        codes.append(w.client.post(CANCEL, {}, format="json", HTTP_X_CSRFTOKEN=w.client.csrf).status_code)

    def do_sync():
        tasks.maintain_subscription(w.a.merchant.id)

    first, second = (do_cancel, do_sync) if order == "cancel_first" else (do_sync, do_cancel)
    race(monkeypatch, first=first, second=second)
    state = read_state(w.a.merchant)
    assert codes == [200]
    assert state.provider_synced_at is not None  # stamped by the sync, never by the cancel

    if order == "cancel_first":
        if snapshot == "qualifying":
            # The late-charge rule: a qualifying paid invoice reactivates the
            # merchant-cancelled row (documented reconciliation consequence).
            assert state.status == "ACTIVE"
            assert state.audits == [REQUESTED, ACTIVATED]
            assert state.payments == [
                ("pay_1", PaymentAttempt.AttemptType.RENEWAL, PaymentAttempt.Status.SUCCEEDED,
                 unix(ts(NEW_START) + 3600))
            ]
            assert state.usages == w.initial.usages + [(unix(ts(NEW_START)), unix(ts(NEW_END)), 0)]
            assert state.cancel_at_period_end is False
            assert state.events == {"evt_race": True}
        else:
            # Provider status alone never moves a CANCELLED row.
            assert state.status == "CANCELLED"
            assert state.audits == [REQUESTED]
            assert state.payments == [] and state.usages == w.initial.usages
            assert state.period == w.initial.period
            assert state.provider_status == ("halted" if snapshot == "halted" else "active")
            assert state.events == {"evt_race": snapshot == "halted"}
        assert (state.past_due_at, state.dunning_stage) == (None, None)
        return

    # sync_first
    if snapshot == "qualifying":
        assert state.status == "ACTIVE" and state.cancel_at_period_end is True
        assert state.audits == [RECOVERED, REQUESTED]
        assert len(state.payments) == 1 and state.payments[0][1] == PaymentAttempt.AttemptType.RETRY
        assert state.usages == w.initial.usages + [(unix(ts(NEW_START)), unix(ts(NEW_END)), 0)]
        assert state.events == {"evt_race": True}
    else:
        assert state.status == "CANCELLED"
        assert state.audits == [REQUESTED]
        assert state.payments == [] and state.usages == w.initial.usages
        assert state.provider_status == "cancelled"  # the cancel's own answer, applied last
        assert (state.past_due_at, state.dunning_stage, state.cancel_at_period_end) == (None, None, False)
        assert state.events == {"evt_race": snapshot == "halted"}
