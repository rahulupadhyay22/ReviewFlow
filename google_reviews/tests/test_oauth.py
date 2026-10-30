"""OAuth connect flow: start / complete / attempts, the attempt lifecycle, the
asynchronous exchange, the connection upsert and token handling (spec 09 "OAuth &
connection"). Real PostgreSQL commits. The only mock is the Google HTTP
boundary (`http`); the real provider runs, including ID-token verification."""
import logging
import threading
import urllib.error
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.utils import timezone

from accounts.models import TeamMember
from core.crypto import decrypt
from core.tenancy import tenant_atomic, tenant_context
from google_reviews import services, tasks
from google_reviews.exceptions import InvalidOAuthState
from google_reviews.models import GoogleConnection, GoogleOAuthState
from google_reviews.tests.helpers import (
    CLIENT_ID,
    MISSING,
    TOKEN_URL,
    Resp,
    add_member,
    api_post,
    attempt_row,
    audit_rows,
    claim,
    connection_rows,
    db_text,
    enqueues,  # noqa: F401
    exchange,
    fixture_json,
    google_settings,  # noqa: F401
    http,  # noqa: F401
    make_state,
    pending_attempt,
    sha256,
    start,
    token_body,
    wire,
)

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("google_settings", "http", "enqueues"),
]

ACCESS = "ya29.TEST-ACCESS-TOKEN"
REFRESH = "1//TEST-REFRESH-TOKEN"


@pytest.fixture
def owner(make_merchant):
    return make_merchant("A")


@pytest.fixture
def client(owner, session_client):
    return session_client(owner.user.email)


def connect(owner, http, *, code="auth-code-1", **wire_kwargs):
    pk, nonce, _state = pending_attempt(owner, code)
    wire(http, nonce, **wire_kwargs)
    exchange(owner, pk)
    return pk


def state_rows(owner):
    with tenant_context(owner.merchant_id), tenant_atomic():
        return list(GoogleOAuthState.objects.order_by("created_at"))


def age(owner, pk, seconds):
    """Make the attempt's expires_at `seconds` in the past."""
    with tenant_context(owner.merchant_id), tenant_atomic():
        GoogleOAuthState.objects.filter(pk=pk).update(expires_at=timezone.now() - timedelta(seconds=seconds))


def expire(owner):
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.expire_attempts()


def google_actions(owner):
    return [r["action"] for r in audit_rows(owner) if r["action"].startswith("google.")]


# --- start --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setting,value",
    [
        ("GOOGLE_INTEGRATION_ENABLED", False),
        ("GOOGLE_OAUTH_CLIENT_ID", ""),
        ("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        ("GOOGLE_OAUTH_REDIRECT_URI", ""),
        ("FERNET_KEY", ""),
    ],
)
def test_start_is_503_google_not_configured_and_stores_nothing(owner, client, http, settings, setting, value):
    setattr(settings, setting, value)

    response = api_post(client, "google/oauth/start")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "google_not_configured"
    assert state_rows(owner) == []
    assert http.calls == []


