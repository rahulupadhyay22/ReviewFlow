import pytest
from rest_framework.test import APIClient

from accounts import services
from accounts.models import Merchant, TeamMember
from auditlog.models import AuditLog
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

LOGIN = "/api/v1/auth/login"
ACCEPT = "/api/v1/auth/accept-invite"
STRONG_PASSWORD = "Br1ght-Falcon-Meadow-7!"


def _owner(merchant):
    return TeamMember.objects.get(merchant=merchant, role="OWNER")


def _invite(merchant, email, role="VIEWER"):
    with tenant_context(merchant.id), tenant_atomic():
        return services.invite_team_member(actor=_owner(merchant), email=email, role=role)


def _bootstrap_csrf_client():
    """GET /auth/login sets csrftoken without authenticating (CSRF bootstrap)."""
    client = APIClient(enforce_csrf_checks=True)
    resp = client.get(LOGIN)
    assert resp.status_code == 204
    return client, client.cookies["csrftoken"].value


def _accept(client, token_header, data):
    kwargs = {"HTTP_X_CSRFTOKEN": token_header} if token_header else {}
    return client.post(ACCEPT, data, format="json", **kwargs)


def _assert_error_shape(body, code=None):
    assert set(body) == {"error"}
    assert "code" in body["error"] and "message" in body["error"]
    if code:
        assert body["error"]["code"] == code


# --- Happy paths ---------------------------------------------------------


def test_new_user_valid_token_and_strong_password_accepts_and_can_then_login(make_merchant):
    owner = make_merchant()
    member, token = _invite(owner.merchant, "newbie@x.com")
    client, csrf = _bootstrap_csrf_client()

    resp = _accept(client, csrf, {"token": token, "password": STRONG_PASSWORD})

    assert resp.status_code == 204
    with tenant_context(owner.merchant_id), tenant_atomic():
        member.refresh_from_db()
        assert member.accepted_at is not None

    login_client, login_csrf = _bootstrap_csrf_client()
    login_resp = login_client.post(
        LOGIN, {"email": "newbie@x.com", "password": STRONG_PASSWORD}, format="json", HTTP_X_CSRFTOKEN=login_csrf
    )
    assert login_resp.status_code == 200
    assert login_resp.json()["merchant"]["id"] == str(owner.merchant_id)


def test_weak_password_returns_422_and_leaves_state_unaccepted(make_merchant):
    owner = make_merchant()
    member, token = _invite(owner.merchant, "weak@x.com")
    client, csrf = _bootstrap_csrf_client()

    resp = _accept(client, csrf, {"token": token, "password": "123"})

    assert resp.status_code == 422
    _assert_error_shape(resp.json(), "validation_error")
    with tenant_context(owner.merchant_id), tenant_atomic():
        member.refresh_from_db()
        assert member.accepted_at is None
    member.user.refresh_from_db()
    assert not member.user.has_usable_password()


def test_existing_user_correct_current_password_accepts_and_leaves_hash_unchanged(make_merchant, add_member, password):
    a, b = make_merchant(), make_merchant()
    existing = add_member(b.merchant, "VIEWER", "known@x.com", accepted=True)
    original_hash = existing.user.password
    member, token = _invite(a.merchant, "known@x.com", role="ADMIN")
    client, csrf = _bootstrap_csrf_client()

    resp = _accept(client, csrf, {"token": token, "password": password})

    assert resp.status_code == 204
    with tenant_context(a.merchant_id), tenant_atomic():
        member.refresh_from_db()
        assert member.accepted_at is not None
    existing.user.refresh_from_db()
    assert existing.user.password == original_hash


# --- Account-takeover guard -----------------------------------------------


def test_existing_user_wrong_password_returns_400_invalid_invite_and_hash_unchanged(make_merchant, add_member):
    a, b = make_merchant(), make_merchant()
    existing = add_member(b.merchant, "VIEWER", "known2@x.com", accepted=True)
    original_hash = existing.user.password
    member, token = _invite(a.merchant, "known2@x.com")
    client, csrf = _bootstrap_csrf_client()

    resp = _accept(client, csrf, {"token": token, "password": "definitely-wrong"})

    assert resp.status_code == 400
    _assert_error_shape(resp.json(), "invalid_invite")
    with tenant_context(a.merchant_id), tenant_atomic():
        member.refresh_from_db()
        assert member.accepted_at is None
    existing.user.refresh_from_db()
    assert existing.user.password == original_hash


# --- Existing User with an unusable password ------------------------------


def test_shared_unusable_password_account_second_merchant_accepts_first_stays_pending(make_merchant):
    a, b = make_merchant(), make_merchant()
    b_member, _ = _invite(b.merchant, "shared@x.com")
    a_member, a_token = _invite(a.merchant, "shared@x.com", role="ADMIN")
    client, csrf = _bootstrap_csrf_client()

    resp = _accept(client, csrf, {"token": a_token, "password": STRONG_PASSWORD})

    assert resp.status_code == 204
    with tenant_context(a.merchant_id), tenant_atomic():
        a_member.refresh_from_db()
        assert a_member.accepted_at is not None
    with tenant_context(b.merchant_id), tenant_atomic():
        b_member.refresh_from_db()
        assert b_member.accepted_at is None


