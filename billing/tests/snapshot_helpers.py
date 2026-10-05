"""Provider-snapshot builders shared by the billing lifecycle tests: a
Razorpay subscription entity and a subscription invoice, in the shape
`billing.services` consumes. Plain helpers, not a conftest (importing from a
conftest invites circular imports)."""
from datetime import timedelta

from django.utils import timezone as dj_timezone

REF = "sub_test_ref"
NOW = dj_timezone.now().replace(microsecond=0)
START = NOW - timedelta(days=10)  # whole seconds: the provider sends unix seconds
END = START + timedelta(days=30)


def ts(dt):
    return int(dt.timestamp())


def entity(plan, *, status="active", start=START, end=None, ref=REF, plan_id="__plan__"):
    return {
        "id": ref,
        "plan_id": plan.provider_plan_id if plan_id == "__plan__" else plan_id,
        "status": status,
        "current_start": ts(start),
        "current_end": ts(end or start + timedelta(days=30)),
    }


def invoice(start=START, end=None, **over):
    base = {
        "id": "inv_1",
        "subscription_id": REF,
        "status": "paid",
        "amount_due": 0,
        "payment_id": "pay_1",
        "billing_start": ts(start),
        "billing_end": ts(end or start + timedelta(days=30)),
        "paid_at": ts(start) + 3600,
    }
    base.update(over)
    return base
