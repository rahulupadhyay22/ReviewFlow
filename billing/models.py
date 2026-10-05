"""Billing models (spec 07; Data-Dictionary.md §Plan, §Subscription,
§UsageRecord, §PaymentAttempt, plus the new BillingEvent).

Plan is GLOBAL (no merchant_id, no RLS), like Merchant: it is reference data
written only through Django Admin and seed_dev. Every other model here is
tenant-owned. Every FK to Merchant, Plan and Subscription is PROTECT, as the
spec's model definitions state: these are financial records and no billing
row is ever deleted.
"""
from django.db import models
from django.db.models import F, Q

from core.exceptions import TenantContextError
from core.managers import TenantScopedManager
from core.models import BaseModel
from core.tenancy import get_current_lookup_billing_ref


class Plan(BaseModel):
    """GLOBAL reference data: one row per offered tier. Not tenant-owned, so
    it carries no RLS on purpose (Multi-Tenancy.md §Data Model Note)."""

    class Name(models.TextChoices):
        STARTER = "Starter", "Starter"
        GROWTH = "Growth", "Growth"
        PRO = "Pro", "Pro"
        BUSINESS = "Business", "Business"

    name = models.CharField(max_length=32, choices=Name.choices)
    # Display value; the amount actually charged is defined by the Razorpay plan.
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="INR")
    quota_requests = models.PositiveIntegerField()
    features_json = models.JSONField(null=True, blank=True)
    provider_plan_id = models.CharField(max_length=64, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=Q(is_active=True), name="billing_plan_active_name_uniq"
            ),
            models.UniqueConstraint(
                fields=["provider_plan_id"],
                condition=Q(provider_plan_id__isnull=False),
                name="billing_plan_provider_plan_id_uniq",
            ),
            models.CheckConstraint(
                condition=Q(monthly_price__gte=0), name="billing_plan_monthly_price_gte_0"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.monthly_price} {self.currency})"


class SubscriptionManager(TenantScopedManager):
    def for_lookup_ref(self, ref):
        """The only merchant-unscoped read of Subscription (spec 07 Change
        1): mirrors the billing_ref_lookup RLS policy and only works inside
        core.tenancy.billing_ref_lookup_atomic() for this same ref."""
        if get_current_lookup_billing_ref() != ref:
            raise TenantContextError(
                "for_lookup_ref() requires billing_ref_lookup_atomic() for this ref."
            )
        return models.Manager.get_queryset(self).filter(payment_provider_ref=ref)


class Subscription(BaseModel):
    """One row per merchant, reused across resubscribes (Decision 3)."""

    class Status(models.TextChoices):
        INCOMPLETE = "INCOMPLETE", "Incomplete"
        ACTIVE = "ACTIVE", "Active"
        PAST_DUE = "PAST_DUE", "Past due"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired"

    merchant = models.ForeignKey(
        "accounts.Merchant", on_delete=models.PROTECT, related_name="subscriptions"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=16, choices=Status.choices)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    payment_provider_ref = models.CharField(max_length=64, null=True, blank=True)
    pending_plan = models.ForeignKey(
        Plan, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    past_due_at = models.DateTimeField(null=True, blank=True)
    dunning_stage = models.PositiveSmallIntegerField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    provider_status = models.CharField(max_length=16, null=True, blank=True)
    provider_synced_at = models.DateTimeField(null=True, blank=True)

    objects = SubscriptionManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["merchant"], name="billing_subscription_merchant_uniq"),
            models.UniqueConstraint(
                fields=["payment_provider_ref"],
                condition=Q(payment_provider_ref__isnull=False),
                name="billing_subscription_ref_uniq",
            ),
            models.CheckConstraint(
                condition=Q(status="INCOMPLETE")
                | (Q(current_period_start__isnull=False) & Q(current_period_end__isnull=False)),
                name="billing_subscription_period_set",
            ),
            models.CheckConstraint(
                condition=Q(current_period_end__gt=F("current_period_start")),
                name="billing_subscription_period_order",
            ),
            models.CheckConstraint(
                condition=(Q(status="PAST_DUE") & Q(past_due_at__isnull=False))
                | (~Q(status="PAST_DUE") & Q(past_due_at__isnull=True)),
                name="billing_subscription_past_due_at",
            ),
            models.CheckConstraint(
                condition=(Q(dunning_stage__isnull=True) & Q(past_due_at__isnull=True))
                | (Q(dunning_stage__isnull=False) & Q(past_due_at__isnull=False)),
                name="billing_subscription_dunning_episode",
            ),
            models.CheckConstraint(
                condition=Q(dunning_stage__in=[0, 3, 6]), name="billing_subscription_dunning_stage"
            ),
        ]

    def __str__(self):
        return f"{self.merchant_id} {self.status}"