def test_shared_unusable_password_account_weak_password_returns_422(make_merchant):
    a, b = make_merchant(), make_merchant()
    _invite(b.merchant, "shared2@x.com")
    a_member, a_token = _invite(a.merchant, "shared2@x.com", role="ADMIN")
    client, csrf = _bootstrap_csrf_client()

    resp = _accept(client, csrf, {"token": a_token, "password": "123"})

    assert resp.status_code == 422
    with tenant_context(a.merchant_id), tenant_atomic():
        a_member.refresh_from_db()
        assert a_member.accepted_at is None
    a_member.user.refresh_from_db()
    assert not a_member.user.has_usable_password()


# --- Cross-merchant token (security) ---------------------------------------


def test_cross_merchant_token_does_not_touch_other_merchant_membership_or_write_its_audit_row(make_merchant):
    a, b = make_merchant(), make_merchant()
    b_member, _ = _invite(b.merchant, "cross@x.com")
    b_role, b_invited_at = b_member.role, b_member.invited_at
    a_member, a_token = _invite(a.merchant, "cross@x.com", role="ADMIN")
    client, csrf = _bootstrap_csrf_client()

    resp = _accept(client, csrf, {"token": a_token, "password": STRONG_PASSWORD})

    assert resp.status_code == 204
    with tenant_context(a.merchant_id), tenant_atomic():
        a_member.refresh_from_db()
        assert a_member.accepted_at is not None
    with tenant_context(b.merchant_id), tenant_atomic():
        b_member.refresh_from_db()
        assert b_member.accepted_at is None
        assert b_member.role == b_role
        assert b_member.invited_at == b_invited_at
        assert not AuditLog.objects.filter(merchant_id=b.merchant_id, action="team_member.accepted").exists()


def test_tampered_payload_pointing_at_other_merchants_member_id_returns_400(make_merchant):
    from django.core import signing

    a, b = make_merchant(), make_merchant()
    b_member, _ = _invite(b.merchant, "edit-target@x.com")
    _, a_token = _invite(a.merchant, "edit-payload@x.com")

    payload = services._invite_signer.unsign_object(a_token, max_age=services.INVITE_MAX_AGE)
    payload["tm"] = str(b_member.pk)
    forged_signer = signing.TimestampSigner(key="attacker-does-not-have-the-key", salt="accounts.invite")
    forged_token = forged_signer.sign_object(payload)

    client, csrf = _bootstrap_csrf_client()
    resp = _accept(client, csrf, {"token": forged_token, "password": STRONG_PASSWORD})

    assert resp.status_code == 400
    _assert_error_shape(resp.json(), "invalid_invite")


# --- Uniform 400 invalid_invite for every token failure ---------------------


def test_tampered_expired_replayed_revoked_superseded_and_suspended_all_return_same_400_body(make_merchant):
    from unittest import mock

    from django.utils import timezone as dj_timezone

    a = make_merchant()
    bodies = []

    # More than 5 accept attempts happen below (one per IP-throttled test
    # client); bypass the invite_accept throttle so it isn't the reason a
    # later attempt fails, exactly like the login-failures test above it.
    with mock.patch("rest_framework.throttling.SimpleRateThrottle.allow_request", return_value=True):

        def attempt(token, password=STRONG_PASSWORD):
            client, csrf = _bootstrap_csrf_client()
            resp = _accept(client, csrf, {"token": token, "password": password})
            assert resp.status_code == 400, resp.content
            bodies.append(resp.json())

        # tampered
        _, tampered_base = _invite(a.merchant, "tampered@x.com")
        attempt(tampered_base + "x")

        # expired
        _, expired_token = _invite(a.merchant, "expired@x.com")
        future = dj_timezone.now() + services.INVITE_MAX_AGE + services.INVITE_MAX_AGE
        with mock.patch("django.core.signing.time.time", return_value=future.timestamp()):
            attempt(expired_token)

        # replayed (already accepted)
        replay_member, replay_token = _invite(a.merchant, "replay@x.com")
        client, csrf = _bootstrap_csrf_client()
        first = _accept(client, csrf, {"token": replay_token, "password": STRONG_PASSWORD})
        assert first.status_code == 204
        attempt(replay_token)

        # revoked
        revoked_member, revoked_token = _invite(a.merchant, "revoked@x.com")
        with tenant_context(a.merchant_id), tenant_atomic():
            services.revoke_team_member(actor=_owner(a.merchant), member_id=revoked_member.pk)
        attempt(revoked_token)

        # superseded by a re-invite
        superseded_member, superseded_token = _invite(a.merchant, "superseded@x.com")
        _invite(a.merchant, "superseded@x.com", role="ADMIN")
        attempt(superseded_token)

        # merchant not ACTIVE
        suspended_member, suspended_token = _invite(a.merchant, "suspended-target@x.com")
        Merchant.objects.filter(id=a.merchant_id).update(status=Merchant.Status.SUSPENDED)
        attempt(suspended_token)
        Merchant.objects.filter(id=a.merchant_id).update(status=Merchant.Status.ACTIVE)

    assert all(b == bodies[0] for b in bodies)
    _assert_error_shape(bodies[0], "invalid_invite")


