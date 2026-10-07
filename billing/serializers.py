"""Billing serializers (Coding-Standards.md §1: shape data, no business
logic). The role-aware `checkout` object and every other decision is made by
billing.services; this module only renders it."""
from rest_framework import serializers

from billing.models import Plan

_iso = serializers.DateTimeField().to_representation


def _dt(value):
    return _iso(value) if value is not None else None


class PlanSerializer(serializers.ModelSerializer):
    """Never includes provider_plan_id."""

    features = serializers.JSONField(source="features_json", read_only=True)

    class Meta:
        model = Plan
        fields = ["id", "name", "monthly_price", "currency", "quota_requests", "features"]


class CheckoutRequestSerializer(serializers.Serializer):
    """The only request input to a billing write. Anything else in the body
    (merchant_id, status, a price) is ignored."""

    plan_id = serializers.UUIDField()
    # Needed only when an upgrade falls back to a replacement (C1); ignored otherwise.
    acknowledge_no_credit = serializers.BooleanField(required=False, default=False)


def _replacement_body(replacement) -> dict | None:
    """`authorized` equals `committed` (spec P2). No provider reference is ever shown."""
    if replacement is None:
        return None
    return {
        "target_plan": {"id": str(replacement["plan"].pk), "name": replacement["plan"].name},
        "kind": replacement["kind"],
        "authorized": replacement["committed"],
        "committed": replacement["committed"],
        "effective_at": _dt(replacement["effective_at"]),
    }


def subscription_body(overview) -> dict:
    """The GET /billing/subscription body (and the `subscription` member of
    the checkout and cancel responses)."""
    subscription, entitlement = overview.subscription, overview.entitlement
    usage = None
    if subscription is not None and entitlement.requests_used is not None:
        usage = {
            "requests_used": entitlement.requests_used,
            "quota_requests": entitlement.quota_requests,
            "requests_remaining": entitlement.requests_remaining,
        }
    next_action = overview.next_action
    return {
        "status": subscription.status if subscription is not None else None,
        "plan": PlanSerializer(subscription.plan).data if subscription is not None else None,
        "pending_plan": (
            PlanSerializer(subscription.pending_plan).data
            if subscription is not None and subscription.pending_plan is not None
            else None
        ),
        "current_period_start": _dt(subscription.current_period_start) if subscription else None,
        "current_period_end": _dt(subscription.current_period_end) if subscription else None,
        "cancel_at_period_end": subscription.cancel_at_period_end if subscription else False,
        "past_due_at": _dt(subscription.past_due_at) if subscription else None,
        "grace_ends_at": _dt(overview.grace_ends_at),
        "dunning_stage": subscription.dunning_stage if subscription else None,
        "usage": usage,
        "can_send": entitlement.can_send,
        "checkout": overview.checkout,
        "next_action": {"type": next_action["type"], "at": _dt(next_action["at"])},
        "replacement": _replacement_body(overview.replacement),
    }
