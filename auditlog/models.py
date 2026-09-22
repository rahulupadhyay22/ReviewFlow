import uuid

from django.conf import settings
from django.db import models

from core.managers import TenantScopedManager


class AuditLog(models.Model):
    """Privileged/administrative actions (Audit-Logging.md). Never updated,
    so no updated_at. merchant is nullable for platform-level rows, which only
    the Phase 16 privileged path writes (RLS hides and rejects them here)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    merchant = models.ForeignKey(
        "accounts.Merchant", null=True, on_delete=models.RESTRICT, related_name="+"
    )
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.RESTRICT, related_name="+"
    )
    action = models.CharField(max_length=100)
    target_type = models.CharField(max_length=100, null=True)
    target_id = models.CharField(max_length=64, null=True)
    # Never phone numbers, tokens, message bodies or raw payloads.
    metadata_json = models.JSONField(null=True)

    objects = TenantScopedManager()

    class Meta:
        indexes = [models.Index(fields=["merchant", "created_at"])]
