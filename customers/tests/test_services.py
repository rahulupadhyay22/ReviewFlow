"""Unit tests for customers/services.py (spec Definition of done "Sales with
and without a customer phone", get_or_create_customer dedup/race)."""
import threading

import pytest
from django.db import connection

from core.tenancy import tenant_atomic, tenant_context
from customers import services
from customers.models import Customer
from integrations.core.schemas import PayloadValidationError

pytestmark = pytest.mark.django_db


def test_get_or_create_creates_a_new_customer(make_merchant):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        customer, created = services.get_or_create_customer(phone="+919999999999", name="Rahul")
    assert created is True
    assert customer.phone == "+919999999999"
    assert customer.name == "Rahul"
    assert customer.merchant_id == owner.merchant_id


def test_get_or_create_reuses_existing_customer_and_keeps_original_name(make_merchant):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        first, _ = services.get_or_create_customer(phone="+919999999999", name="Rahul")
        second, created = services.get_or_create_customer(phone="+919999999999", name="Someone Else")
    assert created is False
    assert second.id == first.id
    assert second.name == "Rahul"


def test_get_or_create_rejects_invalid_phone(make_merchant):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(PayloadValidationError):
            services.get_or_create_customer(phone="98765", name=None)


def test_customers_are_scoped_per_merchant(make_merchant):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        a_customer, _ = services.get_or_create_customer(phone="+919999999999", name="A")
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        b_customer, created = services.get_or_create_customer(phone="+919999999999", name="B")
    assert created is True
    assert b_customer.id != a_customer.id
    assert b_customer.merchant_id == owner_b.merchant_id


@pytest.mark.django_db(transaction=True)
def test_concurrent_get_or_create_for_same_phone_ends_in_one_customer_row(make_merchant):
    owner = make_merchant("A")
    errors = []
    created_ids = []

    def _run():
        try:
            with tenant_context(owner.merchant_id), tenant_atomic():
                customer, _ = services.get_or_create_customer(phone="+919999999999", name="Race")
                created_ids.append(customer.id)
        except Exception as exc:  # captured for the assertion below
            errors.append(exc)
        finally:
            connection.close()

    t1 = threading.Thread(target=_run)
    t2 = threading.Thread(target=_run)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == []
    assert len(set(created_ids)) == 1
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Customer.objects.filter(phone="+919999999999").count() == 1
