from datetime import timedelta
from unittest import mock

import pytest
from django.contrib.sessions.models import Session
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone as dj_timezone
from rest_framework.test import APIClient

from accounts.models import Merchant, TeamMember, User

pytestmark = pytest.mark.django_db

LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
REFRESH = "/api/v1/auth/refresh"
MERCHANT = "/api/v1/merchant"


def assert_error_shape(body, code=None):
    assert set(body) == {"error"}
    assert "code" in body["error"] and "message" in body["error"]
    if code:
        assert body["error"]["code"] == code


def _post(client, url, data=None, token=None):
    kwargs = {"HTTP_X_CSRFTOKEN": token} if token else {}
    return client.post(url, data or {}, format="json", **kwargs)


def test_get_login_sets_csrf_cookie_and_returns_204():
    client = APIClient()
    resp = client.get(LOGIN)
    assert resp.status_code == 204
    assert "csrftoken" in resp.cookies


def test_login_valid_credentials_returns_user_merchant_role_and_sets_session(login, make_merchant):
    member = make_merchant()
    client, resp = login(member.user.email)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"] == {
        "id": str(member.user_id),
        "email": member.user.email,
        "totp_enabled": False,
    }
    assert body["merchant"] == {"id": str(member.merchant_id), "name": member.merchant.name}
    assert body["role"] == "OWNER"
    assert "sessionid" in client.cookies
    assert client.get(MERCHANT).status_code == 200


def test_login_changes_session_key(csrf_client, make_merchant, password):
    member = make_merchant()
    session = csrf_client.session
    session["seed"] = 1
    session.save()
    csrf_client.cookies["sessionid"] = session.session_key
    old_key = session.session_key
    csrf_client.get(LOGIN)
    token = csrf_client.cookies["csrftoken"].value

    resp = _post(csrf_client, LOGIN, {"email": member.user.email, "password": password}, token)

    assert resp.status_code == 200
    assert csrf_client.cookies["sessionid"].value != old_key


def test_login_failures_all_return_identical_401(login, make_merchant, add_member, password):
    good = make_merchant()
    inactive = make_merchant()
    User.objects.filter(id=inactive.user_id).update(is_active=False)
    no_membership = User.objects.create_user("nomember@example.com", password)
    unaccepted_owner = make_merchant()
    unaccepted_user = User.objects.create_user("pending@example.com", password)
    add_member(unaccepted_owner.merchant, "VIEWER", None, accepted=False, user=unaccepted_user)
    suspended = make_merchant()
    Merchant.objects.filter(id=suspended.merchant_id).update(status=Merchant.Status.SUSPENDED)
    deleted = make_merchant()
    Merchant.objects.filter(id=deleted.merchant_id).update(status=Merchant.Status.DELETED)

    attempts = [
        (good.user.email, "wrong-password"),
        ("ghost@example.com", password),
        (inactive.user.email, password),
        (no_membership.email, password),
        (unaccepted_user.email, password),
        (suspended.user.email, password),
        (deleted.user.email, password),
    ]
    bodies = []
    for email, pw in attempts:
        # fresh client each time so the per-IP throttle is not the cause
        with mock.patch("rest_framework.throttling.SimpleRateThrottle.allow_request", return_value=True):
            _, resp = login(email, pw)
        assert resp.status_code == 401, (email, resp.content)
        bodies.append(resp.json())
    assert all(b == bodies[0] for b in bodies)
    assert_error_shape(bodies[0])


def test_login_sixth_rapid_attempt_from_one_ip_returns_429(login, make_merchant):
    member = make_merchant()
    client = APIClient(enforce_csrf_checks=True)
    statuses = [login(member.user.email, "wrong", client=client)[1].status_code for _ in range(6)]
    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429


def test_login_without_csrf_token_returns_403(make_merchant, password):
    member = make_merchant()
    client = APIClient(enforce_csrf_checks=True)
    resp = _post(client, LOGIN, {"email": member.user.email, "password": password})
    assert resp.status_code == 403


def test_logout_returns_204_then_merchant_is_403(session_client, make_merchant):
    member = make_merchant()
    client = session_client(member.user.email)

    resp = _post(client, LOGOUT, token=client.csrf)

    assert resp.status_code == 204
    after = client.get(MERCHANT)
    assert after.status_code == 403
    assert_error_shape(after.json())


def test_logout_without_csrf_token_returns_403(session_client, make_merchant):
    client = session_client(make_merchant().user.email)
    assert _post(client, LOGOUT).status_code == 403
    assert client.get(MERCHANT).status_code == 200  # session not flushed


