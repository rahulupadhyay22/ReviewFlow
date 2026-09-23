"""Customer (Data-Dictionary.md §Customer; Database-Design.md §Customer).

Tenant ownership: MERCHANT. Deliberately thin -- not a CRM. Created only
when a sale supplies a customer phone (spec Decision 17); a sale without
one is still recorded as a Transaction, with customer=NULL.
"""
from django.db import models

from core.managers import TenantScopedManager
from core.models import BaseModel


class Customer(BaseModel):
    merchant = models.ForeignKey(
        "accounts.Merchant", on_delete=models.PROTECT, related_name="customers"
    )
    # E.164, validated before insert (integrations.core.schemas.validate_e164).
    # A Customer row only ever exists when a sale supplied a phone.
    phone = models.CharField(max_length=16)
    name = models.CharField(max_length=255, null=True, blank=True)
    first_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    total_transactions = models.PositiveIntegerField(default=0)
    # Never set in Phase 04 -- opt-out is Phase 08 (inbound WhatsApp / dashboard).
    opted_out = models.BooleanField(default=False)
    opted_out_at = models.DateTimeField(null=True, blank=True)

    objects = TenantScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["merchant", "phone"], name="uniq_customer_merchant_phone")
        ]

    def __str__(self):
        return self.phone
