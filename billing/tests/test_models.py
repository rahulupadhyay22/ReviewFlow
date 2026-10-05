"""One test per unique / check constraint (spec 07 Definition of done)."""
import uuid
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone as dj_timezone

from billing.models import BillingEvent, PaymentAttempt, Plan, Subscription, UsageRecord
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db(transaction=True)


def _payment(merchant, sub, attempt_id, provider="razorpay"):
    return PaymentAttempt.objects.create(
        merchant=merchant,
        subscription=sub,
        provider=provider,
        provider_attempt_id=attempt_id,
        attempt_type="RENEWAL",
        status="SUCCEEDED",
        attempted_at=dj_timezone.now(),
    )


# --- Plan ---------------------------------------------------------------


def test_plan_active_name_is_unique_but_a_retired_one_is_not(make_plan):
    make_plan("Starter")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_plan("Starter")
    make_plan("Starter", is_active=False)  # retired rows do not count


def test_plan_provider_plan_id_is_unique_when_set_and_null_repeats(make_plan):
    make_plan("Starter", provider_plan_id="plan_dup")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_plan("Growth", provider_plan_id="plan_dup")
    make_plan("Growth", provider_plan_id=None)
    make_plan("Pro", provider_plan_id=None)


def test_plan_monthly_price_cannot_be_negative(make_plan):
    with pytest.raises(IntegrityError), transaction.atomic():
        make_plan("Starter", price="-1.00")
    make_plan("Starter", price="0.00")


# --- Subscription -------------------------------------------------------


def test_subscription_is_unique_per_merchant(make_merchant, make_plan, make_subscription):
    owner = make_merchant("A")
    plan = make_plan()
    make_subscription(owner.merchant, plan)
    with pytest.raises(IntegrityError):
        make_subscription(owner.merchant, plan)


def test_subscription_provider_ref_is_unique_when_set(make_merchant, make_plan, make_subscription):
    plan = make_plan()
    make_subscription(make_merchant("A").merchant, plan, ref="sub_dup")
    with pytest.raises(IntegrityError):
        make_subscription(make_merchant("B").merchant, plan, ref="sub_dup")
    make_subscription(make_merchant("C").merchant, plan, ref=None)
    make_subscription(make_merchant("D").merchant, plan, ref=None)


def _save(owner, plan, **kwargs):
    with tenant_context(owner.merchant_id), tenant_atomic():
        with transaction.atomic():
            return Subscription.objects.create(merchant=owner.merchant, plan=plan, **kwargs)


def test_subscription_period_is_required_unless_incomplete(make_merchant, make_plan):
    plan = make_plan()
    now = dj_timezone.now()
    with pytest.raises(IntegrityError):
        _save(make_merchant("A"), plan, status="ACTIVE")
    with pytest.raises(IntegrityError):
        _save(make_merchant("B"), plan, status="ACTIVE", current_period_start=now)
    _save(make_merchant("C"), plan, status="INCOMPLETE")


def test_subscription_period_end_must_follow_start(make_merchant, make_plan):
    now = dj_timezone.now()
    with pytest.raises(IntegrityError):
        _save(
            make_merchant("A"),
            make_plan(),
            status="ACTIVE",
            current_period_start=now,
            current_period_end=now,
        )


def test_subscription_past_due_at_exists_only_while_past_due(make_merchant, make_plan):
    plan = make_plan()
    now = dj_timezone.now()
    period = {"current_period_start": now - timedelta(days=1), "current_period_end": now + timedelta(days=1)}
    with pytest.raises(IntegrityError):  # PAST_DUE without past_due_at
        _save(make_merchant("A"), plan, status="PAST_DUE", **period)
    with pytest.raises(IntegrityError):  # past_due_at on an ACTIVE row
        _save(make_merchant("B"), plan, status="ACTIVE", past_due_at=now, dunning_stage=0, **period)


def test_subscription_dunning_stage_exists_only_with_past_due_at(make_merchant, make_plan):
    plan = make_plan()
    now = dj_timezone.now()
    period = {"current_period_start": now - timedelta(days=1), "current_period_end": now + timedelta(days=1)}
    with pytest.raises(IntegrityError):  # past_due_at without a stage
        _save(make_merchant("A"), plan, status="PAST_DUE", past_due_at=now, **period)
    with pytest.raises(IntegrityError):  # a stage without past_due_at
        _save(make_merchant("B"), plan, status="ACTIVE", dunning_stage=0, **period)


@pytest.mark.parametrize("stage", [1, 2, 4, 7])
def test_subscription_dunning_stage_must_be_0_3_or_6(make_merchant, make_plan, stage):
    now = dj_timezone.now()
    with pytest.raises(IntegrityError):
        _save(
            make_merchant("A"),
            make_plan(),
            status="PAST_DUE",
            past_due_at=now,
            dunning_stage=stage,
            current_period_start=now - timedelta(days=1),
            current_period_end=now + timedelta(days=1),
        )


# --- UsageRecord --------------------------------------------------------


def test_usage_record_is_unique_per_merchant_period(make_merchant, make_plan, make_subscription):
    owner = make_merchant("A")
    sub = make_subscription(owner.merchant, make_plan())
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(IntegrityError), transaction.atomic():
            UsageRecord.objects.create(
                merchant=owner.merchant,
                period_start=sub.current_period_start,
                period_end=sub.current_period_end,
            )


def test_usage_record_period_end_must_follow_start(make_merchant):
    owner = make_merchant("A")
    now = dj_timezone.now()
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(IntegrityError), transaction.atomic():
            UsageRecord.objects.create(merchant=owner.merchant, period_start=now, period_end=now)


# --- PaymentAttempt -----------------------------------------------------


def test_payment_attempt_provider_id_is_unique(make_merchant, make_plan, make_subscription):
    owner = make_merchant("A")
    sub = make_subscription(owner.merchant, make_plan())
    with tenant_context(owner.merchant_id), tenant_atomic():
        _payment(owner.merchant, sub, "pay_same")
        with pytest.raises(IntegrityError), transaction.atomic():
            _payment(owner.merchant, sub, "pay_same")


@pytest.mark.parametrize("attempt_id", ["dunning:abc:3", "", "payX123", "pay", "PAY_123", "xpay_123"])
def test_payment_attempt_id_must_be_a_razorpay_payment_id(
    make_merchant, make_plan, make_subscription, attempt_id
):
    """`payX123` is the case that fails if the LIKE underscore were a
    wildcard; `dunning:` and "" would pass either way."""
    owner = make_merchant("A")
    sub = make_subscription(owner.merchant, make_plan())
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(IntegrityError), transaction.atomic():
            _payment(owner.merchant, sub, attempt_id)


def test_payment_attempt_accepts_a_razorpay_payment_id(make_merchant, make_plan, make_subscription):
    owner = make_merchant("A")
    sub = make_subscription(owner.merchant, make_plan())
    with tenant_context(owner.merchant_id), tenant_atomic():
        _payment(owner.merchant, sub, "pay_Abc123")


# --- BillingEvent -------------------------------------------------------


def test_billing_event_provider_event_id_is_unique(make_merchant):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        make = lambda: BillingEvent.objects.create(  # noqa: E731
            merchant=owner.merchant,
            provider="razorpay",
            provider_event_id="evt_1",
            event_type="subscription.charged",
            provider_ref="sub_1",
        )
        make()
        with pytest.raises(IntegrityError), transaction.atomic():
            make()


def test_plan_and_ids_are_uuids(make_plan):
    assert isinstance(make_plan().id, uuid.UUID)
    assert Plan.objects.count() == 1
