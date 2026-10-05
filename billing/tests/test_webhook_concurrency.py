"""Razorpay webhook races with real threads and real transactions.

Two simultaneous deliveries of the same Razorpay event (spec 07 receive
order step 7: UNIQUE(provider, provider_event_id), a duplicate is a 200
no-op). Real threads, real transactions, deterministic overlap: the first
request inserts its BillingEvent and is held before commit; the second is
started only then, and the first is released only once Postgres shows the
second waiting on a lock, i.e. blocked on the unique index by the first's
uncommitted row.

An event inserted before a sync's fetch began but committed after that sync
marked events processed is not marked by it: the sync's UPDATE cannot see the
uncommitted row, so that event's own task finds it unprocessed and fetches."""
import threading
import time

import pytest
from django.db import connection

from billing import tasks
from billing.models import BillingEvent, Subscription
from billing.tests.webhook_helpers import deliver, event
from core.tenancy import tenant_atomic, tenant_context

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("webhook_secret")]


def _backends_waiting_on_a_lock() -> int:
    with connection.cursor() as cursor:
        # Only another backend, blocked on an INSERT into the events table:
        # the second delivery waiting on the first's uncommitted unique key.
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND wait_event_type = 'Lock' AND query ILIKE %s",
            ["INSERT INTO \"billing_billingevent\"%"],
        )
        return cursor.fetchone()[0]


def test_two_simultaneous_deliveries_of_one_event_store_one_row_and_queue_one_sync(
    webhook_setup, monkeypatch
):
    queued = []
    monkeypatch.setattr(tasks.sync_subscription, "delay", lambda merchant_id: queued.append(merchant_id))

    inserted, release = threading.Event(), threading.Event()
    real_create = BillingEvent.objects.create

    def create_then_hold_the_first(**kwargs):
        row = real_create(**kwargs)  # the second request blocks in here
        if not inserted.is_set():
            inserted.set()
            assert release.wait(timeout=30), "the first request was never released"
        return row

    monkeypatch.setattr(BillingEvent.objects, "create", create_then_hold_the_first)

    statuses, errors = {}, []

    def post(name):
        try:
            statuses[name] = deliver(event(), event_id="evt_simultaneous").status_code
        except Exception as exc:  # captured for the assertion below
            errors.append(exc)
        finally:
            connection.close()

    first = threading.Thread(target=post, args=("first",))
    first.start()
    assert inserted.wait(timeout=30), "the first request never inserted"
    second = threading.Thread(target=post, args=("second",))
    second.start()

    # Condition-based wait (not a fixed sleep): poll until the second request
    # is blocked by the first's uncommitted row, bounded at 30 s.
    deadline = time.monotonic() + 30
    while _backends_waiting_on_a_lock() == 0:
        assert time.monotonic() < deadline, "the second request never blocked on the first"
        time.sleep(0.01)
    release.set()
    first.join(timeout=30)
    second.join(timeout=30)

    assert not errors, errors
    assert statuses == {"first": 200, "second": 200}
    with tenant_context(webhook_setup.a.merchant.id), tenant_atomic():
        assert list(BillingEvent.objects.values_list("provider_event_id", flat=True)) == ["evt_simultaneous"]
    with tenant_context(webhook_setup.b.merchant.id), tenant_atomic():
        assert BillingEvent.objects.count() == 0
    assert queued == [webhook_setup.a.merchant.id]  # only the committed insert queues a sync


def test_an_event_received_before_a_fetch_but_committed_after_its_sync_is_not_marked_processed(
    webhook_setup, monkeypatch
):
    """Read committed: the settled sync's UPDATE cannot see a BillingEvent
    whose transaction is still open, even if that row's created_at is older
    than the fetch start. The late commit then enqueues its own task, which
    finds the event unprocessed and fetches. The clock-skew margin is off here
    so that only commit visibility, not the margin, keeps the row unmarked."""
    a = webhook_setup.a.merchant
    queued = []
    monkeypatch.setattr(tasks.sync_subscription, "delay", lambda merchant_id: queued.append(merchant_id))

    assert deliver(event(), event_id="evt_first").status_code == 200  # committed, task queued
    assert queued == [a.id]

    inserted, release = threading.Event(), threading.Event()
    real_create = BillingEvent.objects.create

    def create_then_hold(**kwargs):
        row = real_create(**kwargs)
        if kwargs["provider_event_id"] == "evt_slow":
            inserted.set()
            assert release.wait(timeout=30), "the slow request was never released"
        return row

    monkeypatch.setattr(BillingEvent.objects, "create", create_then_hold)
    statuses, errors = [], []

    def slow_post():
        try:
            statuses.append(deliver(event(), event_id="evt_slow").status_code)
        except Exception as exc:  # captured for the assertion below
            errors.append(exc)
        finally:
            connection.close()

    slow = threading.Thread(target=slow_post)
    slow.start()
    assert inserted.wait(timeout=30), "the slow request never inserted"
    tasks.sync_subscription(a.id)  # evt_first's task: fetches, settles, marks
    release.set()
    slow.join(timeout=30)

    assert not errors, errors
    assert statuses == [200]
    with tenant_context(a.id), tenant_atomic():
        fetch_started = Subscription.objects.get().provider_synced_at
        slow_row = BillingEvent.objects.get(provider_event_id="evt_slow")
        assert slow_row.created_at < fetch_started  # received before the fetch began...
        assert slow_row.processed_at is None  # ...yet never marked by that sync
        assert BillingEvent.objects.get(provider_event_id="evt_first").processed_at is not None
    assert queued == [a.id, a.id]  # the late commit enqueued its own task
    tasks.sync_subscription(a.id)
    with tenant_context(a.id), tenant_atomic():
        assert BillingEvent.objects.get(provider_event_id="evt_slow").processed_at is not None
