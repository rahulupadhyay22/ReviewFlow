"""Unit tests for transactions/services.py (spec Definition of done "Sales
with and without a customer phone", counter updates, MANAGER scoping)."""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

import pytest

from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context
from customers.models import Customer
from integrations.core.events import SaleCreated
from transactions import services
from transactions.exceptions import TransactionNotFound
from transactions.models import Transaction

pytestmark = pytest.mark.django_db


def _sale(**overrides):
    fields = dict(
        source="webhook",
        external_transaction_id="INV-1",
        amount=Decimal("100.00"),
        currency="INR",
        occurred_at=datetime(2024, 1, 1, 10, 0, tzinfo=dt_timezone.utc),
    )
    fields.update(overrides)
    return SaleCreated(**fields)


# --- record_sale: with/without customer -------------------------------------


def test_record_sale_with_phone_creates_customer_and_transaction(make_merchant, make_location, make_integration):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        txn, created = services.record_sale(
            integration=integration, location=location, sale=_sale(customer_phone="+919999999999", customer_name="Rahul")
        )
        assert created is True
        assert txn.status == Transaction.Status.COMPLETED
        assert txn.customer is not None
        assert txn.customer.phone == "+919999999999"
        assert txn.customer.total_transactions == 1
        assert txn.customer.first_seen_at == txn.occurred_at
        assert txn.customer.last_seen_at == txn.occurred_at


def test_record_sale_without_phone_creates_transaction_with_no_customer(make_merchant, make_location, make_integration):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        txn, created = services.record_sale(integration=integration, location=location, sale=_sale())
        assert created is True
        assert txn.customer is None
        assert txn.status == Transaction.Status.COMPLETED
        assert Customer.objects.count() == 0


def test_record_sale_later_sale_with_phone_still_creates_customer(make_merchant, make_location, make_integration):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.record_sale(integration=integration, location=location, sale=_sale(external_transaction_id="INV-1"))
        txn2, created2 = services.record_sale(
            integration=integration,
            location=location,
            sale=_sale(external_transaction_id="INV-2", customer_phone="+919999999999"),
        )
    assert created2 is True
    assert txn2.customer is not None
    assert txn2.customer.total_transactions == 1


# --- duplicate transaction is a no-op ---------------------------------------


def test_record_sale_duplicate_external_transaction_id_is_no_op(make_merchant, make_location, make_integration):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        first, created1 = services.record_sale(
            integration=integration, location=location, sale=_sale(customer_phone="+919999999999")
        )
        second, created2 = services.record_sale(
            integration=integration, location=location, sale=_sale(customer_phone="+919999999999")
        )
    assert created1 is True
    assert created2 is False
    assert first.id == second.id
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Transaction.objects.count() == 1
        assert Customer.objects.get(phone="+919999999999").total_transactions == 1


# --- counter updates ---------------------------------------------------


def test_record_sale_widens_first_and_last_seen_at(make_merchant, make_location, make_integration):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    early = datetime(2024, 1, 1, tzinfo=dt_timezone.utc)
    late = datetime(2024, 6, 1, tzinfo=dt_timezone.utc)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.record_sale(
            integration=integration,
            location=location,
            sale=_sale(external_transaction_id="INV-1", customer_phone="+919999999999", occurred_at=late),
        )
        services.record_sale(
            integration=integration,
            location=location,
            sale=_sale(external_transaction_id="INV-2", customer_phone="+919999999999", occurred_at=early),
        )
        customer = Customer.objects.get(phone="+919999999999")
    assert customer.total_transactions == 2
    assert customer.first_seen_at == early
    assert customer.last_seen_at == late


# --- merchant invariants -------------------------------------------------


def test_record_sale_raises_when_location_belongs_to_another_merchant(make_merchant, make_location, make_integration):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    location_b = make_location(owner_b.merchant)
    integration_a = make_integration(owner_a.merchant)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(TenantContextError):
            services.record_sale(integration=integration_a, location=location_b, sale=_sale())


# --- list_transactions / get_transaction ------------------------------------


def test_list_transactions_scoped_to_merchant(make_merchant, make_location, make_integration):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    loc_a = make_location(owner_a.merchant)
    loc_b = make_location(owner_b.merchant)
    int_a = make_integration(owner_a.merchant)
    int_b = make_integration(owner_b.merchant)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        txn_a, _ = services.record_sale(integration=int_a, location=loc_a, sale=_sale())
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        services.record_sale(integration=int_b, location=loc_b, sale=_sale())

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        ids = {t.id for t in services.list_transactions(owner_a)}
    assert ids == {txn_a.id}


def test_manager_only_sees_transactions_at_assigned_locations(
    make_merchant, add_member, assign_locations, make_location, make_integration
):
    owner = make_merchant("A")
    loc1 = make_location(owner.merchant, name="L1")
    loc2 = make_location(owner.merchant, name="L2")
    integration = make_integration(owner.merchant)
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    assign_locations(owner, manager, [loc1])

    with tenant_context(owner.merchant_id), tenant_atomic():
        txn1, _ = services.record_sale(
            integration=integration, location=loc1, sale=_sale(external_transaction_id="INV-1")
        )
        txn2, _ = services.record_sale(
            integration=integration, location=loc2, sale=_sale(external_transaction_id="INV-2")
        )

        visible = {t.id for t in services.list_transactions(manager)}
        assert visible == {txn1.id}

        assert services.get_transaction(manager, txn1.id).id == txn1.id
        with pytest.raises(TransactionNotFound):
            services.get_transaction(manager, txn2.id)


@pytest.mark.django_db(transaction=True)
def test_record_sale_failure_does_not_leave_a_committed_customer_without_an_outer_atomic(
    make_merchant, make_location, make_integration, monkeypatch
):
    """record_sale must be atomic on its own, not only when a caller already
    opened a transaction. Called standalone (no enclosing atomic block), a
    failed Transaction insert must roll the Customer back with it -- never
    leave a committed Customer with no sale (Coding-Standards.md §4).

    transaction=True means no surrounding test transaction, so a partial
    commit would genuinely survive and be visible here.
    """
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(Transaction.objects, "create", boom)

    # No outer tenant_atomic(): only tenant_context, so record_sale owns the
    # transaction boundary itself.
    with tenant_context(owner.merchant_id):
        with pytest.raises(RuntimeError):
            services.record_sale(
                integration=integration,
                location=location,
                sale=_sale(customer_phone="+919999999999", customer_name="Rahul"),
            )

    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Customer.objects.count() == 0
        assert Transaction.objects.count() == 0


def test_get_transaction_raises_for_cross_merchant_id(make_merchant, make_location, make_integration):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    loc_b = make_location(owner_b.merchant)
    int_b = make_integration(owner_b.merchant)
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        txn_b, _ = services.record_sale(integration=int_b, location=loc_b, sale=_sale())
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(TransactionNotFound):
            services.get_transaction(owner_a, txn_b.id)
