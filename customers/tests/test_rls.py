"""RLS backstop for customers_customer (Multi-Tenancy.md §Layer 1/2,
"Required Tests" 1-2). Mirrors locations/tests/test_rls.py."""
import pytest
from django.db import Error, connection, transaction

from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context
from customers import services
from customers.models import Customer

pytestmark = pytest.mark.django_db(transaction=True)


def _ids(sql, params=()):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return {str(r[0]) for r in cur.fetchall()}


def _create(merchant_id, phone):
    with tenant_context(merchant_id), tenant_atomic():
        customer, _ = services.get_or_create_customer(phone=phone, name=None)
        return customer


def test_manager_queried_without_tenant_context_raises(make_merchant):
    make_merchant("A")
    with pytest.raises(TenantContextError):
        list(Customer.objects.all())


def test_manager_inside_context_returns_only_current_merchants_rows(make_merchant):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    a_cust = _create(owner_a.merchant_id, "+919999999901")
    _create(owner_b.merchant_id, "+919999999902")

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        ids = {c.id for c in Customer.objects.all()}
    assert ids == {a_cust.id}


def test_raw_sql_in_merchant_context_returns_only_that_merchants_customers(make_merchant):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    a_cust = _create(owner_a.merchant_id, "+919999999901")
    b_cust = _create(owner_b.merchant_id, "+919999999902")

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        ids = _ids("SELECT id FROM customers_customer")
    assert ids == {str(a_cust.id)}
    assert str(b_cust.id) not in ids


def test_raw_sql_without_context_returns_no_customers(make_merchant):
    owner = make_merchant("A")
    _create(owner.merchant_id, "+919999999901")
    assert _ids("SELECT id FROM customers_customer") == set()


def test_raw_insert_for_other_merchant_fails_with_check(make_merchant):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "INSERT INTO customers_customer "
                    "(id, created_at, updated_at, merchant_id, phone, total_transactions, opted_out) "
                    "VALUES (gen_random_uuid(), now(), now(), %s, '+919999999999', 0, false)",
                    [str(owner_b.merchant_id)],
                )


def test_raw_update_for_other_merchant_fails_with_check(make_merchant):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    a_cust = _create(owner_a.merchant_id, "+919999999901")
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE customers_customer SET merchant_id = %s WHERE id = %s",
                    [str(owner_b.merchant_id), str(a_cust.id)],
                )
