"""API, permission, security and concurrency tests for TOTP 2FA. No
external providers are used; recovery/TOTP codes never leave this process.
"""
import threading
from unittest import mock

import pyotp
import pytest
from cryptography.fernet import Fernet
from django.db import connection
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone as dj_timezone
from rest_framework.test import APIClient

from accounts import services
from accounts.exceptions import InvalidCredentials
from accounts.models import TeamMember, User
from auditlog.models import AuditLog
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

LOGIN = "/api/v1/auth/login"
LOGIN_TOTP = "/api/v1/auth/login/totp"
SETUP = "/api/v1/auth/2fa/setup"
CONFIRM = "/api/v1/auth/2fa/confirm"
DISABLE = "/api/v1/auth/2fa/disable"
MERCHANT = "/api/v1/merchant"
TEAM_MEMBERS = "/api/v1/team-members"
REFRESH = "/api/v1/auth/refresh"


@pytest.fixture(autouse=True)
def fernet_key(settings):
    settings.FERNET_KEY = Fernet.generate_key().decode()


def assert_error_shape(body, code=None):
    assert set(body) == {"error"}
    assert "code" in body["error"] and "message" in body["error"]
    if code:
        assert body["error"]["code"] == code


def _post(client, url, data=None, token=None):
    kwargs = {"HTTP_X_CSRFTOKEN": token} if token else {}
    return client.post(url, data or {}, format="json", **kwargs)


def _fresh_client():
    client = APIClient(enforce_csrf_checks=True)
    client.get(LOGIN)
    return client, client.cookies["csrftoken"].value


def enroll(client, password):
    """Runs setup+confirm through the real endpoints. Returns (secret, recovery_codes).

    Confirms with the previous time step's code: confirm consumes the step
    it accepts (replay protection), so confirming with .now() would make a
    test's next .now() code a replay within the same 30 s window."""
    resp = _post(client, SETUP, {"password": password}, client.csrf)
    assert resp.status_code == 200, resp.content
    secret = resp.json()["secret"]
    confirm_code = pyotp.TOTP(secret).at(dj_timezone.now(), counter_offset=-1)
    resp = _post(client, CONFIRM, {"code": confirm_code}, client.csrf)
    assert resp.status_code == 200, resp.content
    return secret, resp.json()["recovery_codes"]


def login_step1(email, password):
    """Independent client, step 1 only. Returns (client, csrf_token, response)."""
    client, token = _fresh_client()
    resp = _post(client, LOGIN, {"email": email, "password": password}, token)
    return client, token, resp


# --- Setup ------------------------------------------------------------


def test_setup_right_password_returns_secret_and_stores_only_the_encrypted_value(
    session_client, make_merchant, password
):
    member = make_merchant()
    client = session_client(member.user.email)

    resp = _post(client, SETUP, {"password": password}, client.csrf)

    assert resp.status_code == 200
    body = resp.json()
    assert body["secret"] in body["otpauth_uri"]
    stored = User.objects.get(pk=member.user_id).totp_secret_encrypted
    assert stored is not None and stored != body["secret"]


def test_setup_wrong_password_returns_400_reauthentication_failed(session_client, make_merchant):
    client = session_client(make_merchant().user.email)
    resp = _post(client, SETUP, {"password": "wrong"}, client.csrf)
    assert resp.status_code == 400
    assert_error_shape(resp.json(), "reauthentication_failed")


def test_setup_already_enrolled_returns_409(session_client, make_merchant, password):
    member = make_merchant()
    client = session_client(member.user.email)
    enroll(client, password)

    resp = _post(client, SETUP, {"password": password}, client.csrf)

    assert resp.status_code == 409
    assert_error_shape(resp.json(), "totp_already_enabled")


def test_setup_second_call_before_confirm_replaces_the_pending_secret(
    session_client, make_merchant, password
):
    client = session_client(make_merchant().user.email)
    secret1 = _post(client, SETUP, {"password": password}, client.csrf).json()["secret"]
    secret2 = _post(client, SETUP, {"password": password}, client.csrf).json()["secret"]
    assert secret1 != secret2

    resp = _post(client, CONFIRM, {"code": pyotp.TOTP(secret2).now()}, client.csrf)
    assert resp.status_code == 200