class UsageRecord(BaseModel):
    """Usage for one billing period. Period bounds are the provider's exact
    billing-cycle instants (O10). The quota is deliberately not copied here:
    it is read from Subscription.plan inside the reservation lock."""

    merchant = models.ForeignKey(
        "accounts.Merchant", on_delete=models.PROTECT, related_name="usage_records"
    )
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    requests_used = models.PositiveIntegerField(default=0)

    objects = TenantScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["merchant", "period_start", "period_end"],
                name="billing_usagerecord_period_uniq",
            ),
            models.CheckConstraint(
                condition=Q(period_end__gt=F("period_start")),
                name="billing_usagerecord_period_order",
            ),
        ]

    def __str__(self):
        return f"{self.merchant_id} {self.period_start:%Y-%m-%d}"


class PaymentAttempt(BaseModel):
    """A row is one real Razorpay payment. Nothing else is ever stored here:
    dunning checkpoints live on Subscription (Change 3)."""

    class Provider(models.TextChoices):
        RAZORPAY = "razorpay", "Razorpay"

    class AttemptType(models.TextChoices):
        RENEWAL = "RENEWAL", "Renewal"
        RETRY = "RETRY", "Retry"

    class Status(models.TextChoices):
        INITIATED = "INITIATED", "Initiated"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    merchant = models.ForeignKey(
        "accounts.Merchant", on_delete=models.PROTECT, related_name="payment_attempts"
    )
    subscription = models.ForeignKey(
        Subscription, on_delete=models.PROTECT, related_name="payment_attempts"
    )
    provider = models.CharField(max_length=16, choices=Provider.choices)
    provider_attempt_id = models.CharField(max_length=64)
    attempt_type = models.CharField(max_length=16, choices=AttemptType.choices)
    status = models.CharField(max_length=16, choices=Status.choices)
    attempted_at = models.DateTimeField()

    objects = TenantScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_attempt_id"], name="billing_paymentattempt_provider_uniq"
            ),
            # The database itself refuses a row that is not keyed by a
            # Razorpay payment id ("pay_" prefix, underscore literal).
            models.CheckConstraint(
                condition=Q(provider_attempt_id__startswith="pay_"),
                name="billing_paymentattempt_real_payment",
            ),
        ]
        indexes = [models.Index(fields=["merchant", "attempted_at"], name="billing_pa_merchant_at_idx")]

    def __str__(self):
        return f"{self.provider_attempt_id} {self.status}"


class BillingEvent(BaseModel):
    """Webhook inbox and dedup row. Identifiers only, never the payload
    (Decision 7)."""

    class Provider(models.TextChoices):
        RAZORPAY = "razorpay", "Razorpay"

    merchant = models.ForeignKey(
        "accounts.Merchant", on_delete=models.PROTECT, related_name="billing_events"
    )
    provider = models.CharField(max_length=16, choices=Provider.choices)
    provider_event_id = models.CharField(max_length=64)
    event_type = models.CharField(max_length=64)
    provider_ref = models.CharField(max_length=64)
    processed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_event_id"], name="billing_billingevent_provider_uniq"
            ),
        ]
        indexes = [
            models.Index(
                fields=["merchant"],
                condition=Q(processed_at__isnull=True),
                name="billing_be_unprocessed_idx",
            )
        ]

    def __str__(self):
        return f"{self.event_type} {self.provider_event_id}"
