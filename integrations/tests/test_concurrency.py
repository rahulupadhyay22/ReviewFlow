"""Concurrency tests for the Integration row lock (spec Decision 20).

Mirrors the barrier/thread pattern in
accounts/tests/test_member_locations.py. SET LOCAL semantics need real
transaction boundaries, so these use transaction=True."""
import threading

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.tenancy import tenant_atomic, tenant_context
from integrations import services

pytestmark = pytest.mark.django_db(transaction=True)


def _connect(owner, provider="webhook", **kwargs):
    with tenant_context(owner.merchant_id), tenant_atomic():
        return services.connect_integration(actor=owner, provider=provider, **kwargs)


def test_replace_location_mappings_locks_integration_before_mapping_queries(
    make_merchant, make_location, register_webhook_provider
):
    """A service-level check, not just a behavioral one: the FOR NO KEY
    UPDATE lock on integrations_integration is issued before any query
    against integrations_integrationlocationmapping."""
    owner = make_merchant("A")
    integration = _connect(owner)
    location = make_location(owner.merchant)

    with tenant_context(owner.merchant_id), tenant_atomic():
        with CaptureQueriesContext(connection) as ctx:
            services.replace_location_mappings(integration, mappings=[{"location_id": location.id}])

    queries = [q["sql"].upper() for q in ctx.captured_queries]
    lock_indices = [
        i for i, sql in enumerate(queries) if "INTEGRATIONS_INTEGRATION" in sql and "FOR NO KEY UPDATE" in sql
    ]
    mapping_indices = [i for i, sql in enumerate(queries) if "INTEGRATIONS_INTEGRATIONLOCATIONMAPPING" in sql]

    assert lock_indices, "expected a FOR NO KEY UPDATE lock on integrations_integration"
    assert mapping_indices, "expected at least one mapping query"
    assert max(lock_indices) < min(mapping_indices)


def test_concurrent_replace_calls_are_serialized_by_the_integration_lock(
    make_merchant, make_location, register_webhook_provider
):
    """Holds a real FOR NO KEY UPDATE lock on the Integration row from
    outside the service, then proves a concurrent replace_location_mappings
    call is blocked until that lock is released, and completes cleanly
    afterward with no IntegrityError and the expected final state."""
    owner = make_merchant("A")
    integration = _connect(owner)
    loc1 = make_location(owner.merchant, name="L1")
    loc2 = make_location(owner.merchant, name="L2")

    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_lock():
        with tenant_context(owner.merchant_id):
            from django.db import transaction as dj_transaction

            with dj_transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute("SET LOCAL app.current_merchant_id = %s", [str(owner.merchant_id)])
                    cur.execute(
                        "SELECT id FROM integrations_integration WHERE id = %s FOR NO KEY UPDATE",
                        [str(integration.id)],
                    )
                lock_acquired.set()
                release_lock.wait(timeout=5)
        connection.close()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert lock_acquired.wait(timeout=5)

    errors = []

    def attempt_replace():
        try:
            with tenant_context(owner.merchant_id), tenant_atomic():
                services.replace_location_mappings(integration, mappings=[{"location_id": loc2.id}])
        except Exception as exc:  # captured for the assertion below
            errors.append(exc)
        finally:
            connection.close()

    replacer = threading.Thread(target=attempt_replace)
    replacer.start()

    # The replacer must still be blocked on the row lock held by hold_lock()
    # -- this is what proves the lock actually serializes the mutation,
    # not just that both calls happen to succeed.
    replacer.join(timeout=1)
    assert replacer.is_alive()

    release_lock.set()
    replacer.join(timeout=5)
    assert not replacer.is_alive()
    holder.join(timeout=5)

    assert errors == []
    with tenant_context(owner.merchant_id), tenant_atomic():
        remaining = set(integration.location_mappings.values_list("location_id", flat=True))
    assert remaining == {loc2.id}
