"""Token refresh and NEEDS_REAUTH (spec 09 "Token handling", "Tokens", G-10, OD-9).
Real provider against the fake Google HTTP boundary (`http`); the fixtures are
anonymized Google payloads."""
import logging
import urllib.error
from datetime import timedelta

import pytest
from django.utils import timezone

from auditlog.models import AuditLog
from core.crypto import decrypt, encrypt
from core.tenancy import tenant_atomic, tenant_context
from google_reviews import services, tasks
from google_reviews.models import GoogleConnection
from google_reviews.providers.base import GooglePermanentError, GoogleRateLimited, GoogleTransientError
from google_reviews.tests.helpers import (
    TOKEN_URL,
    Resp,
    fixture_json,
    google_settings,  # noqa: F401
    http,  # noqa: F401
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("google_settings", "http")]

STORED_REFRESH = "REFRESH-TOKEN-SENTINEL"
REFRESHED = "ya29.TEST-REFRESHED-ACCESS-TOKEN"


@pytest.fixture
def world(make_merchant, make_connection, session_client):
    owner = make_merchant("A")
    conn = make_connection(owner.merchant)
    return {"owner": owner, "connection": conn, "client": session_client(owner.user.email)}


def set_expiry(world, **delta):
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        GoogleConnection.objects.filter(pk=world["connection"].pk).update(expires_at=timezone.now() + timedelta(**delta))


def row(world):
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        return GoogleConnection.objects.filter(pk=world["connection"].pk).values().get()


def audit_count(world):
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        return AuditLog.objects.count()


def refresh_all(world):
    with tenant_context(world["owner"].merchant_id):
        services.refresh_merchant_tokens()


def refresh_one(world):
    with tenant_context(world["owner"].merchant_id):
        services.refresh_connection(world["connection"].pk)


# --- refresh --------------------------------------------------------------------------------


def test_the_daily_refresh_persists_new_ciphertext_and_expiry_and_keeps_the_stored_refresh_token(world, http):
    set_expiry(world, seconds=60)
    http.route(TOKEN_URL, fixture_json("refresh_response.json"))

    refresh_all(world)

    after = row(world)
    assert after["access_token_enc"] != REFRESHED and decrypt(after["access_token_enc"]) == REFRESHED
    assert decrypt(after["refresh_token_enc"]) == STORED_REFRESH
    assert timedelta(seconds=3000) < after["expires_at"] - timezone.now() < timedelta(seconds=3700)
    assert after["status"] == "ACTIVE"
    form = http.form(type("R", (), {"data": http.to(TOKEN_URL)[0].data}))
    assert (form["grant_type"], form["refresh_token"]) == ("refresh_token", STORED_REFRESH)


def test_a_refresh_response_carrying_a_refresh_token_replaces_the_stored_one(world, http):
    set_expiry(world, seconds=60)
    http.route(TOKEN_URL, {**fixture_json("refresh_response.json"), "refresh_token": "1//ROTATED"})

    refresh_one(world)

    assert decrypt(row(world)["refresh_token_enc"]) == "1//ROTATED"


def test_a_token_outside_the_refresh_horizon_is_not_refreshed(world, http):
    set_expiry(world, hours=1)

    refresh_all(world)

    assert http.calls == []


def test_the_refresh_task_refreshes_and_running_it_twice_is_safe(world, http):
    set_expiry(world, seconds=60)
    http.route(TOKEN_URL, fixture_json("refresh_response.json"))

    tasks.refresh_connection_token.delay(str(world["owner"].merchant_id), str(world["connection"].pk))
    tasks.refresh_connection_token.delay(str(world["owner"].merchant_id), str(world["connection"].pk))

    assert decrypt(row(world)["access_token_enc"]) == REFRESHED
    assert len(http.to(TOKEN_URL)) == 1  # the second run found a fresh token


def test_a_needs_reauth_connection_is_not_refreshed(world, http):
    set_expiry(world, seconds=60)
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        GoogleConnection.objects.update(status="NEEDS_REAUTH")

    refresh_all(world)

    assert http.calls == []


# --- revocation vs transient errors -------------------------------------------------------------