def test_setup_anonymous_returns_403():
    resp = APIClient().post(SETUP, {"password": "x"}, format="json")
    assert resp.status_code == 403


# --- Confirm ------------------------------------------------------------


def test_confirm_valid_code_returns_10_unique_codes_and_enables_2fa(
    session_client, make_merchant, password
):
    member = make_merchant()
    client = session_client(member.user.email)

    _secret, codes = enroll(client, password)

    assert len(codes) == 10
    assert len(set(codes)) == 10
    refreshed = _post(client, REFRESH, token=client.csrf)
    assert refreshed.json()["user"]["totp_enabled"] is True


def test_confirm_writes_exactly_one_audit_row_for_the_session_merchant(
    session_client, make_merchant, password
):
    member = make_merchant()
    client = session_client(member.user.email)
    enroll(client, password)

    with tenant_context(member.merchant_id), tenant_atomic():
        rows = list(AuditLog.objects.filter(action=services.AUDIT_TOTP_ENABLED))
    assert len(rows) == 1
    assert rows[0].merchant_id == member.merchant_id
    assert rows[0].actor_user_id == member.user_id


def test_confirm_wrong_code_returns_400_invalid_totp_code(session_client, make_merchant, password):
    client = session_client(make_merchant().user.email)
    _post(client, SETUP, {"password": password}, client.csrf)

    resp = _post(client, CONFIRM, {"code": "000000"}, client.csrf)

    assert resp.status_code == 400
    assert_error_shape(resp.json(), "invalid_totp_code")


def test_confirm_without_pending_secret_returns_409_totp_setup_required(session_client, make_merchant):
    client = session_client(make_merchant().user.email)
    resp = _post(client, CONFIRM, {"code": "123456"}, client.csrf)
    assert resp.status_code == 409
    assert_error_shape(resp.json(), "totp_setup_required")


def test_confirm_already_enrolled_returns_409(session_client, make_merchant, password):
    client = session_client(make_merchant().user.email)
    secret, _codes = enroll(client, password)

    resp = _post(client, CONFIRM, {"code": pyotp.TOTP(secret).now()}, client.csrf)

    assert resp.status_code == 409
    assert_error_shape(resp.json(), "totp_already_enabled")


# --- Login step 1 --------------------------------------------------------


def test_login_enrolled_step1_returns_totp_required_with_no_authenticated_state(
    session_client, make_merchant, password
):
    member = make_merchant()
    enroll(session_client(member.user.email), password)

    client, _token, resp = login_step1(member.user.email, password)

    assert resp.status_code == 200
    assert resp.json() == {"totp_required": True}
    assert "_auth_user_id" not in client.session
    assert "merchant_id" not in client.session
    assert "pending_totp" in client.session


def test_login_enrolled_wrong_password_returns_generic_401_without_totp_leak(
    session_client, make_merchant, password
):
    member = make_merchant()
    enroll(session_client(member.user.email), password)
    client, token = _fresh_client()

    with mock.patch("rest_framework.throttling.SimpleRateThrottle.allow_request", return_value=True):
        resp = _post(client, LOGIN, {"email": member.user.email, "password": "wrong"}, token)

    assert resp.status_code == 401
    assert_error_shape(resp.json(), "invalid_credentials")


# --- Security: pending session is not authorized ---------------------------


def test_pending_session_cannot_reach_authenticated_endpoints(session_client, make_merchant, password):
    member = make_merchant()
    enroll(session_client(member.user.email), password)
    client, token, _resp = login_step1(member.user.email, password)

    assert client.get(MERCHANT).status_code == 403
    assert client.get(TEAM_MEMBERS).status_code == 403
    assert _post(client, REFRESH, token=token).status_code == 403
    assert _post(client, SETUP, {"password": password}, token).status_code == 403


# --- Security: session fixation ---------------------------------------------


