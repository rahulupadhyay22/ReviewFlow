"""Integration and IntegrationLocationMapping (Data-Dictionary.md
§Integration, §IntegrationLocationMapping; Database-Design.md
§Integration, §IntegrationLocationMapping).

Tenant ownership: MERCHANT (direct merchant_id on both tables).
"""
from django.db import models

from core.managers import TenantScopedManager
from core.models import BaseModel


class Integration(BaseModel):
    """A connected sale-capture system at merchant level. One integration
    may feed multiple ReviewFlow locations through IntegrationLocationMapping.
    """

    class Provider(models.TextChoices):
        # Lowercase provider identifiers (Data-Dictionary.md §Integration) --
        # these are identifiers, not workflow statuses, so they are not
        # UPPER_SNAKE_CASE like the Status enum below.
        SHOPIFY = "shopify", "Shopify"
        WOOCOMMERCE = "woocommerce", "WooCommerce"
        PETPOOJA = "petpooja", "Petpooja"
        GOFRUGAL = "gofrugal", "GoFrugal"
        WEBHOOK = "webhook", "Generic Webhook"
        CSV = "csv", "CSV Import"
        ZAPIER = "zapier", "Zapier"
        MAKE = "make", "Make"

    class Status(models.TextChoices):
        CONNECTED = "CONNECTED", "Connected"
        ERROR = "ERROR", "Error"
        DISCONNECTED = "DISCONNECTED", "Disconnected"

    merchant = models.ForeignKey(
        "accounts.Merchant", on_delete=models.PROTECT, related_name="integrations"
    )
    provider = models.CharField(max_length=32, choices=Provider.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CONNECTED)
    # Fernet token of the JSON-serialized credentials (core.crypto). Never
    # plaintext, never returned by the API, never logged (Security-Controls.md
    # §"Encryption & Secrets").
    credentials_encrypted = models.TextField(null=True, blank=True)
    config_json = models.JSONField(null=True, blank=True)

    objects = TenantScopedManager()

    def __str__(self):
        return f"{self.get_provider_display()} ({self.merchant_id})"


class IntegrationLocationMapping(BaseModel):
    """Maps a merchant-level Integration to one or more internal Locations.
    The only source of truth for associating a connected provider with a
    ReviewFlow location (Integration-Architecture.md §"Integration-to-Location
    Mapping")."""

    merchant = models.ForeignKey("accounts.Merchant", on_delete=models.PROTECT, related_name="+")
    integration = models.ForeignKey(
        Integration, on_delete=models.PROTECT, related_name="location_mappings"
    )
    location = models.ForeignKey(
        "locations.Location", on_delete=models.PROTECT, related_name="integration_mappings"
    )
    # Optional location-specific provider mapping. The resolution key is
    # config_json["external_location_id"] (spec Decision 5), a string
    # identifying the provider's own location for multi-location providers.
    config_json = models.JSONField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = TenantScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "location"],
                name="uniq_integrationlocationmapping_integration_location",
            )
        ]

    def __str__(self):
        return f"{self.integration_id} -> {self.location_id}"
