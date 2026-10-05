"""Billing endpoint permissions beyond the role matrix in test_api.py (spec 07
Definition of done, "Roles": OWNER and ADMIN read, only OWNER mutates;
MANAGER/VIEWER/API keys refused). These cover the principal itself: no
session, a membership that is no longer active, a merchant that is not ACTIVE,
and a session whose merchant is not the user's.

Every refused request must also leave billing state untouched and make no
provider call. Razorpay is the FakeProvider from conftest."""
import pytest
from rest_framework.test import APIClient

from accounts.models import Merchant, TeamMember
from billing.models import Subscription
from billing.tests.snapshot_helpers import entity
from billing.tests.state_helpers import read_state
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

PLANS = "/api/v1/billing/plans"
SUBSCRIPTION = "/api/v1/billing/subscription"
CHECKOUT = "/api/v1/billing/checkout"
CANCEL = "/api/v1/billing/subscription/cancel"


@pytest.fixture
def w(lifecycle_setup):
    """A has a PAST_DUE subscription (cancellable), B has an ACTIVE one."""
    w = lifecycle_setup
    w.sub(w.a, "PAST_DUE")
    w.sub(w.b, "ACTIVE")
    return w


def every_endpoint(client, plan_id):
    csrf = getattr(client, "csrf", "")
    return {
        "plans": client.get(PLANS).status_code,
        "subscription": client.get(SUBSCRIPTION).status_code,
        "checkout": client.post(CHECKOUT, {"plan_id": str(plan_id)}, format="json", HTTP_X_CSRFTOKEN=csrf).status_code,
        "cancel": client.post(CANCEL, {}, format="json", HTTP_X_CSRFTOKEN=csrf).status_code,
    }


def assert_nothing_changed(w, before_a, before_b):
    assert read_state(w.a.merchant) == before_a
    assert read_state(w.b.merchant) == before_b
    assert w.provider.calls == []


def refused(codes):
    return all(code in (401, 403) for code in codes.values())


def test_no_session_is_refused_everywhere_and_changes_nothing(w):
    before_a, before_b = read_state(w.a.merchant), read_state(w.b.merchant)
    codes = every_endpoint(APIClient(enforce_csrf_checks=True), w.small.id)
    assert refused(codes), codes
    assert_nothing_changed(w, before_a, before_b)


def test_a_revoked_owner_with_a_live_session_is_refused(w, session_client):
    client = session_client(w.a.user.email)
    with tenant_context(w.a.merchant.id), tenant_atomic():
        TeamMember.objects.filter(user=w.a.user).delete()  # what revoke_team_member does
    before_a, before_b = read_state(w.a.merchant), read_state(w.b.merchant)
    codes = every_endpoint(client, w.small.id)
    assert refused(codes), codes
    assert_nothing_changed(w, before_a, before_b)


def test_an_owner_whose_membership_is_not_accepted_is_refused(w, session_client):
    client = session_client(w.a.user.email)
    with tenant_context(w.a.merchant.id), tenant_atomic():
        TeamMember.objects.filter(user=w.a.user).update(accepted_at=None)
    before_a, before_b = read_state(w.a.merchant), read_state(w.b.merchant)
    codes = every_endpoint(client, w.small.id)
    assert refused(codes), codes
    assert_nothing_changed(w, before_a, before_b)


@pytest.mark.parametrize("status", [Merchant.Status.SUSPENDED, Merchant.Status.DELETED])
def test_an_owner_of_a_merchant_that_is_not_active_is_refused(w, session_client, status):
    """get_active_membership() requires an ACTIVE merchant (Phase 02). Billing
    sync keeps running for SUSPENDED (O14); the dashboard endpoints do not."""
    client = session_client(w.a.user.email)
    Merchant.objects.filter(pk=w.a.merchant.id).update(status=status)
    before_a, before_b = read_state(w.a.merchant), read_state(w.b.merchant)
    codes = every_endpoint(client, w.small.id)
    assert refused(codes), codes
    assert_nothing_changed(w, before_a, before_b)


def test_a_session_pointing_at_another_merchant_is_refused(w, session_client):
    """B's OWNER with a session whose merchant_id names A (a server-side
    session bug): no membership in A, so no access to A's billing."""
    client = session_client(w.b.user.email)
    session = client.session
    session["merchant_id"] = str(w.a.merchant.id)
    session.save()
    before_a, before_b = read_state(w.a.merchant), read_state(w.b.merchant)
    codes = every_endpoint(client, w.small.id)
    assert refused(codes), codes
    assert_nothing_changed(w, before_a, before_b)


def test_an_owner_only_ever_reads_and_cancels_its_own_merchants_subscription(w, session_client):
    """B's OWNER: the read is B's row and the cancel acts on B only. A's
    PAST_DUE row is never touched."""
    before_a = read_state(w.a.merchant)
    client = session_client(w.b.user.email)
    body = client.get(SUBSCRIPTION).json()
    assert body["status"] == "ACTIVE"  # B's, not A's PAST_DUE
    w.provider.entities[w.ref(w.b)] = entity(w.small, ref=w.ref(w.b))  # B's stored period
    resp = client.post(CANCEL, {}, format="json", HTTP_X_CSRFTOKEN=client.csrf)
    assert resp.status_code == 200 and resp.json()["cancel_at_period_end"] is True  # B cancels at period end
    assert read_state(w.a.merchant) == before_a
    assert all(call[1] == w.ref(w.b) for call in w.provider.calls if len(call) > 1)
    with tenant_context(w.a.merchant.id), tenant_atomic():
        assert Subscription.objects.get().status == "PAST_DUE"
