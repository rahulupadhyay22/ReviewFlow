"""seed_dev billing gates not covered by test_seed_dev_billing.py (spec 07
Decision 10 and DoD "Seed and contract"):

- the DEBUG-only guard on the ALREADY-SEEDED path (a pre-Phase-07 database must
  not gain billing fixtures when DEBUG is off);
- the pre-Phase-07 path writes the same development-only values and nothing a
  paid invoice would write;
- seed is the only code allowed to create an ACTIVE subscription without proof,
  and it creates one only when the merchant has no subscription at all: it never
  revives, replaces or resets an existing row, whatever its status.

New file; existing seed tests run unchanged."""
import io
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.utils import timezone

from accounts.models import Merchant
from billing.models import BillingEvent, PaymentAttempt, Plan, Subscription, UsageRecord
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

PASSWORD = "Tr1cky-Horse-Battery-Staple!"


@pytest.fixture(autouse=True)
def local_cache(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    yield
    cache.clear()


def seed():
    out = io.StringIO()
    call_command("seed_dev", "--password", PASSWORD, stdout=out)
    return out.getvalue()


def strip_billing(merchant):
    """Back to how a database seeded before Phase 07 looks."""
    with tenant_context(merchant.id), tenant_atomic():
        UsageRecord.objects.all().delete()
        Subscription.objects.all().delete()
    Plan.objects.all().delete()


def billing_rows(merchant):
    with tenant_context(merchant.id), tenant_atomic():
        return {
            "subscriptions": list(Subscription.objects.values_list("id", "status", "updated_at")),
            "usage": list(UsageRecord.objects.values_list("id", "requests_used", "updated_at")),
            "payments": PaymentAttempt.objects.count(),
            "events": BillingEvent.objects.count(),
        }


def test_a_pre_phase_07_database_gains_nothing_when_debug_is_off(settings):
    settings.DEBUG = True
    seed()
    merchant = Merchant.objects.get()
    strip_billing(merchant)

    settings.DEBUG = False
    with pytest.raises(CommandError):
        seed()

    assert Plan.objects.count() == 0
    assert billing_rows(merchant) == {"subscriptions": [], "usage": [], "payments": 0, "events": 0}


def test_a_fully_seeded_database_is_left_untouched_when_debug_is_off(settings):
    settings.DEBUG = True
    seed()
    merchant = Merchant.objects.get()
    plans = list(Plan.objects.order_by("name").values_list("id", "updated_at"))
    rows = billing_rows(merchant)

    settings.DEBUG = False
    with pytest.raises(CommandError):
        seed()

    assert list(Plan.objects.order_by("name").values_list("id", "updated_at")) == plans
    assert billing_rows(merchant) == rows


def test_the_pre_phase_07_path_writes_development_values_only_and_no_payment_artifacts(settings):
    settings.DEBUG = True
    seed()
    merchant = Merchant.objects.get()
    strip_billing(merchant)

    seed()

    for plan in Plan.objects.all():
        assert plan.monthly_price == 0 and plan.provider_plan_id is None
    with tenant_context(merchant.id), tenant_atomic():
        sub = Subscription.objects.get()
        usage = UsageRecord.objects.get()
    assert sub.status == "ACTIVE"
    assert sub.payment_provider_ref is None and sub.provider_status is None and sub.provider_synced_at is None
    assert (usage.period_start, usage.period_end, usage.requests_used) == (
        sub.current_period_start,
        sub.current_period_end,
        0,
    )
    after = billing_rows(merchant)
    assert after["payments"] == 0 and after["events"] == 0


@pytest.mark.parametrize("status", ["CANCELLED", "EXPIRED", "INCOMPLETE", "PAST_DUE"])
def test_seed_never_revives_or_replaces_an_existing_subscription(settings, status):
    settings.DEBUG = True
    seed()
    merchant = Merchant.objects.get()
    strip_billing(merchant)
    growth = Plan.objects.create(name="Growth", monthly_price=0, quota_requests=500)
    now = timezone.now()
    kwargs = {}
    if status != "INCOMPLETE":
        kwargs.update(current_period_start=now - timedelta(days=40), current_period_end=now - timedelta(days=10))
    if status == "PAST_DUE":
        kwargs.update(past_due_at=now, dunning_stage=0)
    with tenant_context(merchant.id), tenant_atomic():
        Subscription.objects.create(merchant=merchant, plan=growth, status=status, **kwargs)
    before = billing_rows(merchant)

    seed()

    assert billing_rows(merchant) == before  # still that one row, same status, no usage record opened
    with tenant_context(merchant.id), tenant_atomic():
        assert Subscription.objects.get().status == status
        assert UsageRecord.objects.count() == 0