def test_session_fixation_step1_for_enrolled_user_b_deauthenticates_user_a(
    session_client, make_merchant, password
):
    a = make_merchant()
    b = make_merchant()
    enroll(session_client(b.user.email), password)

    client = session_client(a.user.email)  # logged in as A
    token = client.csrf

    resp = _post(client, LOGIN, {"email": b.user.email, "password": password}, token)

    assert resp.status_code == 200
    assert resp.json() == {"totp_required": True}
    assert "_auth_user_id" not in client.session
    assert client.get(MERCHANT).status_code == 403


def test_session_key_changes_between_step1_and_step2(session_client, make_merchant, password):
    member = make_merchant()
    secret, _codes = enroll(session_client(member.user.email), password)
    client, token, _resp = login_step1(member.user.email, password)
    step1_key = client.cookies["sessionid"].value

    resp = _post(client, LOGIN_TOTP, {"code": pyotp.TOTP(secret).now()}, token)

    assert resp.status_code == 200
    assert client.cookies["sessionid"].value != step1_key


# --- Step 2: success and generic failures -----------------------------------


def test_step2_valid_totp_code_completes_login_and_grants_merchant_access(
    session_client, make_merchant, password
):
    member = make_merchant()
    secret, _codes = enroll(session_client(member.user.email), password)
    client, token, _resp = login_step1(member.user.email, password)

    resp = _post(client, LOGIN_TOTP, {"code": pyotp.TOTP(secret).now()}, token)

    assert resp.status_code == 200
    assert resp.json()["user"]["totp_enabled"] is True
    assert client.get(MERCHANT).status_code == 200


def test_step2_without_pending_marker_returns_401(make_merchant):
    client, token = _fresh_client()
    resp = _post(client, LOGIN_TOTP, {"code": "123456"}, token)
    assert resp.status_code == 401
    assert_error_shape(resp.json(), "invalid_credentials")


def test_step2_marker_older_than_5_minutes_returns_401(session_client, make_merchant, password):
    member = make_merchant()
    secret, _codes = enroll(session_client(member.user.email), password)
    client, token, _resp = login_step1(member.user.email, password)

    session = client.session
    session["pending_totp"]["iat"] -= 301
    session.save()

    resp = _post(client, LOGIN_TOTP, {"code": pyotp.TOTP(secret).now()}, token)
    assert resp.status_code == 401
    assert_error_shape(resp.json(), "invalid_credentials")


def test_step2_wrong_code_returns_401(session_client, make_merchant, password):
    member = make_merchant()
    enroll(session_client(member.user.email), password)
    client, token, _resp = login_step1(member.user.email, password)

    resp = _post(client, LOGIN_TOTP, {"code": "000000"}, token)
    assert resp.status_code == 401
    assert_error_shape(resp.json(), "invalid_credentials")


def test_step2_user_deactivated_between_step1_and_step2_returns_401(
    session_client, make_merchant, password
):
    member = make_merchant()
    secret, _codes = enroll(session_client(member.user.email), password)
    client, token, _resp = login_step1(member.user.email, password)

    User.objects.filter(pk=member.user_id).update(is_active=False)

    resp = _post(client, LOGIN_TOTP, {"code": pyotp.TOTP(secret).now()}, token)
    assert resp.status_code == 401
    assert_error_shape(resp.json(), "invalid_credentials")


# --- Replay ---------------------------------------------------------------


def test_step2_replay_of_the_same_totp_code_is_rejected_on_a_fresh_step1(
    session_client, make_merchant, password
):
    member = make_merchant()
    secret, _codes = enroll(session_client(member.user.email), password)
    code = pyotp.TOTP(secret).now()

    with mock.patch("rest_framework.throttling.SimpleRateThrottle.allow_request", return_value=True):
        first_client, first_token, _ = login_step1(member.user.email, password)
        first = _post(first_client, LOGIN_TOTP, {"code": code}, first_token)
        second_client, second_token, _ = login_step1(member.user.email, password)
        second = _post(second_client, LOGIN_TOTP, {"code": code}, second_token)

    assert first.status_code == 200
    assert second.status_code == 401


