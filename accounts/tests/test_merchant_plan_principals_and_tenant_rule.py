"""GET /merchant `plan` (spec 07 Decision 5 and DoD "Webhook... GET /merchant"):

- `{ id, name }` for ACTIVE and PAST_DUE, null for every other state, for BOTH a
  session and an API key (test_merchant_plan.py covers the API key for ACTIVE
  and for no subscription only);
- the TenantContextError rule at the serializer: a missing or foreign tenant
  context RAISES; it is never rendered as `plan: null`, which is reserved for "a
  correctly scoped merchant with no entitled plan".

New file; test_merchant_plan.py and test_merchant_api.py run unchanged."""
from datetime import timedelta

import pytest
from django.utils import timezone as dj_timezone
from rest_framework.test import APIClient

from accounts.serializers import MerchantSerializer
from apikeys import services as apikey_services
from billing.models import Plan, Subscription
from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

MERCHANT = "/api/v1/merchant"
STATUSES = ["INCOMPLETE", "ACTIVE", "PAST_DUE", "CANCELLED", "EXPIRED"]
ENTITLED = {"ACTIVE", "PAST_DUE"}


def _plan(name="Growth"):
    return Plan.objects.create(
        name=name, monthly_price="999.00", quota_requests=100, provider_plan_id=f"plan_{name.lower()}"
    )


def _subscribe(owner, plan, status):
    now = dj_timezone.now()
    kwargs = {"payment_provider_ref": f"sub_{status.lower()}"}
    if status != "INCOMPLETE":
        kwargs.update(current_period_start=now - timedelta(days=5), current_period_end=now + timedelta(days=25))
    if status == "PAST_DUE":
        kwargs.update(past_due_at=now, dunning_stage=0)
    with tenant_context(owner.merchant_id), tenant_atomic():
        return Subscription.objects.create(merchant=owner.merchant, plan=plan, status=status, **kwargs)


def _api_key_get(owner):
    with tenant_context(owner.merchant_id):
        _, raw = apikey_services.create_api_key(actor=owner, scopes=["sales:write"])
    return APIClient().get(MERCHANT, HTTP_AUTHORIZATION=f"Bearer {raw}")


@pytest.mark.parametrize("status", STATUSES)
def test_an_api_key_and_a_session_show_the_same_plan_for_every_status(session_client, make_merchant, status):
    owner = make_merchant()
    plan = _plan()
    _subscribe(owner, plan, status)
    expected = {"id": str(plan.id), "name": "Growth"} if status in ENTITLED else None

    via_key = _api_key_get(owner)
    via_session = session_client(owner.user.email).get(MERCHANT)

    assert via_key.status_code == 200 and via_session.status_code == 200
    assert via_key.json()["plan"] == expected
    assert via_session.json()["plan"] == expected


def test_the_serializer_raises_without_a_tenant_context_instead_of_rendering_null(make_merchant):
    merchant = make_merchant().merchant
    with pytest.raises(TenantContextError):
        MerchantSerializer(merchant).data


def test_the_serializer_raises_in_another_merchants_context_and_never_shows_that_merchants_plan(make_merchant):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    _subscribe(owner_b, _plan(), "ACTIVE")
    with tenant_context(owner_b.merchant_id):
        with pytest.raises(TenantContextError):
            MerchantSerializer(owner_a.merchant).data


def test_the_serializer_renders_null_only_for_a_correctly_scoped_merchant_with_no_entitled_plan(make_merchant):
    owner = make_merchant()
    with tenant_context(owner.merchant_id):
        assert MerchantSerializer(owner.merchant).data["plan"] is None
