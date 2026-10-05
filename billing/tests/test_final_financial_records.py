"""Billing rows are financial records (spec 07 "Models & database changes":
every FK to Merchant, Plan and Subscription is PROTECT; "no billing row is ever
deleted by application code; there is no delete service and no delete
endpoint"), and the webhook is the only unauthenticated billing URL.

Deletion checks run inside the subscriber's tenant context: outside one, RLS
hides the referencing rows from Django's delete collector and the test would
prove nothing."""
from datetime import timedelta

import pytest
from django.db.models import ProtectedError
from django.utils import timezone as dj_timezone
from rest_framework.test import APIClient

from billing.models import PaymentAttempt, Plan, Subscription
from billing.tests import webhook_helpers
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

PLANS = "/api/v1/billing/plans"
SUBSCRIPTION = "/api/v1/billing/subscription"
CHECKOUT = "/api/v1/billing/checkout"
CANCEL = "/api/v1/billing/subscription/cancel"


def test_a_plan_in_force_cannot_be_deleted(make_merchant, make_plan, make_subscription):
    owner = make_merchant()
    plan = make_plan()
    make_subscription(owner.merchant, plan)
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(ProtectedError):
            plan.delete()
    assert Plan.objects.filter(pk=plan.pk).exists()


def test_a_plan_that_is_only_a_pending_downgrade_cannot_be_deleted(make_merchant, make_plan, make_subscription):
    owner = make_merchant()
    current, target = make_plan("Growth", price="1999.00"), make_plan("Starter")
    make_subscription(owner.merchant, current, pending_plan=target)
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(ProtectedError):
            target.delete()
    assert Plan.objects.filter(pk=target.pk).exists()


def test_a_subscription_with_a_payment_cannot_be_deleted(make_merchant, make_plan, make_subscription):
    owner = make_merchant()
    sub = make_subscription(owner.merchant, make_plan())
    with tenant_context(owner.merchant_id), tenant_atomic():
        PaymentAttempt.objects.create(
            merchant=owner.merchant,
            subscription=sub,
            provider="razorpay",
            provider_attempt_id="pay_Protect0001",
            attempt_type="RENEWAL",
            status="SUCCEEDED",
            attempted_at=dj_timezone.now() - timedelta(days=1),
        )
        with pytest.raises(ProtectedError):
            Subscription.objects.get().delete()
        assert Subscription.objects.count() == 1 and PaymentAttempt.objects.count() == 1


def test_a_merchant_with_a_subscription_cannot_be_deleted(make_merchant, make_plan, make_subscription):
    owner = make_merchant()
    make_subscription(owner.merchant, make_plan())
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(ProtectedError):
            owner.merchant.delete()
        assert Subscription.objects.count() == 1


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
@pytest.mark.parametrize("path", [PLANS, SUBSCRIPTION, CHECKOUT, CANCEL])
def test_no_billing_endpoint_offers_an_update_or_delete_method(
    make_merchant, make_plan, make_subscription, session_client, path, method
):
    owner = make_merchant()
    make_subscription(owner.merchant, make_plan())
    client = session_client(owner.user.email)
    resp = getattr(client, method)(path, {}, format="json", HTTP_X_CSRFTOKEN=client.csrf)
    assert resp.status_code == 405
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Subscription.objects.count() == 1


@pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
def test_the_webhook_accepts_only_post(webhook_secret, method):
    resp = getattr(APIClient(), method)(webhook_helpers.URL)
    assert resp.status_code == 405
