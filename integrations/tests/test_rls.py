"""RLS backstop for integrations_integration and
integrations_integrationlocationmapping (Multi-Tenancy.md §Layer 1/2,
"Required Tests" 1-2). Mirrors locations/tests/test_rls.py."""
import pytest
from django.db import Error, connection, transaction

from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context
from integrations import services
from integrations.models import Integration, IntegrationLocationMapping

pytestmark = pytest.mark.django_db(transaction=True)


def _ids(sql, params=()):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return {str(r[0]) for r in cur.fetchall()}


def _connect(owner):
    with tenant_context(owner.merchant_id), tenant_atomic():
        return services.connect_integration(actor=owner, provider="webhook")


def _map(owner, integration, location):
    with tenant_context(owner.merchant_id), tenant_atomic():
        return services.add_location_mapping(integration, location_id=location.id)


# --- Integration -------------------------------------------------------


def test_integration_manager_queried_without_tenant_context_raises(make_merchant):
    make_merchant("A")
    with pytest.raises(TenantContextError):
        list(Integration.objects.all())


def test_integration_manager_inside_context_returns_only_current_merchants_rows(
    make_merchant, register_webhook_provider
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    a_int = _connect(owner_a)
    _connect(owner_b)

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        ids = {i.id for i in Integration.objects.all()}
    assert ids == {a_int.id}


def test_integration_raw_sql_in_merchant_context_returns_only_that_merchants_rows(
    make_merchant, register_webhook_provider
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    a_int = _connect(owner_a)
    b_int = _connect(owner_b)

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        ids = _ids("SELECT id FROM integrations_integration")
    assert ids == {str(a_int.id)}
    assert str(b_int.id) not in ids


def test_integration_raw_sql_without_context_returns_no_rows(make_merchant, register_webhook_provider):
    owner = make_merchant("A")
    _connect(owner)
    assert _ids("SELECT id FROM integrations_integration") == set()


def test_integration_raw_insert_for_other_merchant_fails_with_check(make_merchant):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "INSERT INTO integrations_integration "
                    "(id, created_at, updated_at, merchant_id, provider, status) "
                    "VALUES (gen_random_uuid(), now(), now(), %s, 'webhook', 'CONNECTED')",
                    [str(owner_b.merchant_id)],
                )


def test_integration_raw_update_for_other_merchant_fails_with_check(make_merchant, register_webhook_provider):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    a_int = _connect(owner_a)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE integrations_integration SET merchant_id = %s WHERE id = %s",
                    [str(owner_b.merchant_id), str(a_int.id)],
                )


# --- IntegrationLocationMapping -----------------------------------------


def test_mapping_manager_queried_without_tenant_context_raises(make_merchant):
    make_merchant("A")
    with pytest.raises(TenantContextError):
        list(IntegrationLocationMapping.objects.all())


def test_mapping_manager_inside_context_returns_only_current_merchants_rows(
    make_merchant, make_location, register_webhook_provider
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    a_int = _connect(owner_a)
    b_int = _connect(owner_b)
    a_loc = make_location(owner_a.merchant)
    b_loc = make_location(owner_b.merchant)
    a_map = _map(owner_a, a_int, a_loc)
    _map(owner_b, b_int, b_loc)

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        ids = {m.id for m in IntegrationLocationMapping.objects.all()}
    assert ids == {a_map.id}


def test_mapping_raw_sql_without_context_returns_no_rows(
    make_merchant, make_location, register_webhook_provider
):
    owner = make_merchant("A")
    integration = _connect(owner)
    location = make_location(owner.merchant)
    _map(owner, integration, location)
    assert _ids("SELECT id FROM integrations_integrationlocationmapping") == set()


def test_mapping_raw_insert_for_other_merchant_fails_with_check(
    make_merchant, make_location, register_webhook_provider
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    a_int = _connect(owner_a)
    a_loc = make_location(owner_a.merchant)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "INSERT INTO integrations_integrationlocationmapping "
                    "(id, created_at, updated_at, merchant_id, integration_id, location_id, is_active) "
                    "VALUES (gen_random_uuid(), now(), now(), %s, %s, %s, true)",
                    [str(owner_b.merchant_id), str(a_int.id), str(a_loc.id)],
                )
