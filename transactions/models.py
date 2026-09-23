"""Transaction (Data-Dictionary.md §Transaction; Database-Design.md
§Transaction).

Tenant ownership: direct merchant_id (spec Decision 14 -- Data-Dictionary
is the field-level authority and gives Transaction a non-null merchant_id;
Database-Design's LOCATION label is not followed literally here).

customer is nullable (spec Decision 17): a sale with no customer phone is
still recorded as a COMPLETED Transaction, with customer=NULL. Such a
transaction is never review-eligible (Phase 10).
"""
from django.db import models

from core.managers import TenantScopedManager
from core.models import BaseModel


class Transaction(BaseModel):
    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", "Completed"
        REFUNDED = "REFUNDED", "Refunded"
        VOIDED = "VOIDED", "Voided"

    # RESTRICT, not PROTECT: Database-Design.md -- financial records are
    # never cascade-deleted, and RESTRICT is the documented behavior here
    # specifically (unlike the PROTECT used elsewhere in this phase).
    merchant = models.ForeignKey("accounts.Merchant", on_delete=models.RESTRICT, related_name="+")
    location = models.ForeignKey(
        "locations.Location", on_delete=models.PROTECT, related_name="transactions"
    )
    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transactions",
    )
    integration = models.ForeignKey(
        "integrations.Integration",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transactions",
    )
    external_transaction_id = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    payment_method = models.CharField(max_length=32, null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.COMPLETED)
    occurred_at = models.DateTimeField()

    objects = TenantScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["location", "external_transaction_id"],
                name="uniq_transaction_location_external_txn",
            )
        ]
        indexes = [models.Index(fields=["merchant", "occurred_at"], name="txn_merchant_occurred_idx")]

    def __str__(self):
        return self.external_transaction_id
