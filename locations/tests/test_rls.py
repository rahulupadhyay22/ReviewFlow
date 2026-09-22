"""RLS backstop for locations_location (Multi-Tenancy.md §Layer 1/2,
"Required Tests" 1-2). SET LOCAL semantics need real transaction boundaries,
so these use transaction=True. Mirrors accounts/tests/test_rls.py."""
import pytest
from django.db import Error, connection, transaction

from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context
from locations.models import Location

pytestmark = pytest.mark.django_db(transaction=True)


def _ids(sql, params=()):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return {str(r[0]) for r in cur.fetchall()}


def _create(merchant_id, name):
    with tenant_context(merchant_id), tenant_atomic():
        from locations import services

        return services.create_location(name=name)


def test_manager_queried_without_tenant_context_raises(make_merchant):
    make_merchant("A")
    with pytest.raises(TenantContextError):
        list(Location.objects.all())


def test_manager_inside_context_returns_only_current_merchants_rows(make_merchant):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    a_loc = _create(owner_a.merchant_id, "A loc")
    _create(owner_b.merchant_id, "B loc")

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        ids = {loc.id for loc in Location.objects.all()}
    assert ids == {a_loc.id}


def test_raw_sql_in_merchant_context_returns_only_that_merchants_locations(make_merchant):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    a_loc = _create(owner_a.merchant_id, "A loc")
    b_loc = _create(owner_b.merchant_id, "B loc")

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        ids = _ids("SELECT id FROM locations_location")
    assert ids == {str(a_loc.id)}
    assert str(b_loc.id) not in ids


def test_raw_sql_without_context_returns_no_locations(make_merchant):
    owner = make_merchant("A")
    _create(owner.merchant_id, "A loc")
    assert _ids("SELECT id FROM locations_location") == set()


def test_raw_insert_for_other_merchant_fails_with_check(make_merchant):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "INSERT INTO locations_location "
                    "(id, created_at, updated_at, merchant_id, name, is_active) "
                    "VALUES (gen_random_uuid(), now(), now(), %s, 'Intruder', true)",
                    [str(owner_b.merchant_id)],
                )


def test_raw_update_for_other_merchant_fails_with_check(make_merchant):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    a_loc = _create(owner_a.merchant_id, "A loc")
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE locations_location SET merchant_id = %s WHERE id = %s",
                    [str(owner_b.merchant_id), str(a_loc.id)],
                )