def test_start_stores_only_hashes_and_returns_the_documented_url(owner, client, http):
    response = api_post(client, "google/oauth/start")

    assert response.status_code == 200
    url = response.json()["authorization_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert query["scope"] == "openid email https://www.googleapis.com/auth/business.manage"
    assert (query["access_type"], query["prompt"], query["response_type"]) == ("offline", "consent", "code")
    assert query["client_id"] == CLIENT_ID
    assert "include_granted_scopes" not in query and "hd" not in query
    state, nonce = query["state"], query["nonce"]
    assert state != nonce and len(state) >= 32 and len(nonce) >= 32
    (row,) = state_rows(owner)
    assert (row.status, row.state_hash, row.nonce_hash) == ("ISSUED", sha256(state), sha256(nonce))
    assert row.user_id == owner.user.pk and row.code_enc is None
    assert timedelta(seconds=500) < row.expires_at - timezone.now() <= timedelta(seconds=600)
    assert state not in db_text(owner.merchant_id) and nonce not in db_text(owner.merchant_id)
    assert http.calls == []


@pytest.mark.parametrize("role", [TeamMember.Role.MANAGER, TeamMember.Role.VIEWER])
def test_manager_and_viewer_get_403_on_start_complete_and_attempts(owner, session_client, role):
    member = add_member(owner.merchant, role, f"{role.lower()}@example.com")
    other = session_client(member.user.email)

    assert api_post(other, "google/oauth/start").status_code == 403
    assert api_post(other, "google/oauth/complete", {"code": "c", "state": "s"}).status_code == 403
    assert other.get(f"/api/v1/google/oauth/attempts/{uuid.uuid4()}").status_code == 403


# --- complete -------------------------------------------------------------------------


def test_complete_claims_the_attempt_enqueues_once_after_commit_and_calls_no_google(owner, client, http, enqueues):
    state, _ = start(owner)

    response = api_post(client, "google/oauth/complete", {"code": "auth-code-1", "state": state})

    assert response.status_code == 202
    (row,) = state_rows(owner)
    assert response.json() == {"attempt_id": str(row.pk), "status": "PENDING"}
    assert row.status == "PENDING" and row.code_enc not in (None, "auth-code-1")
    assert decrypt(row.code_enc) == "auth-code-1"
    assert http.calls == []
    assert enqueues.exchange == [((str(owner.merchant_id), str(row.pk)), False)]


def test_complete_enqueues_nothing_when_the_transaction_rolls_back(owner, enqueues):
    state, _ = start(owner)

    with pytest.raises(RuntimeError), tenant_context(owner.merchant_id), tenant_atomic():
        services.complete_google_oauth(actor=owner.user, code="auth-code-1", state=state)
        raise RuntimeError("roll back")

    assert enqueues.exchange == []
    assert state_rows(owner)[0].status == "ISSUED"


def test_complete_with_an_invalid_code_is_422(owner, client):
    state, _ = start(owner)
    assert api_post(client, "google/oauth/complete", {"state": state}).status_code == 422
    assert api_post(client, "google/oauth/complete", {"code": "x" * 2049, "state": state}).status_code == 422
    assert state_rows(owner)[0].status == "ISSUED"


def test_complete_is_503_when_not_configured(owner, client, settings):
    state, _ = start(owner)
    settings.GOOGLE_INTEGRATION_ENABLED = False
    response = api_post(client, "google/oauth/complete", {"code": "c", "state": state})
    assert response.status_code == 503 and response.json()["error"]["code"] == "google_not_configured"


def test_every_bad_state_gets_the_identical_400(owner, client, make_merchant, session_client):
    body = {"code": "auth-code-1"}
    responses = {}

    responses["unknown"] = api_post(client, "google/oauth/complete", {**body, "state": "no-such-state"})

    state, _ = start(owner)
    age(owner, state_rows(owner)[-1].pk, 5)
    responses["expired"] = api_post(client, "google/oauth/complete", {**body, "state": state})

    state, _ = start(owner)
    assert api_post(client, "google/oauth/complete", {**body, "state": state}).status_code == 202
    responses["reused"] = api_post(client, "google/oauth/complete", {**body, "state": state})

    admin = add_member(owner.merchant, TeamMember.Role.ADMIN, "admin@example.com")
    admin_state, _ = start(admin)
    responses["other_user"] = api_post(client, "google/oauth/complete", {**body, "state": admin_state})

    other = make_merchant("B")
    other_state, _ = start(other)
    responses["other_merchant"] = api_post(client, "google/oauth/complete", {**body, "state": other_state})

    assert {r.status_code for r in responses.values()} == {400}
    assert {r.content for r in responses.values()} == {responses["unknown"].content}
    assert responses["unknown"].json()["error"]["code"] == "invalid_oauth_state"
    # the other user's and other merchant's attempts were not consumed
    admins_attempt = next(r for r in state_rows(admin) if r.user_id == admin.user.pk)
    assert admins_attempt.status == "ISSUED"
    assert state_rows(other)[0].status == "ISSUED"


def test_two_concurrent_completes_with_one_state_claim_it_exactly_once(owner, spawn, enqueues):
    state, _ = start(owner)
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait(timeout=10)
        try:
            services.complete_google_oauth(actor=owner.user, code="auth-code-1", state=state)
        except InvalidOAuthState:
            return "lost"
        return "won"

    results = sorted(t.join() for t in [spawn(attempt, owner.merchant_id), spawn(attempt, owner.merchant_id)])

    assert results == ["lost", "won"]
    assert state_rows(owner)[0].status == "PENDING"
    assert len(enqueues.exchange) == 1


# --- the exchange: connection upsert -----------------------------------------------------


def test_a_successful_exchange_stores_ciphertext_activates_and_audits(owner, http, enqueues):
    pk = connect(owner, http)

    row = attempt_row(owner, pk)
    (conn,) = connection_rows(owner)
    assert (row["status"], row["failure_code"], row["code_enc"]) == ("COMPLETED", None, None)
    assert row["completed_at"] is not None and row["google_connection_id"] == conn["id"]
    assert conn["status"] == "ACTIVE" and conn["connected_at"] is not None
    assert conn["google_account_sub"] == "sub-123" and conn["google_account_email"] == "owner@example.com"
    assert ACCESS not in (conn["access_token_enc"], conn["refresh_token_enc"])
    assert (decrypt(conn["access_token_enc"]), decrypt(conn["refresh_token_enc"])) == (ACCESS, REFRESH)
    assert timedelta(seconds=3000) < conn["expires_at"] - timezone.now() < timedelta(seconds=3700)
    (audit,) = [r for r in audit_rows(owner) if r["action"].startswith("google.")]
    assert audit["action"] == "google.connection_added"
    assert audit["target_id"] == str(conn["id"]) and audit["actor_user_id"] == owner.user.pk
    assert not audit["metadata_json"]


def test_reauthorizing_the_same_account_updates_the_same_row(owner, http):
    connect(owner, http)
    (first,) = connection_rows(owner)

    connect(owner, http, claims={"email": "renamed@example.com"}, token={"access_token": "ya29.SECOND", "refresh_token": "1//SECOND"})

    (second,) = connection_rows(owner)
    assert second["id"] == first["id"] and second["google_account_email"] == "renamed@example.com"
    assert (decrypt(second["access_token_enc"]), decrypt(second["refresh_token_enc"])) == ("ya29.SECOND", "1//SECOND")
    assert second["status"] == "ACTIVE"
    assert google_actions(owner) == ["google.connection_added", "google.connection_reauthorized"]


def test_a_different_account_creates_a_second_connection(owner, http):
    connect(owner, http)
    connect(owner, http, claims={"sub": "sub-456", "email": "second@example.com"})

    assert sorted(c["google_account_sub"] for c in connection_rows(owner)) == ["sub-123", "sub-456"]
    assert google_actions(owner) == ["google.connection_added"] * 2


def test_no_refresh_token_keeps_the_stored_one_for_an_active_connection(owner, http):
    connect(owner, http)

    pk = connect(owner, http, token={"refresh_token": MISSING, "access_token": "ya29.NEW"})

    (conn,) = connection_rows(owner)
    assert attempt_row(owner, pk)["status"] == "COMPLETED"
    assert (decrypt(conn["access_token_enc"]), decrypt(conn["refresh_token_enc"])) == ("ya29.NEW", REFRESH)
    assert google_actions(owner) == ["google.connection_added", "google.connection_reauthorized"]


def test_no_refresh_token_fails_for_a_needs_reauth_connection_and_leaves_it_unchanged(owner, http):
    connect(owner, http)
    with tenant_context(owner.merchant_id), tenant_atomic():
        GoogleConnection.objects.update(status="NEEDS_REAUTH")
    (before,) = connection_rows(owner)

    pk = connect(owner, http, token={"refresh_token": MISSING, "access_token": "ya29.NEW"})

    assert attempt_row(owner, pk)["failure_code"] == "REFRESH_TOKEN_MISSING"
    assert attempt_row(owner, pk)["status"] == "FAILED"
    (after,) = connection_rows(owner)
    assert after == before
    assert google_actions(owner) == ["google.connection_added"]


def test_no_refresh_token_for_a_new_account_creates_no_row(owner, http):
    pk = connect(owner, http, token={"refresh_token": MISSING})

    assert (attempt_row(owner, pk)["status"], attempt_row(owner, pk)["failure_code"]) == ("FAILED", "REFRESH_TOKEN_MISSING")
    assert connection_rows(owner) == [] and google_actions(owner) == []


def test_scope_missing_fails_without_a_connection(owner, http):
    pk = connect(owner, http, token={"scope": "openid https://www.googleapis.com/auth/userinfo.email"})

    assert attempt_row(owner, pk)["failure_code"] == "SCOPE_MISSING"
    assert connection_rows(owner) == [] and google_actions(owner) == []


@pytest.mark.parametrize(
    "claims",
    [{"sub": MISSING}, {"sub": ""}, {"sub": "x" * 256}, {"email": MISSING}, {"email": ""}],
    ids=["no-sub", "empty-sub", "sub-256", "no-email", "empty-email"],
)
def test_missing_identity_fails_without_a_connection(owner, http, claims):
    pk = connect(owner, http, claims=claims)

    assert (attempt_row(owner, pk)["status"], attempt_row(owner, pk)["failure_code"]) == ("FAILED", "IDENTITY_MISSING")
    assert connection_rows(owner) == [] and google_actions(owner) == []


def test_there_is_no_email_fallback_when_sub_is_missing(owner, http):
    connect(owner, http)
    (before,) = connection_rows(owner)

    pk = connect(owner, http, claims={"sub": MISSING}, token={"access_token": "ya29.OTHER"})

    assert attempt_row(owner, pk)["failure_code"] == "IDENTITY_MISSING"
    assert connection_rows(owner) == [before]


@pytest.mark.parametrize(
    "answer",
    [
        Resp(500, {"error": "backend_error"}),
        Resp(400, fixture_json("refresh_invalid_grant.json")),
        Resp(429, fixture_json("rate_limit.json")),
        urllib.error.URLError("timed out"),
        TimeoutError(),
    ],
    ids=["5xx", "invalid_grant", "429", "network", "timeout"],
)
def test_a_failed_exchange_is_terminal_not_retried_and_writes_no_connection(owner, http, answer):
    pk, _nonce, _ = pending_attempt(owner)
    http.route(TOKEN_URL, answer)

    exchange(owner, pk)
    exchange(owner, pk)  # a duplicate delivery

    row = attempt_row(owner, pk)
    assert (row["status"], row["failure_code"], row["code_enc"]) == ("FAILED", "EXCHANGE_FAILED", None)
    assert len(http.to(TOKEN_URL)) == 1
    assert connection_rows(owner) == [] and google_actions(owner) == []


def test_a_user_removed_before_the_exchange_cannot_create_a_connection(owner, http):
    admin = add_member(owner.merchant, TeamMember.Role.ADMIN, "admin@example.com")
    pk, nonce, _ = pending_attempt(admin)
    wire(http, nonce)
    with tenant_context(owner.merchant_id), tenant_atomic():
        TeamMember.objects.filter(pk=admin.pk).update(accepted_at=None)

    exchange(admin, pk)

    assert (attempt_row(owner, pk)["status"], attempt_row(owner, pk)["failure_code"]) == ("FAILED", "ACTOR_NOT_MEMBER")
    assert connection_rows(owner) == [] and google_actions(owner) == []


def test_finish_race_attempt_no_longer_pending_rolls_back_connection_and_audit(owner, http):
    pk, nonce, _ = pending_attempt(owner)
    id_token = wire(http, nonce)

    def interrupt_then_answer(req):
        # while the exchange is in flight the sweep fails the attempt
        with tenant_atomic():
            GoogleOAuthState.objects.filter(pk=pk).update(
                status="FAILED", failure_code="EXCHANGE_INTERRUPTED", completed_at=timezone.now()
            )
        return token_body(id_token)

    http.route(TOKEN_URL, interrupt_then_answer)

    exchange(owner, pk)

    row = attempt_row(owner, pk)
    assert (row["status"], row["failure_code"], row["google_connection_id"]) == ("FAILED", "EXCHANGE_INTERRUPTED", None)
    assert connection_rows(owner) == [] and google_actions(owner) == []


# --- code at rest, duplicates, crashes ------------------------------------------------------


def test_the_code_is_gone_from_the_database_before_google_is_called_and_a_duplicate_task_calls_nothing(owner, http):
    pk, nonce, _ = pending_attempt(owner)
    id_token = wire(http, nonce)
    seen = []

    def check_then_answer(req):
        seen.append(attempt_row(owner, pk))
        return token_body(id_token)

    http.route(TOKEN_URL, check_then_answer)

    tasks.exchange_oauth_code.apply(args=(str(owner.merchant_id), str(pk)))
    tasks.exchange_oauth_code.apply(args=(str(owner.merchant_id), str(pk)))  # redelivery

    assert [(s["status"], s["code_enc"]) for s in seen] == [("PENDING", None)]
    assert len(http.to(TOKEN_URL)) == 1
    assert len(connection_rows(owner)) == 1


def test_exchange_task_has_no_automatic_retry_and_runs_on_the_google_sync_queue():
    for task in (tasks.exchange_oauth_code,):
        assert task.max_retries == 0
        assert not getattr(task, "autoretry_for", ())
        assert task.queue == "google_sync"


def test_a_task_that_dies_after_take_code_leaves_no_code_and_expires_to_exchange_interrupted(owner, settings):
    pk, _nonce, _ = pending_attempt(owner)
    with tenant_context(owner.merchant_id):
        assert services._take_code(pk) is not None  # ...and the worker dies here
    assert attempt_row(owner, pk)["code_enc"] is None

    age(owner, pk, 5)
    expire(owner)
    assert attempt_row(owner, pk)["status"] == "PENDING"  # inside the grace period

    age(owner, pk, settings.GOOGLE_OAUTH_EXCHANGE_GRACE_SECONDS + 5)
    expire(owner)
    row = attempt_row(owner, pk)
    assert (row["status"], row["failure_code"], row["code_enc"]) == ("FAILED", "EXCHANGE_INTERRUPTED", None)


def test_a_broker_publish_failure_leaves_the_attempt_to_expire_and_clears_the_code(owner, monkeypatch):
    def boom(*args):
        raise ConnectionError("broker down")

    monkeypatch.setattr(tasks.exchange_oauth_code, "delay", boom)
    state, _ = start(owner)

    attempt = claim(owner, state)  # must not raise

    assert attempt_row(owner, attempt.pk)["code_enc"] is not None
    age(owner, attempt.pk, 5)
    expire(owner)
    row = attempt_row(owner, attempt.pk)
    assert (row["status"], row["failure_code"], row["code_enc"]) == ("FAILED", "OAUTH_EXPIRED", None)


# --- expiry ----------------------------------------------------------------------------------


def test_an_expired_issued_attempt_cannot_be_claimed_and_expires_with_oauth_expired(owner):
    state, _ = start(owner)
    pk = state_rows(owner)[0].pk
    age(owner, pk, 5)

    with pytest.raises(InvalidOAuthState):
        claim(owner, state)
    expire(owner)

    row = attempt_row(owner, pk)
    assert (row["status"], row["failure_code"]) == ("FAILED", "OAUTH_EXPIRED")


def test_an_expired_code_holding_attempt_is_not_exchanged_and_expires_with_the_code_cleared(owner, http):
    pk, nonce, _ = pending_attempt(owner)
    wire(http, nonce)
    age(owner, pk, 5)

    exchange(owner, pk)
    assert http.calls == [] and attempt_row(owner, pk)["status"] == "PENDING"
    expire(owner)

    row = attempt_row(owner, pk)
    assert (row["status"], row["failure_code"], row["code_enc"]) == ("FAILED", "OAUTH_EXPIRED", None)


def test_an_exchange_in_flight_is_not_expired_before_the_grace_period(owner, settings):
    grace = settings.GOOGLE_OAUTH_EXCHANGE_GRACE_SECONDS
    inside = make_state(owner, status="PENDING", expires_in=-(grace - 10))
    outside = make_state(owner, status="PENDING", expires_in=-(grace + 10))

    expire(owner)

    assert attempt_row(owner, inside.pk)["status"] == "PENDING"
    row = attempt_row(owner, outside.pk)
    assert (row["status"], row["failure_code"]) == ("FAILED", "EXCHANGE_INTERRUPTED")


def test_terminal_attempts_never_change(owner, make_connection):
    conn = make_connection(owner.merchant)
    done = make_state(owner, status="COMPLETED", expires_in=-5000, connection=conn)
    failed = make_state(owner, status="FAILED", expires_in=-5000, failure_code="SCOPE_MISSING")
    before = (attempt_row(owner, done.pk), attempt_row(owner, failed.pk))

    expire(owner)

    assert (attempt_row(owner, done.pk), attempt_row(owner, failed.pk)) == before


# --- GET /google/oauth/attempts/{id} -------------------------------------------------------------


def test_the_starting_user_reads_the_attempt_outcome(owner, client, http):
    pk = connect(owner, http)
    (conn,) = connection_rows(owner)

    response = client.get(f"/api/v1/google/oauth/attempts/{pk}")

    assert response.status_code == 200
    assert response.json() == {"id": str(pk), "status": "COMPLETED", "failure_code": None, "connection_id": str(conn["id"])}


def test_another_owner_or_admin_of_the_same_merchant_gets_404(owner, http, session_client):
    pk = connect(owner, http)
    admin = add_member(owner.merchant, TeamMember.Role.ADMIN, "admin@example.com")

    assert session_client(admin.user.email).get(f"/api/v1/google/oauth/attempts/{pk}").status_code == 404


def test_polling_a_stale_attempt_reports_it_failed_with_the_right_code(owner, client, settings):
    grace = settings.GOOGLE_OAUTH_EXCHANGE_GRACE_SECONDS
    issued = make_state(owner, status="ISSUED", expires_in=-5)
    pending = make_state(owner, status="PENDING", code=True, expires_in=-5)
    in_flight = make_state(owner, status="PENDING", expires_in=-(grace + 5))

    got = {
        name: client.get(f"/api/v1/google/oauth/attempts/{row.pk}").json()
        for name, row in (("issued", issued), ("pending", pending), ("in_flight", in_flight))
    }

    assert {k: (v["status"], v["failure_code"]) for k, v in got.items()} == {
        "issued": ("FAILED", "OAUTH_EXPIRED"),
        "pending": ("FAILED", "OAUTH_EXPIRED"),
        "in_flight": ("FAILED", "EXCHANGE_INTERRUPTED"),
    }


# --- purge --------------------------------------------------------------------------------------


def test_starting_an_attempt_purges_old_terminal_rows_only(owner, make_connection, settings):
    retention = settings.GOOGLE_OAUTH_ATTEMPT_RETENTION_SECONDS
    conn = make_connection(owner.merchant)
    old_failed = make_state(owner, status="FAILED", failure_code="SCOPE_MISSING", expires_in=-(retention + 60))
    old_done = make_state(owner, status="COMPLETED", connection=conn, expires_in=-(retention + 60))
    recent_failed = make_state(owner, status="FAILED", failure_code="SCOPE_MISSING", expires_in=-(retention - 3600))
    live = make_state(owner, status="ISSUED", expires_in=300)

    start(owner)

    remaining = {r.pk for r in state_rows(owner)}
    assert old_failed.pk not in remaining and old_done.pk not in remaining
    assert recent_failed.pk in remaining and live.pk in remaining
    assert len(connection_rows(owner)) == 1  # the referenced connection is not deleted


def test_the_daily_refresh_sweeps_expired_and_purges_old_attempts(owner, settings):
    retention = settings.GOOGLE_OAUTH_ATTEMPT_RETENTION_SECONDS
    stale = make_state(owner, status="ISSUED", expires_in=-5)
    ancient = make_state(owner, status="FAILED", failure_code="SCOPE_MISSING", expires_in=-(retention + 60))

    tasks.refresh_merchant_google_tokens.delay(str(owner.merchant_id))

    assert attempt_row(owner, stale.pk)["failure_code"] == "OAUTH_EXPIRED"
    assert ancient.pk not in {r.pk for r in state_rows(owner)}


# --- secrecy ------------------------------------------------------------------------------------


def test_no_secret_reaches_logs_audit_task_arguments_responses_or_columns(owner, client, http, enqueues, caplog):
    caplog.set_level(logging.DEBUG)
    start_response = api_post(client, "google/oauth/start")
    query = parse_qs(urlparse(start_response.json()["authorization_url"]).query)
    state, nonce = query["state"][0], query["nonce"][0]
    complete = api_post(client, "google/oauth/complete", {"code": "auth-code-1", "state": state})
    attempt_id = complete.json()["attempt_id"]
    id_token = wire(http, nonce)

    exchange(owner, attempt_id)
    poll = client.get(f"/api/v1/google/oauth/attempts/{attempt_id}")

    secrets = [ACCESS, REFRESH, "auth-code-1", id_token, "sub-123", "owner@example.com", state, nonce]
    for secret in secrets:
        assert secret not in caplog.text, "leaked to logs"
    for secret in (ACCESS, REFRESH, "auth-code-1", id_token, state, nonce):
        assert secret not in db_text(owner.merchant_id), "stored in plaintext"
        assert secret not in str(enqueues.exchange), "in a task argument"
    for response in (complete, poll):
        for secret in secrets:
            assert secret not in response.content.decode()
    assert start_response.content.decode().count(ACCESS) == 0
    assert all(not row["metadata_json"] for row in audit_rows(owner) if row["action"].startswith("google."))
