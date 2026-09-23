"""RLS backstop for transactions_transaction (Multi-Tenancy.md §Layer 1/2,
"Required Tests" 1-2). Mirrors locations/tests/test_rls.py. Includes a
customer-less row (spec Decision 17)."""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

import pytest
from django.db import Error, connection, transaction

from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context
from integrations.core.events import SaleCreated
from transactions import services
from transactions.models import Transaction

pytestmark = pytest.mark.django_db(transaction=True)


def _ids(sql, params=()):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return {str(r[0]) for r in cur.fetchall()}


def _sale(**overrides):
    fields = dict(
        source="webhook",
        external_transaction_id="INV-1",
        amount=Decimal("100.00"),
        currency="INR",
        occurred_at=datetime(2024, 1, 1, tzinfo=dt_timezone.utc),
    )
    fields.update(overrides)
    return SaleCreated(**fields)


def _record(merchant_id, integration, location, **sale_kwargs):
    with tenant_context(merchant_id), tenant_atomic():
        txn, _ = services.record_sale(integration=integration, location=location, sale=_sale(**sale_kwargs))
        return txn


def test_manager_queried_without_tenant_context_raises(make_merchant):
    make_merchant("A")
    with pytest.raises(TenantContextError):
        list(Transaction.objects.all())


def test_manager_inside_context_returns_only_current_merchants_rows_including_customerless(
    make_merchant, make_location, make_integration
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    loc_a = make_location(owner_a.merchant)
    loc_b = make_location(owner_b.merchant)
    int_a = make_integration(owner_a.merchant)
    int_b = make_integration(owner_b.merchant)

    # A's transaction has no customer phone (spec Decision 17).
    a_txn = _record(owner_a.merchant_id, int_a, loc_a)
    _record(owner_b.merchant_id, int_b, loc_b, customer_phone="+919999999999")

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        rows = list(Transaction.objects.all())
    assert {t.id for t in rows} == {a_txn.id}
    assert rows[0].customer_id is None


def test_raw_sql_in_merchant_context_returns_only_that_merchants_transactions(
    make_merchant, make_location, make_integration
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    loc_a = make_location(owner_a.merchant)
    loc_b = make_location(owner_b.merchant)
    int_a = make_integration(owner_a.merchant)
    int_b = make_integration(owner_b.merchant)
    a_txn = _record(owner_a.merchant_id, int_a, loc_a)
    b_txn = _record(owner_b.merchant_id, int_b, loc_b)

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        ids = _ids("SELECT id FROM transactions_transaction")
    assert ids == {str(a_txn.id)}
    assert str(b_txn.id) not in ids


def test_raw_sql_without_context_returns_no_transactions(make_merchant, make_location, make_integration):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    _record(owner.merchant_id, integration, location)
    assert _ids("SELECT id FROM transactions_transaction") == set()


def test_raw_insert_for_other_merchant_fails_with_check(make_merchant, make_location, make_integration):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    loc_a = make_location(owner_a.merchant)
    int_a = make_integration(owner_a.merchant)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "INSERT INTO transactions_transaction "
                    "(id, created_at, updated_at, merchant_id, location_id, integration_id, "
                    "external_transaction_id, amount, currency, status, occurred_at) "
                    "VALUES (gen_random_uuid(), now(), now(), %s, %s, %s, 'X', 1, 'INR', 'COMPLETED', now())",
                    [str(owner_b.merchant_id), str(loc_a.id), str(int_a.id)],
                )


def test_raw_update_for_other_merchant_fails_with_check(make_merchant, make_location, make_integration):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    loc_a = make_location(owner_a.merchant)
    int_a = make_integration(owner_a.merchant)
    a_txn = _record(owner_a.merchant_id, int_a, loc_a)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE transactions_transaction SET merchant_id = %s WHERE id = %s",
                    [str(owner_b.merchant_id), str(a_txn.id)],
                )
