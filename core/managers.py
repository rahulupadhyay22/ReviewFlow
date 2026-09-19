from django.db import models

from core.exceptions import TenantContextError
from core.tenancy import get_current_merchant_id


class TenantScopedManager(models.Manager):
    """Default manager for tenant-owned models (Multi-Tenancy.md §Layer 1).

    Filters on the model's `tenant_field` (default "merchant_id"; transitive
    models use a lookup path such as "location__merchant_id"). RLS is the
    backstop, not this manager.
    """

    def get_queryset(self):
        merchant_id = get_current_merchant_id()
        if merchant_id is None:
            raise TenantContextError(f"{self.model.__name__} queried without a tenant context.")
        tenant_field = getattr(self.model, "tenant_field", "merchant_id")
        return super().get_queryset().filter(**{tenant_field: merchant_id})
