import pytest
from django.db import connection

from accounts.models import Merchant, TeamMember
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

MERCHANT = "/api/v1/merchant"


def _patch(client, data):
    return client.patch(MERCHANT, data, format="json", HTTP_X_CSRFTOKEN=client.csrf)


def test_get_merchant_returns_only_own_merchant_with_null_plan(session_client, make_merchant):
    a, b = make_merchant(), make_merchant()
    client = session_client(a.user.email)

    resp = client.get(MERCHANT)

    assert resp.status_code == 200
    assert resp.json() == {
        "id": str(a.merchant_id),
        "name": a.merchant.name,
        "business_type": None,
        "timezone": "Asia/Kolkata",
        "plan": None,
        "status": "ACTIVE",
    }
    assert str(b.merchant_id) not in resp.content.decode()


@pytest.mark.parametrize("role", ["OWNER", "ADMIN", "MANAGER", "VIEWER"])
def test_get_merchant_allowed_for_every_role(role, session_client, make_merchant, add_member):
    owner = make_merchant()
    email = owner.user.email
    if role != "OWNER":
        email = f"{role.lower()}@example.com"
        add_member(owner.merchant, role, email)
    assert session_client(email).get(MERCHANT).status_code == 200


@pytest.mark.parametrize("role", ["OWNER", "ADMIN"])
def test_patch_merchant_allowed_for_owner_and_admin(role, session_client, make_merchant, add_member):
    owner = make_merchant()
    email = owner.user.email
    if role != "OWNER":
        email = f"{role.lower()}@example.com"
        add_member(owner.merchant, role, email)
    client = session_client(email)

    resp = _patch(client, {"name": "New Name", "business_type": "Cafe", "timezone": "UTC"})

    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"
    assert resp.json()["timezone"] == "UTC"
    assert Merchant.objects.get(id=owner.merchant_id).business_type == "Cafe"


@pytest.mark.parametrize("role", ["MANAGER", "VIEWER"])
def test_patch_merchant_forbidden_for_manager_and_viewer(role, session_client, make_merchant, add_member):
    owner = make_merchant()
    email = f"{role.lower()}@example.com"
    add_member(owner.merchant, role, email)
    client = session_client(email)

    resp = _patch(client, {"name": "Hacked"})

    assert resp.status_code == 403
    assert set(resp.json()) == {"error"}
    assert Merchant.objects.get(id=owner.merchant_id).name == owner.merchant.name


def test_patch_merchant_invalid_timezone_returns_422_with_field_errors(session_client, make_merchant):
    owner = make_merchant()
    client = session_client(owner.user.email)

    resp = _patch(client, {"timezone": "Mars/Olympus"})

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"]
    assert error["field_errors"]["timezone"]
    assert Merchant.objects.get(id=owner.merchant_id).timezone == "Asia/Kolkata"


def test_patch_merchant_without_csrf_token_returns_403(session_client, make_merchant):
    owner = make_merchant()
    client = session_client(owner.user.email)
    resp = client.patch(MERCHANT, {"name": "x"}, format="json")
    assert resp.status_code == 403
    assert Merchant.objects.get(id=owner.merchant_id).name == owner.merchant.name


def test_patch_merchant_ignores_merchant_id_id_and_status_from_body(session_client, make_merchant):
    a, b = make_merchant(), make_merchant()
    client = session_client(a.user.email)

    resp = _patch(
        client,
        {
            "name": "A renamed",
            "merchant_id": str(b.merchant_id),
            "id": str(b.merchant_id),
            "status": "SUSPENDED",
            "plan": "enterprise",
        },
    )

    # Unknown/protected fields are ignored (not rejected): the request succeeds.
    assert resp.status_code == 200
    assert resp.json()["name"] == "A renamed"
    assert resp.json()["id"] == str(a.merchant_id)
    assert resp.json()["status"] == "ACTIVE"
    assert resp.json()["plan"] is None
    b_after = Merchant.objects.get(id=b.merchant_id)
    a_after = Merchant.objects.get(id=a.merchant_id)
    assert b_after.name == b.merchant.name
    assert b_after.status == Merchant.Status.ACTIVE
    assert a_after.status == Merchant.Status.ACTIVE


def test_suspended_merchant_loses_access_on_next_request(session_client, make_merchant):
    owner = make_merchant()
    client = session_client(owner.user.email)
    Merchant.objects.filter(id=owner.merchant_id).update(status=Merchant.Status.SUSPENDED)
    assert client.get(MERCHANT).status_code == 403


@pytest.mark.django_db(transaction=True)
def test_authenticated_request_runs_with_session_merchant_tenant_context(
    session_client, make_merchant
):
    a, b = make_merchant(), make_merchant()
    client = session_client(a.user.email)
    seen = {}
    from accounts import views

    original = views.MerchantView.get

    def spy(self, request):
        with connection.cursor() as cur:
            cur.execute("SELECT current_setting('app.current_merchant_id', true)")
            seen["value"] = cur.fetchone()[0]
        return original(self, request)

    views.MerchantView.get = spy
    try:
        assert client.get(MERCHANT).status_code == 200
    finally:
        views.MerchantView.get = original

    assert seen["value"] == str(a.merchant_id)
    assert seen["value"] != str(b.merchant_id)


@pytest.mark.django_db(transaction=True)
def test_anonymous_request_has_no_tenant_context(make_merchant):
    from rest_framework.test import APIClient

    from accounts import views

    make_merchant()
    seen = {}
    original = views.LoginView.get

    def spy(self, request):
        with connection.cursor() as cur:
            cur.execute("SELECT current_setting('app.current_merchant_id', true)")
            seen["value"] = cur.fetchone()[0]
        return original(self, request)

    views.LoginView.get = spy
    try:
        assert APIClient().get("/api/v1/auth/login").status_code == 204
    finally:
        views.LoginView.get = original
    assert seen["value"] in (None, "")


def test_session_of_other_merchants_member_cannot_read_this_merchant(session_client, make_merchant):
    a, b = make_merchant(), make_merchant()
    client = session_client(b.user.email)
    body = client.get(MERCHANT).json()
    assert body["id"] == str(b.merchant_id)
    assert body["id"] != str(a.merchant_id)
    with tenant_context(a.merchant_id), tenant_atomic():
        assert not TeamMember.objects.filter(user=b.user).exists()
