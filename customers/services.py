"""Customer services (Coding-Standards.md §1)."""
from django.db import IntegrityError, transaction

from core.tenancy import get_current_merchant_id, tenant_atomic
from customers.models import Customer
from integrations.core.schemas import validate_e164


def get_or_create_customer(*, phone: str, name: str | None) -> tuple[Customer, bool]:
    """merchant_id comes from the tenant context. Handles the
    UNIQUE(merchant_id, phone) race through a savepoint and IntegrityError,
    then re-reads -- never check-then-insert. name is set only on create."""
    validate_e164(phone)  # defense in depth: SaleCreated already validated this.
    merchant_id = get_current_merchant_id()
    with tenant_atomic():
        try:
            with transaction.atomic():
                customer = Customer.objects.create(merchant_id=merchant_id, phone=phone, name=name)
                return customer, True
        except IntegrityError:
            return Customer.objects.get(merchant_id=merchant_id, phone=phone), False
