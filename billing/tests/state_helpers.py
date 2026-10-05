"""Read-back helpers for the maintenance and race tests: one call returns
everything a billing transition can change for a merchant (the subscription
fields, the billing audit actions, the payment ledger, the usage records and
which webhook events are processed), so a test asserts the whole outcome, not
just the status. Plain helpers, not a conftest."""
from datetime import timedelta
from types import SimpleNamespace

from auditlog.models import AuditLog
from billing.models import BillingEvent, PaymentAttempt, Subscription, UsageRecord
from core.tenancy import tenant_atomic, tenant_context


def read_state(merchant) -> SimpleNamespace:
    with tenant_context(merchant.id), tenant_atomic():
        sub = Subscription.objects.get()
        return SimpleNamespace(
            status=sub.status,
            period=(sub.current_period_start, sub.current_period_end),
            past_due_at=sub.past_due_at,
            dunning_stage=sub.dunning_stage,
            cancel_at_period_end=sub.cancel_at_period_end,
            provider_status=sub.provider_status,
            provider_synced_at=sub.provider_synced_at,
            audits=[
                a.action
                for a in AuditLog.objects.filter(action__startswith="billing.").order_by("created_at", "id")
            ],
            payments=[
                (p.provider_attempt_id, p.attempt_type, p.status, p.attempted_at)
                for p in PaymentAttempt.objects.order_by("created_at", "id")
            ],
            usages=[
                (u.period_start, u.period_end, u.requests_used)
                for u in UsageRecord.objects.order_by("period_start")
            ],
            events={e.provider_event_id: e.processed_at is not None for e in BillingEvent.objects.all()},
        )


def make_event(merchant, ref, event_id="evt_1", *, age=timedelta(minutes=3)):
    """An unprocessed BillingEvent received `age` ago (created_at backdated)."""
    from django.utils import timezone as dj_timezone

    with tenant_context(merchant.id), tenant_atomic():
        event = BillingEvent.objects.create(
            merchant=merchant,
            provider="razorpay",
            provider_event_id=event_id,
            event_type="subscription.charged",
            provider_ref=ref,
        )
        BillingEvent.objects.filter(pk=event.pk).update(created_at=dj_timezone.now() - age)
    return event
