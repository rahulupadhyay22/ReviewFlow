"""Quota reservations racing a late-charge reactivation (spec 07 DoD "Quota
primitive" concurrency items and "Expiry versus sync" rule 3), real threads and
real transactions.

The outcome is asserted as invariants that hold in every interleaving: an
EXPIRED row is never reservable, so each reservation is either NOT_ENTITLED
(before the reactivation committed) or RESERVED against the NEW period's
UsageRecord (after it); the old period's record is never touched; and the new
record's requests_used equals the number of RESERVED results exactly. No
sleeps: threads are released together by a Barrier and joined with a bound."""
import threading
from datetime import timedelta

import pytest

from billing import services, tasks
from billing.models import UsageRecord
from billing.services import ReservationResult as R
from billing.tests.snapshot_helpers import END, NOW, START, entity, invoice
from billing.tests.state_helpers import read_state
from core.tenancy import tenant_atomic, tenant_context
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)

NEW_START = NOW - timedelta(days=1)
NEW_END = NEW_START + timedelta(days=30)
DEADLINE = 60
RESERVERS, ATTEMPTS = 6, 3


def test_reservations_racing_a_late_charge_reactivation_only_ever_land_on_the_new_period(lifecycle_setup):
    w = lifecycle_setup
    w.sub(w.a, "EXPIRED", provider_status="halted")
    mid, ref = w.a.merchant.id, w.ref(w.a)
    with tenant_context(mid), tenant_atomic():
        UsageRecord.objects.update(requests_used=7)
    w.provider.entities[ref] = entity(w.small, status="active", start=NEW_START, ref=ref)
    w.provider.invoices = [invoice(start=NEW_START, subscription_id=ref)]

    barrier = threading.Barrier(RESERVERS + 1, timeout=DEADLINE)
    results, errors = [], []

    def reserver():
        try:
            barrier.wait()
            for _ in range(ATTEMPTS):
                with tenant_context(mid), tenant_atomic():
                    results.append(services.reserve_quota_unit())
        except Exception as exc:  # captured and asserted below
            errors.append(exc)
        finally:
            connection.close()

    def reactivator():
        try:
            barrier.wait()
            tasks.maintain_subscription(mid)
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=reserver) for _ in range(RESERVERS)]
    threads.append(threading.Thread(target=reactivator))
    for t in threads:
        t.start()
    for t in threads:
        t.join(DEADLINE)
    assert not any(t.is_alive() for t in threads)
    assert not errors, errors

    assert len(results) == RESERVERS * ATTEMPTS
    assert set(results) <= {R.RESERVED, R.NOT_ENTITLED}  # quota is 100: never exhausted
    state = read_state(w.a.merchant)
    assert state.status == "ACTIVE" and state.period == (NEW_START, NEW_END)
    assert state.usages == [(START, END, 7), (NEW_START, NEW_END, results.count(R.RESERVED))]