# --- Attempt cap -----------------------------------------------------------


def test_step2_attempt_cap_blocks_the_correct_code_after_5_failures(
    session_client, make_merchant, password
):
    member = make_merchant()
    secret, _codes = enroll(session_client(member.user.email), password)
    client, token, _resp = login_step1(member.user.email, password)

    with mock.patch("rest_framework.throttling.SimpleRateThrottle.allow_request", return_value=True):
        for _ in range(5):
            resp = _post(client, LOGIN_TOTP, {"code": "000000"}, token)
            assert resp.status_code == 401
        resp = _post(client, LOGIN_TOTP, {"code": pyotp.TOTP(secret).now()}, token)

    assert resp.status_code == 401
    assert "pending_totp" not in client.session


# --- Recovery codes ---------------------------------------------------------


def test_step2_recovery_code_accepts_case_and_hyphen_variants(session_client, make_merchant, password):
    member = make_merchant()
    _secret, codes = enroll(session_client(member.user.email), password)
    variant = codes[0].upper().replace("-", "")
    client, token, _resp = login_step1(member.user.email, password)

    resp = _post(client, LOGIN_TOTP, {"code": variant}, token)

    assert resp.status_code == 200


def test_step2_recovery_code_removed_after_use_and_writes_audit_row(
    session_client, make_merchant, password
):
    member = make_merchant()
    _secret, codes = enroll(session_client(member.user.email), password)
    client, token, _resp = login_step1(member.user.email, password)

    _post(client, LOGIN_TOTP, {"code": codes[0]}, token)

    assert len(User.objects.get(pk=member.user_id).totp_recovery_code_hashes) == 9
    with tenant_context(member.merchant_id), tenant_atomic():
        rows = list(AuditLog.objects.filter(action=services.AUDIT_TOTP_RECOVERY_USED))
    assert len(rows) == 1
    assert rows[0].metadata_json == {"recovery_codes_remaining": 9}


def test_step2_recovery_code_fails_on_reuse(session_client, make_merchant, password):
    member = make_merchant()
    _secret, codes = enroll(session_client(member.user.email), password)

    with mock.patch("rest_framework.throttling.SimpleRateThrottle.allow_request", return_value=True):
        first_client, first_token, _ = login_step1(member.user.email, password)
        first = _post(first_client, LOGIN_TOTP, {"code": codes[0]}, first_token)
        second_client, second_token, _ = login_step1(member.user.email, password)
        second = _post(second_client, LOGIN_TOTP, {"code": codes[0]}, second_token)

    assert first.status_code == 200
    assert second.status_code == 401


# --- Concurrency (real PostgreSQL) ------------------------------------------


def _race_complete_totp_login(user, code):
    """Two threads call complete_totp_login with the same code, each on its
    own DB connection. Returns the outcome counts."""
    barrier = threading.Barrier(2)
    results = []

    def worker():
        pending = services.make_pending_totp(user)
        barrier.wait()
        try:
            services.complete_totp_login(pending=pending, code=code)
            results.append("ok")
        except InvalidCredentials:
            results.append("fail")
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


@pytest.mark.django_db(transaction=True)
def test_concurrent_step2_with_the_same_recovery_code_results_in_exactly_one_success(
    session_client, make_merchant, password
):
    member = make_merchant()
    _secret, codes = enroll(session_client(member.user.email), password)
    user = User.objects.get(pk=member.user_id)

    results = _race_complete_totp_login(user, codes[0])

    assert results.count("ok") == 1
    assert results.count("fail") == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_step2_with_the_same_totp_code_results_in_exactly_one_success(
    session_client, make_merchant, password
):
    member = make_merchant()
    secret, _codes = enroll(session_client(member.user.email), password)
    user = User.objects.get(pk=member.user_id)
    code = pyotp.TOTP(secret).now()

    results = _race_complete_totp_login(user, code)

    assert results.count("ok") == 1
    assert results.count("fail") == 1


