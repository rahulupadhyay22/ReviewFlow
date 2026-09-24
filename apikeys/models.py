"""
ApiKey (Data-Dictionary.md §ApiKey, spec .claude/specs/05-public-api-keys.md).

A hashed, scoped, individually revocable credential for the public API
(Authentication.md §2). merchant_id is direct; RLS carries both the standard
tenant_isolation policy and the pre-tenant, SELECT-only api_key_lookup
policy (core/rls.py:rls_select_by_key_hash).
"""
import uuid

from django.db import models

from accounts.models import Merchant
from core.exceptions import TenantContextError
from core.managers import TenantScopedManager
from core.models import BaseModel
from core.tenancy import get_current_lookup_key_hash


class ApiKeyManager(TenantScopedManager):
    def for_lookup_hash(self, key_hash):
        """The only merchant-unscoped read of ApiKey: mirrors the
        api_key_lookup RLS policy and only works inside
        core.tenancy.api_key_lookup_atomic() for this same hash."""
        if get_current_lookup_key_hash() != key_hash:
            raise TenantContextError("for_lookup_hash() requires api_key_lookup_atomic() for this hash.")
        return models.Manager.get_queryset(self).filter(key_hash=key_hash)


class ApiKey(BaseModel):
    """A public-API credential. The plaintext (rf_live_<32>) is shown once
    at creation and never stored; only its sha256 hash is kept. There is no
    name/label/prefix field (Data-Dictionary.md has none)."""

    class Scope(models.TextChoices):
        SALES_WRITE = "sales:write"
        REVIEWS_READ = "reviews:read"
        TRANSACTIONS_READ = "transactions:read"

    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="api_keys")
    key_hash = models.CharField(max_length=64)
    scopes_json = models.JSONField()
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    objects = ApiKeyManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["key_hash"], name="apikeys_apikey_key_hash_uniq"),
        ]

    def __str__(self):
        return str(self.id)