def test_refresh_returns_login_body_and_moves_session_expiry_forward(session_client, make_merchant):
    member = make_merchant()
    client = session_client(member.user.email)
    key = client.cookies["sessionid"].value
    soon = dj_timezone.now() + timedelta(minutes=5)
    Session.objects.filter(session_key=key).update(expire_date=soon)

    resp = _post(client, REFRESH, token=client.csrf)

    assert resp.status_code == 200
    assert resp.json()["merchant"]["id"] == str(member.merchant_id)
    assert resp.json()["role"] == "OWNER"
    assert resp.json()["user"]["email"] == member.user.email
    new_key = client.cookies["sessionid"].value
    expire = Session.objects.get(session_key=new_key).expire_date
    assert expire > soon + timedelta(minutes=30)


def test_refresh_without_csrf_token_returns_403(session_client, make_merchant):
    client = session_client(make_merchant().user.email)
    assert _post(client, REFRESH).status_code == 403


def test_refresh_never_sets_current_user_id_for_multi_merchant_user(
    session_client, make_merchant, add_member
):
    a, b = make_merchant(), make_merchant()
    add_member(b.merchant, "VIEWER", None, user=a.user)
    client = session_client(a.user.email)

    with CaptureQueriesContext(connection) as ctx:
        resp = _post(client, REFRESH, token=client.csrf)

    assert resp.status_code == 200
    assert resp.json()["merchant"]["id"] == str(a.merchant_id)
    assert not any("app.current_user_id" in q["sql"] for q in ctx.captured_queries)


def test_refresh_does_not_call_login_lookup(session_client, make_merchant):
    client = session_client(make_merchant().user.email)
    with mock.patch("core.tenancy.user_lookup_atomic", side_effect=AssertionError("lookup used")), \
         mock.patch("accounts.services.resolve_login_membership", side_effect=AssertionError("lookup used")):
        assert _post(client, REFRESH, token=client.csrf).status_code == 200


@pytest.mark.parametrize("url", [LOGOUT, REFRESH])
def test_unauthenticated_logout_and_refresh_return_403_with_error_shape(url):
    client = APIClient()
    resp = client.post(url, {}, format="json")
    assert resp.status_code == 403
    assert_error_shape(resp.json())


def test_unauthenticated_merchant_get_returns_403_with_error_shape():
    resp = APIClient().get(MERCHANT)
    assert resp.status_code == 403
    assert_error_shape(resp.json())


def test_login_validation_error_uses_422_shape():
    client = APIClient()
    client.get(LOGIN)
    resp = client.post(LOGIN, {"email": "not-an-email"}, format="json")
    assert resp.status_code == 422
    assert_error_shape(resp.json(), "validation_error")
    assert "field_errors" in resp.json()["error"]


def test_session_membership_deleted_mid_session_gets_403(session_client, make_merchant, add_member):
    owner = make_merchant()
    viewer_user = add_member(owner.merchant, "VIEWER", "v@example.com")
    client = session_client("v@example.com")
    assert client.get(MERCHANT).status_code == 200

    from core.tenancy import tenant_atomic, tenant_context

    with tenant_context(owner.merchant_id), tenant_atomic():
        TeamMember.objects.filter(id=viewer_user.id).delete()

    assert client.get(MERCHANT).status_code == 403


def test_login_throttle_is_not_bypassed_by_rotating_x_forwarded_for(make_merchant):
    """Security-Controls.md §Rate Limiting: a client-supplied X-Forwarded-For
    must not create a fresh throttle bucket per request."""
    member = make_merchant()
    client = APIClient(enforce_csrf_checks=True)
    client.get(LOGIN)
    token = client.cookies["csrftoken"].value
    statuses = [
        client.post(
            LOGIN,
            {"email": member.user.email, "password": "wrong"},
            format="json",
            HTTP_X_CSRFTOKEN=token,
            HTTP_X_FORWARDED_FOR=f"203.0.113.{i}",
        ).status_code
        for i in range(6)
    ]
    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429


def test_removed_member_can_still_log_out(session_client, make_merchant, add_member):
    owner = make_merchant()
    removed = add_member(owner.merchant, "VIEWER", "gone@example.com")
    client = session_client("gone@example.com")

    from core.tenancy import tenant_atomic, tenant_context

    with tenant_context(owner.merchant_id), tenant_atomic():
        TeamMember.objects.filter(id=removed.id).delete()

    assert client.get(MERCHANT).status_code == 403
    assert _post(client, LOGOUT, token=client.csrf).status_code == 204
    assert "_auth_user_id" not in client.session
