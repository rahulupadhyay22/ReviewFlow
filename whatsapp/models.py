"""WhatsApp models (spec 08; Data-Dictionary.md §WhatsAppAccount,
§WhatsAppLocationMapping, §MessageTemplate). WhatsAppMessage is Phase 11.

WhatsAppAccount is MERCHANT-owned for OWN_NUMBER rows and GLOBAL
(merchant_id NULL) for the single SHARED_POOL row; its RLS is the asymmetric
set of spec 08 Change 1 (core.rls.rls_shared_pool).
"""
from django.db import models
from django.db.models import Q

from core.exceptions import TenantContextError
from core.managers import TenantScopedManager
from core.models import BaseModel
from core.tenancy import get_current_merchant_id


class WhatsAppAccountManager(TenantScopedManager):
    """Mirrors the tenant_read policy: the current merchant's rows plus the
    shared (merchant NULL) row. Needs a tenant context, except shared_pool()."""

    def get_queryset(self):
        merchant_id = get_current_merchant_id()
        if merchant_id is None:
            raise TenantContextError("WhatsAppAccount queried without a tenant context.")
        return models.Manager.get_queryset(self).filter(
            Q(merchant_id=merchant_id) | Q(merchant__isnull=True)
        )

    def shared_pool(self):
        """The platform's SHARED_POOL row(s); the only read that works without
        a tenant context (the shared row is readable under tenant_read)."""
        return models.Manager.get_queryset(self).filter(
            sender_type=WhatsAppAccount.SenderType.SHARED_POOL, merchant__isnull=True
        )


class WhatsAppAccount(BaseModel):
    class SenderType(models.TextChoices):
        OWN_NUMBER = "OWN_NUMBER", "Own number"
        SHARED_POOL = "SHARED_POOL", "Shared pool"

    class Provider(models.TextChoices):
        # Lowercase provider identifier, like Integration.Provider.
        META_CLOUD = "meta_cloud", "Meta Cloud API"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        PENDING = "PENDING", "Pending"
        SUSPENDED = "SUSPENDED", "Suspended"

    merchant = models.ForeignKey(
        "accounts.Merchant",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="whatsapp_accounts",
    )
    sender_type = models.CharField(max_length=16, choices=SenderType.choices)
    provider = models.CharField(max_length=16, choices=Provider.choices)
    phone_number_id = models.CharField(max_length=64)
    business_account_id = models.CharField(max_length=64, null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices)
    connected_at = models.DateTimeField(null=True, blank=True)

    objects = WhatsAppAccountManager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(sender_type="SHARED_POOL", merchant__isnull=True)
                    | Q(sender_type="OWN_NUMBER", merchant__isnull=False)
                ),
                name="whatsapp_account_shared_iff_no_merchant",
            ),
            models.UniqueConstraint(
                fields=["provider", "phone_number_id"], name="whatsapp_account_provider_phone_uniq"
            ),
            # V1 has exactly one shared account; dropped when rotation lands (V1.1).
            models.UniqueConstraint(
                fields=["sender_type"],
                condition=Q(sender_type="SHARED_POOL"),
                name="whatsapp_account_single_shared_pool",
            ),
        ]
        indexes = [models.Index(fields=["merchant"], name="whatsapp_account_merchant_idx")]

    def __str__(self):
        return f"{self.sender_type} {self.pk}"


class WhatsAppLocationMapping(BaseModel):
    """Which account a Location sends from (UNIQUE(location)). Tenant path is
    through the location, not the account, whose merchant is NULL when shared."""

    tenant_field = "location__merchant_id"

    whatsapp_account = models.ForeignKey(
        WhatsAppAccount, on_delete=models.PROTECT, related_name="location_mappings"
    )
    location = models.OneToOneField(
        "locations.Location", on_delete=models.PROTECT, related_name="whatsapp_mapping"
    )

    objects = TenantScopedManager()


class MessageTemplate(BaseModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    merchant = models.ForeignKey(
        "accounts.Merchant", on_delete=models.PROTECT, related_name="message_templates"
    )
    name = models.CharField(max_length=255)
    language = models.CharField(max_length=16)
    body = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    provider_template_id = models.CharField(max_length=64, null=True, blank=True)

    objects = TenantScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["merchant", "name", "language"], name="whatsapp_template_merchant_name_lang_uniq"
            ),
            models.UniqueConstraint(
                fields=["provider_template_id"],
                condition=Q(provider_template_id__isnull=False),
                name="whatsapp_template_provider_id_uniq",
            ),
        ]
        indexes = [models.Index(fields=["merchant", "status"], name="whatsapp_tpl_merch_status_idx")]
