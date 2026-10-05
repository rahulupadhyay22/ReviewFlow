"""Django Admin for billing (spec 07 "Admin"). Only Plan is registered: it is
global reference data, so it needs no tenant context. Subscription,
UsageRecord, PaymentAttempt and BillingEvent are tenant tables, and
TenantScopedManager needs a tenant context, so cross-tenant admin waits for
the audited Phase 16 path (same precedent as accounts/admin.py)."""
from django.contrib import admin

from billing.models import Plan

# A Razorpay plan's amount is immutable, so a price change is a new Plan row
# (and retiring the old one), never an edit of these.
IMMUTABLE_AFTER_CREATION = ("monthly_price", "currency", "provider_plan_id")


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "monthly_price", "currency", "quota_requests", "is_active", "provider_plan_id")
    ordering = ("monthly_price", "name")

    def get_readonly_fields(self, request, obj=None):
        # Editable while adding, read-only once the row exists.
        return IMMUTABLE_AFTER_CREATION if obj is not None else ()

    def has_delete_permission(self, request, obj=None):
        return False