# --- Step-2 auth race --------------------------------------------------------


def test_step2_fails_when_teammember_is_revoked_between_step1_and_step2(
    session_client, make_merchant, add_member, password
):
    owner = make_merchant()
    viewer = add_member(owner.merchant, "VIEWER", "viewer@example.com")
    secret, _codes = enroll(session_client("viewer@example.com"), password)
    client, token, _resp = login_step1("viewer@example.com", password)

    with tenant_context(owner.merchant_id), tenant_atomic():
        TeamMember.objects.filter(pk=viewer.pk).delete()

    resp = _post(client, LOGIN_TOTP, {"code": pyotp.TOTP(secret).now()}, token)

    assert resp.status_code == 401
    assert_error_shape(resp.json(), "invalid_credentials")


def test_step2_role_change_between_step1_and_step2_is_reflected_in_session_body(
    session_client, make_merchant, add_member, password
):
    owner = make_merchant()
    viewer = add_member(owner.merchant, "VIEWER", "viewer@example.com")
    secret, _codes = enroll(session_client("viewer@example.com"), password)
    client, token, _resp = login_step1("viewer@example.com", password)

    with tenant_context(owner.merchant_id):
        services.change_team_member_role(actor=owner, member_id=viewer.pk, role="MANAGER")

    resp = _post(client, LOGIN_TOTP, {"code": pyotp.TOTP(secret).now()}, token)

    assert resp.status_code == 200
    assert resp.json()["role"] == "MANAGER"


# --- Disable ----------------------------------------------------------------


def test_disable_with_totp_code_returns_204_clears_fields_and_writes_audit_row(
    session_client, make_merchant, password
):
    member = make_merchant()
    client = session_client(member.user.email)
    secret, _codes = enroll(client, password)

    resp = _post(client, DISABLE, {"password": password, "code": pyotp.TOTP(secret).now()}, client.csrf)

    assert resp.status_code == 204
    user = User.objects.get(pk=member.user_id)
    assert user.totp_secret_encrypted is None
    assert user.totp_confirmed_at is None
    assert user.totp_last_used_step is None
    assert user.totp_recovery_code_hashes == []
    with tenant_context(member.merchant_id), tenant_atomic():
        rows = list(AuditLog.objects.filter(action=services.AUDIT_TOTP_DISABLED))
    assert len(rows) == 1


def test_disable_with_recovery_code_returns_204(session_client, make_merchant, password):
    client = session_client(make_merchant().user.email)
    _secret, codes = enroll(client, password)

    resp = _post(client, DISABLE, {"password": password, "code": codes[0]}, client.csrf)

    assert resp.status_code == 204


def test_disable_wrong_password_returns_400_reauthentication_failed(session_client, make_merchant, password):
    client = session_client(make_merchant().user.email)
    secret, _codes = enroll(client, password)

    resp = _post(client, DISABLE, {"password": "wrong", "code": pyotp.TOTP(secret).now()}, client.csrf)

    assert resp.status_code == 400
    assert_error_shape(resp.json(), "reauthentication_failed")


def test_disable_wrong_code_returns_the_same_400_reauthentication_failed(
    session_client, make_merchant, password
):
    client = session_client(make_merchant().user.email)
    enroll(client, password)

    resp = _post(client, DISABLE, {"password": password, "code": "000000"}, client.csrf)

    assert resp.status_code == 400
    assert_error_shape(resp.json(), "reauthentication_failed")


def test_disable_not_enrolled_returns_409_totp_not_enabled(session_client, make_merchant, password):
    client = session_client(make_merchant().user.email)
    resp = _post(client, DISABLE, {"password": password, "code": "123456"}, client.csrf)
    assert resp.status_code == 409
    assert_error_shape(resp.json(), "totp_not_enabled")


def test_login_is_single_step_again_after_disable(session_client, make_merchant, password):
    member = make_merchant()
    client = session_client(member.user.email)
    secret, _codes = enroll(client, password)
    _post(client, DISABLE, {"password": password, "code": pyotp.TOTP(secret).now()}, client.csrf)

    fresh, token = _fresh_client()
    resp = _post(fresh, LOGIN, {"email": member.user.email, "password": password}, token)

    assert resp.status_code == 200
    assert "totp_required" not in resp.json()
    assert resp.json()["user"]["totp_enabled"] is False


