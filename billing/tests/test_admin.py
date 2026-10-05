"""Django Admin for billing (spec 07 "Admin" and Definition of done): only Plan
is registered; monthly_price, currency and provider_plan_id are editable while
adding and read-only afterwards; delete is not offered. Subscription,
UsageRecord, PaymentAttempt and BillingEvent are tenant tables and stay
unregistered (cross-tenant admin waits for the audited Phase 16 path).

Plan is global, so no tenant context is involved."""
import pytest
from django.contrib import admin
from django.test import Client, RequestFactory

from accounts.models import User
from billing.admin import IMMUTABLE_AFTER_CREATION, PlanAdmin
from billing.models import BillingEvent, PaymentAttempt, Plan, Subscription, UsageRecord

pytestmark = pytest.mark.django_db

PASSWORD = "Tr1cky-Horse-Battery-Staple!"


@pytest.fixture
def staff_client():
    user = User.objects.create_superuser("staff@example.com", PASSWORD)
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def plan(make_plan):
    return make_plan("Starter", price="999.00", quota=100, provider_plan_id="plan_admin_1")


def test_only_plan_is_registered():
    assert admin.site.is_registered(Plan)
    for model in (Subscription, UsageRecord, PaymentAttempt, BillingEvent):
        assert not admin.site.is_registered(model), model.__name__


def test_the_immutable_fields_are_exactly_the_three_the_spec_names():
    assert IMMUTABLE_AFTER_CREATION == ("monthly_price", "currency", "provider_plan_id")


def test_read_only_fields_apply_only_to_an_existing_plan(plan):
    model_admin = PlanAdmin(Plan, admin.site)
    request = RequestFactory().get("/")
    assert model_admin.get_readonly_fields(request, None) == ()  # adding
    assert model_admin.get_readonly_fields(request, plan) == IMMUTABLE_AFTER_CREATION  # editing


def test_delete_is_never_permitted(plan, staff_client):
    model_admin = PlanAdmin(Plan, admin.site)
    request = RequestFactory().get("/")
    request.user = User.objects.get(email="staff@example.com")
    assert model_admin.has_delete_permission(request, plan) is False
    assert model_admin.has_delete_permission(request) is False
    assert staff_client.get(f"/admin/billing/plan/{plan.pk}/delete/").status_code == 403
    assert Plan.objects.filter(pk=plan.pk).exists()


def test_the_add_form_accepts_the_immutable_fields(staff_client):
    page = staff_client.get("/admin/billing/plan/add/")
    assert page.status_code == 200
    for field in IMMUTABLE_AFTER_CREATION:
        assert f'name="{field}"' in page.content.decode(), field
    resp = staff_client.post(
        "/admin/billing/plan/add/",
        {
            "name": "Growth",
            "monthly_price": "1999.00",
            "currency": "INR",
            "quota_requests": 500,
            "provider_plan_id": "plan_admin_new",
            "is_active": "on",
        },
    )
    assert resp.status_code == 302, resp.content.decode()[:500]
    created = Plan.objects.get(provider_plan_id="plan_admin_new")
    assert (created.name, str(created.monthly_price), created.currency, created.quota_requests) == (
        "Growth",
        "1999.00",
        "INR",
        500,
    )


def test_the_change_form_does_not_offer_the_immutable_fields(plan, staff_client):
    page = staff_client.get(f"/admin/billing/plan/{plan.pk}/change/")
    assert page.status_code == 200
    html = page.content.decode()
    for field in IMMUTABLE_AFTER_CREATION:
        assert f'name="{field}"' not in html, field  # displayed, not an input
    assert 'name="quota_requests"' in html  # the other fields stay editable


def test_a_posted_change_to_an_immutable_field_is_ignored_but_other_fields_save(plan, staff_client):
    resp = staff_client.post(
        f"/admin/billing/plan/{plan.pk}/change/",
        {
            "name": "Starter",
            "quota_requests": 250,
            "is_active": "on",
            # attempted edits of the protected fields:
            "monthly_price": "1.00",
            "currency": "USD",
            "provider_plan_id": "plan_hijacked",
        },
    )
    assert resp.status_code == 302, resp.content.decode()[:500]
    plan.refresh_from_db()
    assert plan.quota_requests == 250  # an editable field saved
    assert str(plan.monthly_price) == "999.00"
    assert plan.currency == "INR"
    assert plan.provider_plan_id == "plan_admin_1"
