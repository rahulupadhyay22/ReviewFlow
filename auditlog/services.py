import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import models

from accounts.models import Merchant, TeamMember, User
from auditlog.models import AuditLog
from core.exceptions import ReviewFlowError, TenantContextError
from core.tenancy import get_current_merchant_id, get_current_platform_write, tenant_atomic, tenant_context

# Platform-side (Audit-Logging.md): staff access to one merchant's data.
AUDIT_STAFF_DATA_VIEWED = "staff.merchant_data_viewed"


class ActorNotMember(ReviewFlowError):
    """The actor has no accepted membership in the current merchant."""

    http_status = 403
    code = "actor_not_member"


class StaffAccessDenied(ReviewFlowError):
    """A privileged read was attempted by someone who is not an active staff
    user."""

    http_status = 403
    code = "staff_access_denied"


def record(
    action: str,
    *,
    actor: User | None = None,
    target: models.Model | None = None,
    metadata: dict | None = None,
) -> AuditLog:
    """Write one merchant-scoped audit row. The merchant always comes from the
    tenant context, never an argument. Audit rows have no update/delete path."""
    merchant_id = get_current_merchant_id()
    if merchant_id is None:
        raise TenantContextError("audit record() requires a tenant context.")
    with tenant_atomic():
        if actor is not None and not TeamMember.objects.filter(
            user=actor, accepted_at__isnull=False
        ).exists():
            raise ActorNotMember("Audit actor is not a member of the current merchant.")
        return AuditLog.objects.create(
            merchant_id=merchant_id,
            actor_user=actor,
            action=action,
            target_type=target._meta.label_lower if target is not None else None,
            target_id=str(target.pk) if target is not None else None,
            metadata_json=metadata,
        )


def record_platform(
    action: str,
    *,
    actor: User | None = None,
    target: models.Model | None = None,
    metadata: dict | None = None,
) -> AuditLog:
    """Write one platform-level audit row (merchant_id NULL; spec 08 Change 1).

    Only valid inside core.tenancy.platform_write_atomic(): the INSERT passes
    the platform_insert policy only there. AuditLog.objects needs a tenant
    context, so the row is created through the plain base manager -- still
    RLS-gated. No membership check: the actor is platform staff, if any."""
    if get_current_platform_write() is None:
        raise TenantContextError("record_platform() requires platform_write_atomic().")
    return AuditLog._base_manager.create(
        merchant_id=None,
        actor_user=actor,
        action=action,
        target_type=target._meta.label_lower if target is not None else None,
        target_id=str(target.pk) if target is not None else None,
        metadata_json=metadata,
    )


@contextmanager
def staff_merchant_read(*, staff_user: User, merchant_id: uuid.UUID | str, resource: str) -> Iterator[Merchant]:
    """The audited privileged path for staff reads of ONE merchant's tenant
    data (Multi-Tenancy.md "No Standing Privileged Role"; spec 16 D1). No RLS
    bypass: the read runs under that merchant's own tenant_context and
    tenant_isolation policy.

    1. The audit row (staff actor, the merchant as target, metadata
       {"resource"} only) is written and COMMITTED in its own durable tenant
       transaction before any tenant data is read. If that insert fails,
       nothing is read; if the read fails, the row stays (the access was
       attempted).
    2. The caller reads and materializes its data inside the second durable
       tenant transaction this yields.

    Both transactions are durable: inside any other atomic block Django
    raises RuntimeError before anything happens, so neither can become a
    savepoint whose SET LOCAL outlives it. tenant_context resets on exit and
    on exceptions. record() is not used: it requires the actor to be a
    member of the merchant, which staff are not.

    Yields the Merchant fetched for the audit row (a global, non-RLS row), so
    callers need no second lookup."""
    if not (staff_user.is_active and staff_user.is_staff):
        raise StaffAccessDenied("Only an active staff user can read merchant data.")
    if get_current_merchant_id() is not None:
        raise TenantContextError("staff_merchant_read() must not run inside a tenant context.")
    # Merchant is a global, non-RLS table: reading it is not a bypass.
    merchant = Merchant.objects.get(pk=merchant_id)
    with tenant_context(merchant.id):
        with tenant_atomic(durable=True):
            AuditLog.objects.create(
                merchant_id=merchant.id,
                actor_user=staff_user,
                action=AUDIT_STAFF_DATA_VIEWED,
                target_type=merchant._meta.label_lower,
                target_id=str(merchant.pk),
                metadata_json={"resource": resource},
            )
        with tenant_atomic(durable=True):
            yield merchant
