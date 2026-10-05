"""GET /merchant `plan` is derived from the merchant's subscription (spec 07
Decision 5 and "GET /merchant (existing, changed)"): `{ id, name }` of
Subscription.plan while the status is ACTIVE or PAST_DUE, else null. Session and
API-key behavior are otherwise unchanged. These are new tests; the existing
test_merchant_api.py runs unchanged (its merchants have no subscription, so
plan stays null there).

Plan and Subscription rows are built directly: this app's tests do not use the
billing fixtures (fixtures do not cross app test packages)."""
from datetime import timedelta

import pytest
from django.utils import timezone as dj_timezone
from rest_framework.test import APIClient

from apikeys import services as apikey_services
from billing.models import Plan, Subscription
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

MERCHANT = "/api/v1/merchant"


def _plan(name="Starter", **kwargs):
    return Plan.objects.create(
        name=name, monthly_price="999.00", quota_requests=100, provider_plan_id=f"plan_{name.lower()}", **kwargs
    )


def _subscribe(owner, plan, status):
    now = dj_timezone.now()
    kwargs = {}
    if status == "INCOMPLETE":
        kwargs["payment_provider_ref"] = "sub_incomplete"
    else:
        kwargs.update(current_period_start=now - timedelta(days=5), current_period_end=now + timedelta(days=25))
    if status == "PAST_DUE":
        kwargs.update(past_due_at=now, dunning_stage=0)
    with tenant_context(owner.merchant_id), tenant_atomic():
        return Subscription.objects.create(merchant=owner.merchant, plan=plan, status=status, **kwargs)


def _api_key_get(owner):
    with tenant_context(owner.merchant_id):
        _, raw = apikey_services.create_api_key(actor=owner, scopes=["sales:write"])
    return APIClient().get(MERCHANT, HTTP_AUTHORIZATION=f"Bearer {raw}")


def test_a_merchant_with_no_subscription_has_a_null_plan(session_client, make_merchant):
    owner = make_merchant()
    assert session_client(owner.user.email).get(MERCHANT).json()["plan"] is None


@pytest.mark.parametrize("status", ["ACTIVE", "PAST_DUE"])
def test_an_entitled_status_shows_the_subscriptions_plan(session_client, make_merchant, status):
    owner = make_merchant()
    plan = _plan("Growth")
    _subscribe(owner, plan, status)
    body = session_client(owner.user.email).get(MERCHANT).json()
    assert body["plan"] == {"id": str(plan.id), "name": "Growth"}
    assert set(body["plan"]) == {"id", "name"}  # nothing else about the plan or the provider
    assert body["id"] == str(owner.merchant_id) and body["status"] == "ACTIVE"  # the rest is unchanged


@pytest.mark.parametrize("status", ["INCOMPLETE", "CANCELLED", "EXPIRED"])
def test_every_other_status_has_a_null_plan(session_client, make_merchant, status):
    owner = make_merchant()
    _subscribe(owner, _plan(), status)
    assert session_client(owner.user.email).get(MERCHANT).json()["plan"] is None


def test_an_api_key_sees_the_same_derived_plan_and_null_without_a_subscription(make_merchant):
    with_plan, without = make_merchant(), make_merchant()
    plan = _plan("Pro")
    _subscribe(with_plan, plan, "ACTIVE")
    resp = _api_key_get(with_plan)
    assert resp.status_code == 200 and resp.json()["plan"] == {"id": str(plan.id), "name": "Pro"}
    resp = _api_key_get(without)
    assert resp.status_code == 200 and resp.json()["plan"] is None


def test_patch_returns_the_derived_plan_and_a_plan_in_the_body_is_ignored(session_client, make_merchant):
    owner = make_merchant()
    plan = _plan("Starter")
    _subscribe(owner, plan, "ACTIVE")
    client = session_client(owner.user.email)
    resp = client.patch(MERCHANT, {"name": "Renamed", "plan": "enterprise"}, format="json", HTTP_X_CSRFTOKEN=client.csrf)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"
    assert resp.json()["plan"] == {"id": str(plan.id), "name": "Starter"}  # derived, not the submitted value


def test_a_retired_plan_is_still_shown_to_its_subscriber(session_client, make_merchant):
    owner = make_merchant()
    plan = _plan("Business", is_active=False)
    _subscribe(owner, plan, "ACTIVE")
    assert session_client(owner.user.email).get(MERCHANT).json()["plan"] == {"id": str(plan.id), "name": "Business"}


def test_each_merchant_sees_only_its_own_plan(session_client, make_merchant):
    a, b, c = make_merchant(), make_merchant(), make_merchant()
    starter, pro = _plan("Starter"), _plan("Pro")
    _subscribe(a, starter, "ACTIVE")
    _subscribe(b, pro, "PAST_DUE")  # c has no subscription at all
    seen_a = session_client(a.user.email).get(MERCHANT)
    seen_b = session_client(b.user.email).get(MERCHANT)
    seen_c = session_client(c.user.email).get(MERCHANT)
    assert seen_a.json()["plan"]["name"] == "Starter" and str(pro.id) not in seen_a.content.decode()
    assert seen_b.json()["plan"]["name"] == "Pro" and str(starter.id) not in seen_b.content.decode()
    assert seen_c.json()["plan"] is None
    # An API key resolves to its own merchant's subscription, never another's.
    assert _api_key_get(a).json()["plan"]["name"] == "Starter"
    assert _api_key_get(c).json()["plan"] is None