def test_accepting_same_token_twice_results_in_one_acceptance_and_one_audit_row(make_merchant):
    a = make_merchant()
    member, token = _invite(a.merchant, "dup@x.com")
    client1, csrf1 = _bootstrap_csrf_client()
    client2, csrf2 = _bootstrap_csrf_client()

    first = _accept(client1, csrf1, {"token": token, "password": STRONG_PASSWORD})
    second = _accept(client2, csrf2, {"token": token, "password": STRONG_PASSWORD})

    assert first.status_code == 204
    assert second.status_code == 400
    with tenant_context(a.merchant_id), tenant_atomic():
        assert (
            AuditLog.objects.filter(merchant_id=a.merchant_id, action="team_member.accepted").count() == 1
        )


# --- CSRF and throttling ----------------------------------------------------


def test_missing_csrf_token_returns_403(make_merchant):
    owner = make_merchant()
    _, token = _invite(owner.merchant, "nocsrf@x.com")
    client = APIClient(enforce_csrf_checks=True)
    client.get(LOGIN)  # sets the cookie but we never send X-CSRFToken

    resp = client.post(ACCEPT, {"token": token, "password": STRONG_PASSWORD}, format="json")

    assert resp.status_code == 403


def test_sixth_rapid_attempt_from_one_ip_returns_429(make_merchant):
    owner = make_merchant()
    client = APIClient(enforce_csrf_checks=True)
    client.get(LOGIN)
    csrf = client.cookies["csrftoken"].value

    statuses = []
    for _ in range(6):
        resp = client.post(
            ACCEPT, {"token": "not-a-real-token", "password": "irrelevant"}, format="json", HTTP_X_CSRFTOKEN=csrf
        )
        statuses.append(resp.status_code)

    assert statuses[:5] == [400] * 5
    assert statuses[5] == 429


def test_csrf_bootstrap_sequence_get_login_then_post_accept_succeeds(make_merchant):
    owner = make_merchant()
    _, token = _invite(owner.merchant, "bootstrap@x.com")
    client = APIClient(enforce_csrf_checks=True)

    login_get = client.get(LOGIN)
    assert login_get.status_code == 204
    csrf = client.cookies["csrftoken"].value

    resp = client.post(
        ACCEPT, {"token": token, "password": STRONG_PASSWORD}, format="json", HTTP_X_CSRFTOKEN=csrf
    )
    assert resp.status_code == 204


# --- Token transport: body only, never a query parameter -------------------


def test_token_as_query_parameter_only_with_no_body_token_returns_422(make_merchant):
    owner = make_merchant()
    _, token = _invite(owner.merchant, "queryparam@x.com")
    client, csrf = _bootstrap_csrf_client()

    resp = client.post(
        f"{ACCEPT}?token={token}", {"password": STRONG_PASSWORD}, format="json", HTTP_X_CSRFTOKEN=csrf
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["field_errors"]["token"]
    with tenant_context(owner.merchant_id), tenant_atomic():
        member = TeamMember.objects.get(user__email="queryparam@x.com")
    assert member.accepted_at is None


# --- No SET LOCAL app.current_user_id while a merchant context is active ---


@pytest.mark.django_db(transaction=True)
def test_accept_invite_does_not_leave_tenant_context_set(make_merchant):
    from django.db import connection

    owner = make_merchant()
    _, token = _invite(owner.merchant, "setlocal@x.com")
    client, csrf = _bootstrap_csrf_client()

    resp = _accept(client, csrf, {"token": token, "password": STRONG_PASSWORD})
    assert resp.status_code == 204

    with connection.cursor() as cur:
        cur.execute("SELECT current_setting('app.current_merchant_id', true)")
        merchant_after = cur.fetchone()[0]
    assert merchant_after in (None, "")


def test_user_logged_into_merchant_b_accepts_merchant_a_invite_on_same_session(
    make_merchant, session_client, password
):
    """Regression for the SessionMerchantMiddleware accept-invite bypass: an
    authenticated merchant-B session must not be tenant-wrapped into B when
    it posts to /auth/accept-invite (user_lookup_atomic refuses nesting)."""
    a = make_merchant("A")
    b = make_merchant("B")
    email = b.user.email
    client = session_client(email)  # logged into B; client.csrf is post-login

    member_a, token = _invite(a.merchant, email, role="VIEWER")

    resp = _accept(client, client.csrf, {"token": token, "password": password})
    assert resp.status_code == 204, resp.content

    with tenant_context(a.merchant_id), tenant_atomic():
        member_a.refresh_from_db()
        assert member_a.accepted_at is not None
    with tenant_context(b.merchant_id), tenant_atomic():
        b_member = TeamMember.objects.get(pk=b.pk)
        assert b_member.role == "OWNER"
        assert b_member.accepted_at == b.accepted_at
        assert b_member.updated_at == b.updated_at

    # The B session still works and is still scoped to B.
    resp = client.get("/api/v1/merchant")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(b.merchant_id)
