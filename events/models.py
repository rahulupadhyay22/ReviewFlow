"""IntegrationEvent -- the event inbox (Data-Dictionary.md
§IntegrationEvent; Database-Design.md §IntegrationEvent).

Tenant ownership: MERCHANT, direct and non-null -- merchant_id is resolved
from Integration.merchant_id before this row is ever created
(Multi-Tenancy.md §"IntegrationEvent -- merchant identified before
creation, not after").

attempt_count, error_code and error_message are additions for documented
behavior (spec Decision 6/19), not in the original Data-Dictionary.
CANCELLED is an additional terminal status (spec Decision 18): disconnecting
an integration moves its pending RECEIVED/FAILED events there. Rows are
never deleted.
"""
from django.db import models

from core.managers import TenantScopedManager
from core.models import BaseModel


class IntegrationEvent(BaseModel):
    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        PROCESSED = "PROCESSED", "Processed"
        FAILED = "FAILED", "Failed"
        DEAD_LETTER = "DEAD_LETTER", "Dead letter"
        CANCELLED = "CANCELLED", "Cancelled"

    merchant = models.ForeignKey("accounts.Merchant", on_delete=models.PROTECT, related_name="+")
    integration = models.ForeignKey(
        "integrations.Integration", on_delete=models.PROTECT, related_name="events"
    )
    # Denormalized from Integration.provider for fast filtering/logging.
    source = models.CharField(max_length=32)
    external_event_id = models.CharField(max_length=255)
    location = models.ForeignKey(
        "locations.Location", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    # Raw inbound payload (JSON-decoded). Never logged; purged at 90 days in
    # Phase 15 (Privacy-Data-Retention.md).
    payload = models.JSONField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RECEIVED)
    received_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    # Both fields come only from events.services.SAFE_ERRORS -- never
    # exception text, payload values, phone numbers, credentials or tokens
    # (spec Decision 19).
    error_code = models.CharField(max_length=64, null=True, blank=True)
    error_message = models.CharField(max_length=255, null=True, blank=True)

    objects = TenantScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "external_event_id"],
                name="uniq_integrationevent_integration_external_event",
            )
        ]
        indexes = [
            models.Index(fields=["status", "received_at"], name="ievent_status_recv_idx")
        ]

    def __str__(self):
        return f"{self.source}:{self.external_event_id}"
