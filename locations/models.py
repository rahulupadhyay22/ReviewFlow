"""Locations (Data-Dictionary.md §Location).

The unit everything else attaches to: campaigns, WhatsApp mapping, Google
mapping, QR codes, and integration-location mappings (later phases).
Tenant ownership: MERCHANT.
"""
from django.db import models

from core.managers import TenantScopedManager
from core.models import BaseModel


class Location(BaseModel):
    """A merchant's physical/business location. Never hard-deleted by
    application code (DELETE /locations/{id} soft-deletes via is_active)."""

    merchant = models.ForeignKey(
        "accounts.Merchant", on_delete=models.PROTECT, related_name="locations"
    )
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=500, null=True, blank=True)
    phone = models.CharField(max_length=32, null=True, blank=True)
    # Overrides Merchant.timezone when set (Data-Dictionary.md §Location).
    timezone = models.CharField(max_length=64, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = TenantScopedManager()

    def __str__(self):
        return self.name