@pytest.mark.parametrize(
    "body,reason",
    [("refresh_invalid_grant.json", "invalid_grant"), ("refresh_admin_policy_enforced.json", "admin_policy_enforced")],
)
def test_only_a_revocation_error_sets_needs_reauth_with_one_warning_and_no_audit_row(world, http, caplog, body, reason):
    set_expiry(world, seconds=60)
    http.route(TOKEN_URL, Resp(400, fixture_json(body)))
    caplog.set_level(logging.DEBUG)
    audits_before = audit_count(world)

    refresh_all(world)

    after = row(world)
    assert after["status"] == "NEEDS_REAUTH"
    assert decrypt(after["refresh_token_enc"]) == STORED_REFRESH  # nothing is overwritten or cleared
    warnings = [r for r in caplog.records if "google.connection_needs_reauth" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert str(world["connection"].pk) in warnings[0].getMessage() and reason in warnings[0].getMessage()
    for secret in (STORED_REFRESH, "ACCESS-TOKEN-SENTINEL", after["google_account_sub"], after["google_account_email"]):
        assert secret not in caplog.text
    assert audit_count(world) == audits_before  # OD-9: no AuditLog row


@pytest.mark.parametrize(
    "answer",
    [
        Resp(400, {"error": "invalid_request"}),
        Resp(400, b"<html>bad gateway</html>"),
        Resp(401, {"error": "invalid_client"}),
        Resp(500, {"error": "backend_error"}),
        Resp(429, fixture_json("rate_limit.json")),
        urllib.error.URLError("timed out"),
        TimeoutError(),
    ],
    ids=["400-other", "400-not-json", "401", "5xx", "429", "network", "timeout"],
)
def test_a_transient_or_unrelated_error_never_changes_the_status(world, http, answer):
    set_expiry(world, seconds=60)
    http.route(TOKEN_URL, answer)
    before = row(world)

    with pytest.raises((GoogleTransientError, GooglePermanentError, GoogleRateLimited)):
        refresh_one(world)
    refresh_all(world)  # the daily loop swallows and logs, it must not flip the status either

    assert row(world) == before


def test_mark_needs_reauth_is_conditional_on_the_refresh_token_that_was_read(world):
    seen = row(world)["refresh_token_enc"]
    with tenant_context(world["owner"].merchant_id):
        assert services.mark_needs_reauth(world["connection"].pk, refresh_token_enc_seen="stale", reason="invalid_grant") is False
    assert row(world)["status"] == "ACTIVE"

    with tenant_context(world["owner"].merchant_id):
        assert services.mark_needs_reauth(world["connection"].pk, refresh_token_enc_seen=seen, reason="invalid_grant") is True
    assert row(world)["status"] == "NEEDS_REAUTH"


# --- a reauthorization that commits during the call -------------------------------------------------


def reauthorize_during_call(world, answer):
    """The token endpoint answers `answer`, but first a reauthorization commits."""

    def during(req):
        with tenant_atomic():
            GoogleConnection.objects.filter(pk=world["connection"].pk).update(
                access_token_enc=encrypt("ya29.FROM-REAUTH"),
                refresh_token_enc=encrypt("1//FROM-REAUTH"),
                status="ACTIVE",
                expires_at=timezone.now() + timedelta(seconds=60),
            )
        return answer

    return during


def test_a_refresh_does_not_overwrite_a_reauthorization_that_committed_meanwhile(world, http):
    set_expiry(world, seconds=60)
    http.route(TOKEN_URL, reauthorize_during_call(world, fixture_json("refresh_response.json")))

    refresh_one(world)

    after = row(world)
    assert decrypt(after["access_token_enc"]) == "ya29.FROM-REAUTH"
    assert decrypt(after["refresh_token_enc"]) == "1//FROM-REAUTH"


def test_a_revocation_for_a_replaced_refresh_token_does_not_set_needs_reauth(world, http):
    set_expiry(world, seconds=60)
    http.route(TOKEN_URL, reauthorize_during_call(world, Resp(400, fixture_json("refresh_invalid_grant.json"))))

    refresh_one(world)

    after = row(world)
    assert after["status"] == "ACTIVE" and decrypt(after["refresh_token_enc"]) == "1//FROM-REAUTH"


# --- GET /google/connections ---------------------------------------------------------------------------


def test_the_connections_list_exposes_status_for_the_banner_and_no_secret(world):
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        GoogleConnection.objects.update(status="NEEDS_REAUTH")

    response = world["client"].get("/api/v1/google/connections")

    assert response.status_code == 200
    (item,) = response.json()["results"]
    assert set(item) == {"id", "google_account_email", "status", "connected_at"}
    assert item["status"] == "NEEDS_REAUTH"
    body = response.content.decode()
    for secret in (STORED_REFRESH, "ACCESS-TOKEN-SENTINEL", "sub-1\""):
        assert secret not in body
