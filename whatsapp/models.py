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
from core.tenancy import get_current_lookup_phone_number_id, get_current_merchant_id


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

    def for_lookup_phone_number_id(self, phone_number_id):
        """The only merchant-unscoped read of an OWN_NUMBER account (spec
        08-whatsapp-embedded-signup ES-3): mirrors the SELECT-only
        whatsapp_number_lookup RLS policy and works only inside
        core.tenancy.whatsapp_number_lookup_atomic() for this same id. Callers
        select merchant_id only (values_list)."""
        if get_current_lookup_phone_number_id() != phone_number_id:
            raise TenantContextError(
                "for_lookup_phone_number_id() requires whatsapp_number_lookup_atomic() "
                "for this phone_number_id."
            )
        return models.Manager.get_queryset(self).filter(
            sender_type=WhatsAppAccount.SenderType.OWN_NUMBER,
            provider=WhatsAppAccount.Provider.META_CLOUD,
            phone_number_id=phone_number_id,
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
    # Fernet ciphertext (core.crypto.encrypt) of the merchant's Meta business
    # token (spec 08-whatsapp-embedded-signup ES-1). OWN_NUMBER only: NULL for
    # SHARED_POOL (its token is env-only) and after a disconnect. No expiry or
    # refresh column until token expiry is verified (WhatsApp-Meta.md E-2c).
    access_token_enc = models.TextField(null=True, blank=True)

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
            models.CheckConstraint(
                condition=Q(sender_type="OWN_NUMBER") | Q(access_token_enc__isnull=True),
                name="whatsapp_account_token_own_number_only",
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
    # NULL = the shared-pool WABA (every row that exists today). Otherwise one
    # of the merchant's own OWN_NUMBER accounts, whose WABA the template
    # belongs to (spec 08-whatsapp-embedded-signup ES-2). RLS cannot prove the
    # account is the merchant's own: the service enforces that, as it does for
    # WhatsAppLocationMapping.
    whatsapp_account = models.ForeignKey(
        WhatsAppAccount,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="message_templates",
    )

    objects = TenantScopedManager()

    class Meta:
        constraints = [
            # Two partial uniques replace UNIQUE(merchant, name, language):
            # NULLs are distinct in a plain unique, and NULLS NOT DISTINCT
            # needs PostgreSQL 15+ (the production version is unrecorded).
            models.UniqueConstraint(
                fields=["merchant", "name", "language"],
                condition=Q(whatsapp_account__isnull=True),
                name="whatsapp_template_shared_name_lang_uniq",
            ),
            models.UniqueConstraint(
                fields=["merchant", "whatsapp_account", "name", "language"],
                condition=Q(whatsapp_account__isnull=False),
                name="whatsapp_template_own_name_lang_uniq",
            ),
            models.UniqueConstraint(
                fields=["provider_template_id"],
                condition=Q(provider_template_id__isnull=False),
                name="whatsapp_template_provider_id_uniq",
            ),
        ]
        indexes = [models.Index(fields=["merchant", "status"], name="whatsapp_tpl_merch_status_idx")]


class EmbeddedSignupState(BaseModel):
    """One issued Embedded Signup state (spec 08-whatsapp-embedded-signup
    ES-18). Only the SHA-256 of the state is stored, never the plaintext. A
    state is consumed by deleting its row in one conditional DELETE, so there
    is no consumed flag and a consumed state can never be restored. MERCHANT
    table: plain rls_direct tenant_isolation."""

    merchant = models.ForeignKey(
        "accounts.Merchant", on_delete=models.PROTECT, related_name="embedded_signup_states"
    )
    user = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="embedded_signup_states"
    )
    state_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()

    objects = TenantScopedManager()