# --- Permissions -------------------------------------------------------------


def test_setup_confirm_disable_all_return_403_for_anonymous():
    client = APIClient()
    assert client.post(SETUP, {"password": "x"}, format="json").status_code == 403
    assert client.post(CONFIRM, {"code": "123456"}, format="json").status_code == 403
    assert client.post(DISABLE, {"password": "x", "code": "123456"}, format="json").status_code == 403


@pytest.mark.parametrize("role", ["OWNER", "ADMIN", "MANAGER", "VIEWER"])
def test_setup_confirm_and_disable_allowed_for_every_role(
    session_client, make_merchant, add_member, password, role
):
    owner = make_merchant()
    if role == "OWNER":
        client = session_client(owner.user.email)
    else:
        email = f"{role.lower()}@example.com"
        add_member(owner.merchant, role, email)
        client = session_client(email)

    secret, _codes = enroll(client, password)  # exercises setup + confirm
    resp = _post(client, DISABLE, {"password": password, "code": pyotp.TOTP(secret).now()}, client.csrf)

    assert resp.status_code == 204


def test_totp_endpoints_act_only_on_request_user_ignoring_a_body_user_id(
    session_client, make_merchant, add_member, password
):
    owner = make_merchant()
    victim = add_member(owner.merchant, "VIEWER", "victim@example.com")
    client = session_client(owner.user.email)

    resp = _post(client, SETUP, {"password": password, "user_id": str(victim.user_id)}, client.csrf)

    assert resp.status_code == 200
    assert User.objects.get(pk=victim.user_id).totp_secret_encrypted is None
    assert User.objects.get(pk=owner.user_id).totp_secret_encrypted is not None


# --- Throttles ----------------------------------------------------------


def test_login_totp_scope_returns_429_after_5_requests_per_minute(session_client, make_merchant, password):
    member = make_merchant()
    enroll(session_client(member.user.email), password)
    client, token, _resp = login_step1(member.user.email, password)

    statuses = [_post(client, LOGIN_TOTP, {"code": "000000"}, token).status_code for _ in range(6)]

    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429


def test_totp_manage_scope_returns_429_after_5_requests_per_minute(session_client, make_merchant):
    client = session_client(make_merchant().user.email)

    statuses = [_post(client, SETUP, {"password": "wrong"}, client.csrf).status_code for _ in range(6)]

    assert statuses[:5] == [400] * 5
    assert statuses[5] == 429


# --- Tenant isolation ---------------------------------------------------


def test_totp_audit_rows_are_visible_only_in_their_own_merchant_tenant_context(
    session_client, make_merchant, password
):
    a = make_merchant()
    b = make_merchant()
    enroll(session_client(a.user.email), password)

    with tenant_context(a.merchant_id), tenant_atomic():
        assert AuditLog.objects.filter(action=services.AUDIT_TOTP_ENABLED).exists()
    with tenant_context(b.merchant_id), tenant_atomic():
        assert not AuditLog.objects.filter(action=services.AUDIT_TOTP_ENABLED).exists()


# --- Redaction ------------------------------------------------------------


def test_no_response_other_than_setup_confirm_contains_the_secret_or_recovery_codes(
    session_client, make_merchant, password
):
    member = make_merchant()
    client = session_client(member.user.email)
    secret, codes = enroll(client, password)

    fresh, token = _fresh_client()
    login_resp = _post(fresh, LOGIN, {"email": member.user.email, "password": password}, token)
    totp_resp = _post(fresh, LOGIN_TOTP, {"code": pyotp.TOTP(secret).now()}, token)
    refresh_resp = _post(fresh, REFRESH, token=token)
    disable_resp = _post(client, DISABLE, {"password": password, "code": codes[0]}, client.csrf)

    for resp in (login_resp, totp_resp, refresh_resp, disable_resp):
        text = resp.content.decode()
        assert secret not in text
        for code in codes:
            assert code not in text
            assert code.replace("-", "") not in text


