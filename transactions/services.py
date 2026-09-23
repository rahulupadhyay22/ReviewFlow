"""Transaction services (Coding-Standards.md §1)."""
import uuid
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, QuerySet
from django.db.models.functions import Coalesce, Greatest, Least

from accounts.models import TeamMember
from core.exceptions import TenantContextError
from core.tenancy import get_current_merchant_id, tenant_atomic
from customers.services import get_or_create_customer
from integrations.core.events import SaleCreated
from integrations.models import Integration
from locations.models import Location
from locations.services import accessible_locations
from transactions.exceptions import TransactionNotFound
from transactions.models import Transaction


def record_sale(*, integration: Integration, location: Location, sale: SaleCreated) -> tuple[Transaction, bool]:
    """Records one normalized sale. A sale with no customer phone is still
    recorded, with customer=None (spec Decision 17) -- no Customer row is
    created for it. A duplicate (location, external_transaction_id) is a
    no-op that returns the existing row."""
    merchant_id = get_current_merchant_id()

    # One transaction for the whole sale: customer get-or-create, the
    # Transaction insert and the counter update either all commit or all roll
    # back. This must not depend on a caller already having opened an atomic
    # block -- called standalone, a committed Customer with no Transaction
    # would be a partial write (Coding-Standards.md §4).
    with tenant_atomic():
        customer = None
        if sale.customer_phone:
            customer, _ = get_or_create_customer(phone=sale.customer_phone, name=sale.customer_name)

        # Defensive invariant (spec Decision 14), not client input validation --
        # a mismatch here means a tenant-context bug upstream, not a bad request.
        if location.merchant_id != merchant_id or integration.merchant_id != merchant_id:
            raise TenantContextError(
                "record_sale() location/integration does not match the tenant context."
            )
        if customer is not None and customer.merchant_id != merchant_id:
            raise TenantContextError("record_sale() customer does not match the tenant context.")

        try:
            with transaction.atomic():
                txn = Transaction.objects.create(
                    merchant_id=merchant_id,
                    location=location,
                    customer=customer,
                    integration=integration,
                    external_transaction_id=sale.external_transaction_id,
                    amount=sale.amount,
                    currency=sale.currency,
                    payment_method=sale.payment_method,
                    status=Transaction.Status.COMPLETED,
                    occurred_at=sale.occurred_at,
                )
                created = True
        except IntegrityError:
            txn = Transaction.objects.get(
                location=location, external_transaction_id=sale.external_transaction_id
            )
            created = False

        if created and customer is not None:
            from customers.models import Customer

            Customer.objects.filter(pk=customer.pk).update(
                total_transactions=F("total_transactions") + 1,
                first_seen_at=Least(Coalesce(F("first_seen_at"), sale.occurred_at), sale.occurred_at),
                last_seen_at=Greatest(Coalesce(F("last_seen_at"), sale.occurred_at), sale.occurred_at),
            )
            # .update() bypasses the in-memory instance -- refresh it so
            # txn.customer (the same object) reflects the new counters.
            customer.refresh_from_db()

        return txn, created


def list_transactions(
    actor: TeamMember,
    *,
    location_id: uuid.UUID | str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    status: str | None = None,
) -> QuerySet[Transaction]:
    qs = Transaction.objects.filter(location__in=accessible_locations(actor)).select_related("customer")
    if location_id is not None:
        qs = qs.filter(location_id=location_id)
    if date_from is not None:
        qs = qs.filter(occurred_at__gte=date_from)
    if date_to is not None:
        qs = qs.filter(occurred_at__lte=date_to)
    if status is not None:
        qs = qs.filter(status=status)
    return qs.order_by("-created_at")


def get_transaction(actor: TeamMember, transaction_id: uuid.UUID | str) -> Transaction:
    try:
        return (
            Transaction.objects.filter(location__in=accessible_locations(actor))
            .select_related("customer")
            .get(pk=transaction_id)
        )
    except (Transaction.DoesNotExist, ValueError, TypeError, ValidationError):
        raise TransactionNotFound() from None
