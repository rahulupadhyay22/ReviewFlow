"""RLS backstop for events_integrationevent (Multi-Tenancy.md §Layer 1/2,
"Required Tests" 1-2), plus the Phase 04 suite-wide checks: RLS is
ENABLE+FORCE with a tenant_isolation policy on all five new tables, and
makemigrations --check reports no pending changes (spec Definition of done
"Models, migrations and RLS"). Mirrors locations/tests/test_rls.py."""
import subprocess
import sys

import pytest
from django.conf import settings
from django.db import Error, connection, transaction

from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context
from events import services as events_services
from events.models import IntegrationEvent
from events.tests.conftest import make_payload

pytestmark = pytest.mark.django_db(transaction=True)


def _ids(sql, params=()):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return {str(r[0]) for r in cur.fetchall()}


def _record(owner, integration, external_event_id):
    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(
            integration=integration, external_event_id=external_event_id, payload=make_payload()
        )
        return event


def test_manager_queried_without_tenant_context_raises(make_merchant):
    make_merchant("A")
    with pytest.raises(TenantContextError):
        list(IntegrationEvent.objects.all())


def test_manager_inside_context_returns_only_current_merchants_rows(make_merchant, make_integration):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    int_a = make_integration(owner_a.merchant)
    int_b = make_integration(owner_b.merchant)
    a_event = _record(owner_a, int_a, "1")
    _record(owner_b, int_b, "1")

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        ids = {e.id for e in IntegrationEvent.objects.all()}
    assert ids == {a_event.id}


def test_raw_sql_in_merchant_context_returns_only_that_merchants_events(make_merchant, make_integration):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    int_a = make_integration(owner_a.merchant)
    int_b = make_integration(owner_b.merchant)
    a_event = _record(owner_a, int_a, "1")
    b_event = _record(owner_b, int_b, "1")

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        ids = _ids("SELECT id FROM events_integrationevent")
    assert ids == {str(a_event.id)}
    assert str(b_event.id) not in ids


def test_raw_sql_without_context_returns_no_events(make_merchant, make_integration):
    owner = make_merchant("A")
    integration = make_integration(owner.merchant)
    _record(owner, integration, "1")
    assert _ids("SELECT id FROM events_integrationevent") == set()


def test_raw_insert_for_other_merchant_fails_with_check(make_merchant, make_integration):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    int_a = make_integration(owner_a.merchant)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "INSERT INTO events_integrationevent "
                    "(id, created_at, updated_at, merchant_id, integration_id, source, "
                    "external_event_id, payload, status, attempt_count) "
                    "VALUES (gen_random_uuid(), now(), now(), %s, %s, 'webhook', 'x', '{}', 'RECEIVED', 0)",
                    [str(owner_b.merchant_id), str(int_a.id)],
                )


def test_raw_update_for_other_merchant_fails_with_check(make_merchant, make_integration):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration = make_integration(owner_a.merchant)
    a_event = _record(owner_a, integration, "1")
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE events_integrationevent SET merchant_id = %s WHERE id = %s",
                    [str(owner_b.merchant_id), str(a_event.id)],
                )


# --- Suite-wide catalog checks (Phase 04 Definition of done) ---------------

_TENANT_TABLES = [
    "integrations_integration",
    "integrations_integrationlocationmapping",
    "customers_customer",
    "events_integrationevent",
    "transactions_transaction",
]


def test_all_five_phase_04_tables_have_rls_enabled_forced_and_a_tenant_isolation_policy():
    with connection.cursor() as cur:
        cur.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = ANY(%s)",
            [_TENANT_TABLES],
        )
        rows = {name: (rowsecurity, force) for name, rowsecurity, force in cur.fetchall()}

        cur.execute(
            "SELECT tablename, policyname FROM pg_policies "
            "WHERE tablename = ANY(%s) AND policyname = 'tenant_isolation'",
            [_TENANT_TABLES],
        )
        policies = {tablename for tablename, _ in cur.fetchall()}

    assert set(rows) == set(_TENANT_TABLES)
    for table in _TENANT_TABLES:
        assert rows[table] == (True, True), f"{table} is not ENABLE+FORCE RLS"
        assert table in policies, f"{table} has no tenant_isolation policy"


def test_makemigrations_check_reports_no_pending_changes():
    """The Phase 04 Definition of done requires `makemigrations --check
    --dry-run` to report no changes -- run here so it is enforced in CI,
    not only by hand."""
    result = subprocess.run(
        [sys.executable, "manage.py", "makemigrations", "--check", "--dry-run"],
        cwd=str(settings.BASE_DIR),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
