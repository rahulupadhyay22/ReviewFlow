"""The Phase 07 billing step of `python manage.py seed_dev` (spec 07 Decision 10
and Definition of done "Seed and contract"): four development plans, and an
ACTIVE development subscription on Growth with a one-year period and its
UsageRecord. Idempotent on its own, DEBUG-only, and added to a database seeded
before Phase 07 on the next run. The values are development fixtures, not
pricing or quota decisions.

New file: the existing test_seed_dev.py runs unchanged."""
import io
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.core.management import CommandError, call_command
from rest_framework.test import APIClient

from accounts.models import Merchant
from billing import services as billing_services
from billing.models import BillingEvent, PaymentAttempt, Plan, Subscription, UsageRecord
from core.management.commands.seed_dev import SEED_OWNER_EMAIL
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

PASSWORD = "Tr1cky-Horse-Battery-Staple!"
EXPECTED_PLANS = {"Starter": 100, "Growth": 500, "Pro": 2000, "Business": 10000}


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


def seed_merchant():
    return Merchant.objects.get()


def rows(merchant):
    with tenant_context(merchant.id), tenant_atomic():
        return {
            "subscriptions": list(Subscription.objects.values_list("id", "updated_at")),
            "usage": list(UsageRecord.objects.values_list("id", "updated_at", "requests_used")),
            "payments": PaymentAttempt.objects.count(),
            "events": BillingEvent.objects.count(),
        }


def login(email):
    client = APIClient(enforce_csrf_checks=True)
    client.get("/api/v1/auth/login")
    token = client.cookies["csrftoken"].value
    resp = client.post(
        "/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json", HTTP_X_CSRFTOKEN=token
    )
    assert resp.status_code == 200
    client.csrf = client.cookies["csrftoken"].value  # the token rotates at login
    return client


def test_a_fresh_seed_creates_four_dev_plans_with_zero_price_and_no_provider_id(settings):
    settings.DEBUG = True
    seed()
    plans = {p.name: p for p in Plan.objects.all()}
    assert {name: p.quota_requests for name, p in plans.items()} == EXPECTED_PLANS
    for plan in plans.values():
        assert plan.monthly_price == 0 and plan.provider_plan_id is None and plan.is_active


def test_a_fresh_seed_creates_an_active_growth_subscription_with_a_one_year_period_and_usage(settings):
    settings.DEBUG = True
    seed()
    merchant = seed_merchant()
    with tenant_context(merchant.id), tenant_atomic():
        sub = Subscription.objects.select_related("plan").get()
        usage = UsageRecord.objects.get()
    assert (sub.status, sub.plan.name) == ("ACTIVE", "Growth")
    assert sub.payment_provider_ref is None and sub.provider_status is None  # no provider, no proof
    assert sub.current_period_end - sub.current_period_start == timedelta(days=365)
    assert (usage.period_start, usage.period_end, usage.requests_used) == (
        sub.current_period_start,
        sub.current_period_end,
        0,
    )
    after = rows(merchant)
    assert after["payments"] == 0 and after["events"] == 0  # nothing a paid invoice would write


def test_the_fresh_seed_output_labels_the_values_and_keeps_the_password_last(settings):
    settings.DEBUG = True
    output = seed()
    assert "development values only" in output
    assert output.splitlines()[-1].split("Password:")[-1].strip() == PASSWORD


def test_the_dev_subscription_makes_the_seed_owner_entitled_and_shows_its_plan(settings):
    settings.DEBUG = True
    seed()
    merchant = seed_merchant()
    plan = Plan.objects.get(name="Growth")
    body = login(SEED_OWNER_EMAIL).get("/api/v1/merchant").json()
    assert body["plan"] == {"id": str(plan.id), "name": "Growth"}
    with tenant_context(merchant.id):
        entitlement = billing_services.get_entitlement()
    assert entitlement.can_send is True


def test_a_second_run_changes_nothing(settings):
    settings.DEBUG = True
    seed()
    merchant = seed_merchant()
    plans_before = list(Plan.objects.order_by("name").values_list("id", "updated_at"))
    before = rows(merchant)
    output = seed()
    assert "already seeded" in output and "added" not in output
    assert list(Plan.objects.order_by("name").values_list("id", "updated_at")) == plans_before
    assert rows(merchant) == before
    assert Plan.objects.count() == 4


def _strip_billing(merchant):
    """Back to how a database seeded before Phase 07 looks."""
    with tenant_context(merchant.id), tenant_atomic():
        UsageRecord.objects.all().delete()
        Subscription.objects.all().delete()
    Plan.objects.all().delete()


def test_a_database_seeded_before_phase_07_gains_the_billing_fixtures_on_the_next_run(settings):
    settings.DEBUG = True
    seed()
    merchant = seed_merchant()
    _strip_billing(merchant)
    assert Plan.objects.count() == 0

    output = seed()

    assert "already seeded" in output and "added" in output
    assert {p.name: p.quota_requests for p in Plan.objects.all()} == EXPECTED_PLANS
    with tenant_context(merchant.id), tenant_atomic():
        assert Subscription.objects.get().plan.name == "Growth"
        assert UsageRecord.objects.count() == 1
    again = seed()
    assert "added" not in again and Plan.objects.count() == 4


def test_existing_plans_are_reused_not_duplicated(settings):
    settings.DEBUG = True
    seed()
    merchant = seed_merchant()
    with tenant_context(merchant.id), tenant_atomic():
        UsageRecord.objects.all().delete()
        Subscription.objects.all().delete()
    seed()  # plans exist, the subscription does not
    assert Plan.objects.count() == 4
    with tenant_context(merchant.id), tenant_atomic():
        assert Subscription.objects.count() == 1


def test_with_debug_false_nothing_billing_is_written(settings):
    settings.DEBUG = False
    with pytest.raises(CommandError):
        seed()
    # No merchant exists, so there is nothing a subscription could belong to.
    assert Plan.objects.count() == 0 and Merchant.objects.count() == 0


def test_a_seed_subscription_is_not_a_provider_subscription_so_the_sweep_ignores_it(settings):
    settings.DEBUG = True
    seed()
    merchant = seed_merchant()
    with tenant_context(merchant.id):
        assert billing_services.maintenance_due() is False  # inside its period, no provider ref


def test_cancel_on_the_seed_subscription_is_the_documented_409_and_its_plans_are_not_offered(settings):
    """The row has no provider reference (409 subscription_provider_state_unsupported
    for cancel), and the seed plans have no provider_plan_id, so checkout of one
    is rejected as an unavailable plan (422 validation_error) before any provider
    call."""
    settings.DEBUG = True
    seed()
    client = login(SEED_OWNER_EMAIL)
    growth = Plan.objects.get(name="Growth")
    cancel = client.post("/api/v1/billing/subscription/cancel", {}, format="json", HTTP_X_CSRFTOKEN=client.csrf)
    assert cancel.status_code == 409
    assert cancel.json()["error"]["code"] == "subscription_provider_state_unsupported"
    checkout = client.post(
        "/api/v1/billing/checkout", {"plan_id": str(growth.id)}, format="json", HTTP_X_CSRFTOKEN=client.csrf
    )
    assert checkout.status_code == 422
    assert checkout.json()["error"]["code"] == "validation_error"
