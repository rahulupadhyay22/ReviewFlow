from django.db import models

from accounts.models import TeamMember, User
from auditlog.models import AuditLog
from core.exceptions import ReviewFlowError, TenantContextError
from core.tenancy import get_current_merchant_id, get_current_platform_write, tenant_atomic


class ActorNotMember(ReviewFlowError):
    """The actor has no accepted membership in the current merchant."""

    http_status = 403
    code = "actor_not_member"


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