def test_audit_metadata_never_contains_a_code_hash_or_secret(session_client, make_merchant, password):
    member = make_merchant()
    client = session_client(member.user.email)
    secret, codes = enroll(client, password)
    fresh, token = _fresh_client()
    _post(fresh, LOGIN, {"email": member.user.email, "password": password}, token)
    _post(fresh, LOGIN_TOTP, {"code": codes[0]}, token)

    with tenant_context(member.merchant_id), tenant_atomic():
        rows = AuditLog.objects.filter(action__startswith="user.totp")
        remaining_hashes = User.objects.get(pk=member.user_id).totp_recovery_code_hashes
        for row in rows:
            payload = str(row.metadata_json)
            assert secret not in payload
            for code in codes:
                assert code not in payload
                assert code.replace("-", "") not in payload
            for stored_hash in remaining_hashes:
                assert stored_hash not in payload


def test_captured_logs_during_totp_flow_contain_no_secret_or_codes(
    session_client, make_merchant, password, caplog
):
    member = make_merchant()
    client = session_client(member.user.email)
    with caplog.at_level("DEBUG"):
        secret, codes = enroll(client, password)
        fresh, token = _fresh_client()
        _post(fresh, LOGIN, {"email": member.user.email, "password": password}, token)
        _post(fresh, LOGIN_TOTP, {"code": codes[0]}, token)

    log_text = caplog.text
    assert secret not in log_text
    for code in codes:
        assert code not in log_text


# --- Django Admin ------------------------------------------------------


def test_admin_user_change_page_shows_confirmed_at_and_hides_totp_secrets(make_merchant, password):
    member = make_merchant()
    with tenant_context(member.merchant_id):
        secret, _uri = services.begin_totp_setup(user=member.user, password=password)
        services.confirm_totp_setup(user=member.user, code=pyotp.TOTP(secret).now())

    staff = User.objects.create_superuser("staff@example.com", password)
    admin_client = DjangoClient()
    admin_client.force_login(staff)

    resp = admin_client.get(reverse("admin:accounts_user_change", args=[member.user_id]))

    assert resp.status_code == 200
    content = resp.content.decode()
    assert "totp_confirmed_at" in content
    stored_secret = User.objects.get(pk=member.user_id).totp_secret_encrypted
    assert stored_secret not in content
    assert secret not in content
    for stored_hash in User.objects.get(pk=member.user_id).totp_recovery_code_hashes:
        assert stored_hash not in content


def test_admin_change_form_post_cannot_overwrite_totp_secret_fields(make_merchant, password):
    """UserEditForm excludes the TOTP columns, so even a staff POST to the
    change form (e.g. a crafted request replaying hidden-field values) must
    leave them untouched -- the spec requires they are never posted, not
    just never rendered."""
    member = make_merchant()
    with tenant_context(member.merchant_id):
        secret, _uri = services.begin_totp_setup(user=member.user, password=password)
        services.confirm_totp_setup(user=member.user, code=pyotp.TOTP(secret).now())
    before = User.objects.get(pk=member.user_id)

    staff = User.objects.create_superuser("staff2@example.com", password)
    admin_client = DjangoClient()
    admin_client.force_login(staff)

    resp = admin_client.post(
        reverse("admin:accounts_user_change", args=[member.user_id]),
        {
            "email": before.email,
            "is_active": "on",
            "totp_secret_encrypted": "tampered",
            "totp_last_used_step": "999999",
            "totp_recovery_code_hashes": "[]",
            "initial-password": before.password,
            "password": before.password,
        },
    )

    after = User.objects.get(pk=member.user_id)
    assert resp.status_code in (200, 302)
    assert after.totp_secret_encrypted == before.totp_secret_encrypted
    assert after.totp_last_used_step == before.totp_last_used_step
    assert after.totp_recovery_code_hashes == before.totp_recovery_code_hashes
