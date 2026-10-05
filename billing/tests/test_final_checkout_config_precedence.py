"""The two documented checkout answers that must never be conflated (spec 07
"Production controls" and "Rollout"):

- no eligible plan (missing, malformed, unknown, inactive, or without a
  provider_plan_id) -> 422 validation_error, because the plan is validated
  first, EVEN WHEN the Razorpay credentials are unset;
- an offered plan with the credentials unset -> 503 billing_not_configured,
  with no provider call, on every row state, and nothing changes locally.

test_api.py covers the 422 with credentials set and the 503 with only
RAZORPAY_KEY_SECRET cleared on a fresh merchant. FakeProvider records every
call, so `calls == []` proves nothing reached Razorpay. Mocked responses
verify ReviewFlow's handling only, never Razorpay's real contract."""
import uuid

import pytest

from auditlog.models import AuditLog
from billing.models import Subscription
from billing.tests.snapshot_helpers import REF
from billing.tests.state_helpers import read_state
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

CHECKOUT = "/api/v1/billing/checkout"

# Which credential is missing: both, only the id, only the secret.
UNSET = {
    "both": {"RAZORPAY_KEY_ID": "", "RAZORPAY_KEY_SECRET": ""},
    "id_only": {"RAZORPAY_KEY_ID": ""},
    "secret_only": {"RAZORPAY_KEY_SECRET": ""},
}


@pytest.fixture
def s(lifecycle_setup, session_client):
    """Merchant A with an OWNER session and offered plans. The provider fixture
    (inside lifecycle_setup) runs first, so unsetting credentials afterwards is
    visible to the service and any provider call is still recorded."""
    lifecycle_setup.client = session_client(lifecycle_setup.a.user.email)
    return lifecycle_setup


def unset(settings, which):
    for name, value in UNSET[which].items():
        setattr(settings, name, value)


def post(s, plan_id):
    return s.client.post(CHECKOUT, {"plan_id": plan_id}, format="json", HTTP_X_CSRFTOKEN=s.client.csrf)


def billing_audits(merchant):
    with tenant_context(merchant.id), tenant_atomic():
        return list(AuditLog.objects.filter(action__startswith="billing.").values_list("action", flat=True))


@pytest.mark.parametrize("which", UNSET)
def test_an_ineligible_plan_is_422_even_when_credentials_are_unset(s, settings, make_plan, which):
    retired = make_plan("Business", price="10.00", quota=1, provider_plan_id="plan_old", is_active=False)
    providerless = make_plan("Business", price="10.00", quota=1, provider_plan_id=None)
    unset(settings, which)
    bodies = (None, "not-a-uuid", str(uuid.uuid4()), str(retired.id), str(providerless.id))
    for plan_id in bodies:
        resp = s.client.post(
            CHECKOUT, {} if plan_id is None else {"plan_id": plan_id}, format="json", HTTP_X_CSRFTOKEN=s.client.csrf
        )
        assert resp.status_code == 422, plan_id
        assert resp.json()["error"]["code"] == "validation_error", plan_id
    assert s.provider.calls == []
    with tenant_context(s.a.merchant.id), tenant_atomic():
        assert Subscription.objects.count() == 0


@pytest.mark.parametrize("which", UNSET)
def test_an_offered_plan_with_unset_credentials_is_503_and_creates_nothing(s, settings, which):
    unset(settings, which)
    resp = post(s, str(s.small.id))
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "billing_not_configured"
    assert s.provider.calls == []
    with tenant_context(s.a.merchant.id), tenant_atomic():
        assert Subscription.objects.count() == 0
    assert billing_audits(s.a.merchant) == []


@pytest.mark.parametrize("status", ["INCOMPLETE", "ACTIVE", "CANCELLED", "EXPIRED"])
def test_an_offered_plan_with_unset_credentials_on_an_existing_row_is_503_and_changes_nothing(
    s, settings, status
):
    """The configuration check precedes every provider call and local write,
    whatever the row state (a plan change on ACTIVE included)."""
    s.sub(s.a, status, provider_status="active" if status == "ACTIVE" else None)
    s.provider.entities[REF] = {"id": REF, "status": "active", "plan_id": "plan_small"}
    before = read_state(s.a.merchant)
    unset(settings, "both")

    resp = post(s, str(s.big.id))

    assert resp.status_code == 503 and resp.json()["error"]["code"] == "billing_not_configured"
    assert s.provider.calls == []
    assert read_state(s.a.merchant) == before


def test_the_422_does_not_depend_on_the_row_state_when_credentials_are_unset(s, settings):
    s.sub(s.a, "ACTIVE")
    unset(settings, "both")
    resp = post(s, str(uuid.uuid4()))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"
    assert s.provider.calls == []
