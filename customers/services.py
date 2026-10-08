"""Customer services (Coding-Standards.md §1)."""
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.tenancy import get_current_merchant_id, tenant_atomic
from customers.exceptions import CustomerNotFound
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


OPT_OUT_SOURCES = frozenset({"INBOUND_KEYWORD", "MANUAL"})


def get_customer(customer_id) -> Customer:
    """The current merchant's customer, or CustomerNotFound (404) for an
    unknown id or another merchant's (TenantScopedManager and RLS hide those)."""
    with tenant_atomic():
        customer = Customer.objects.filter(pk=customer_id).first()
    if customer is None:
        raise CustomerNotFound()
    return customer


def opt_out_customer(*, phone: str | None = None, customer: Customer | None = None, source: str, actor=None):
    """Opt a customer out of the current merchant's review requests
    (Business-Rules §3). Returns the Customer, or None when no Customer exists
    for the phone (a Customer exists only from a sale, so a phone with none
    was never messaged -- none is created).

    A conditional UPDATE ... WHERE opted_out = false, so two concurrent STOPs
    are safe and opted_out_at keeps the first time. Never deletes history.
    `actor` is accepted for the spec's signature; a manual opt-out writes no
    AuditLog row (spec 08 OD-7)."""
    if source not in OPT_OUT_SOURCES:
        raise ValueError("Unknown opt-out source.")
    if (phone is None) == (customer is None):
        raise ValueError("Pass exactly one of phone or customer.")
    with tenant_atomic():
        queryset = Customer.objects.filter(pk=customer.pk) if customer is not None else Customer.objects.filter(phone=phone)
        now = timezone.now()
        queryset.filter(opted_out=False).update(opted_out=True, opted_out_at=now, updated_at=now)
        return queryset.first()
