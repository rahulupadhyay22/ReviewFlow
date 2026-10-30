"""P-3: two connect attempts for the same Google account racing to create the
first connection. Real PostgreSQL, real threads, real commits and unique-index
waits; the only mock is the Google HTTP boundary (`http`). The deterministic test
reaches the same constraint path without timing."""
import threading
import time

import pytest
from django.db import IntegrityError, connection

from core.crypto import decrypt
from google_reviews import services
from google_reviews.models import GoogleConnection, GoogleOAuthState
from core.tenancy import tenant_atomic
from google_reviews.tests.helpers import (
    JWKS_URL,
    TOKEN_URL,
    attempt_row,
    audit_rows,
    connection_rows,
    db_text,
    enqueues,  # noqa: F401
    exchange,
    google_settings,  # noqa: F401
    http,  # noqa: F401
    jwks,
    make_claims,
    pending_attempt,
    sign,
    token_body,
)
from django.utils import timezone

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("google_settings", "http", "enqueues"),
]


@pytest.fixture
def owner(make_merchant):
    return make_merchant("A")


@pytest.fixture
def two_attempts(owner, http):
    """Two PENDING attempts whose exchanges return the same `sub` with different
    tokens (tags A and B). Returns (attempt_a_pk, attempt_b_pk)."""
    a_pk, a_nonce, _ = pending_attempt(owner, "code-A")
    b_pk, b_nonce, _ = pending_attempt(owner, "code-B")
    nonces = {"code-A": a_nonce, "code-B": b_nonce}

    def token(req):
        code = http.form(req)["code"]
        tag = code[-1]
        return token_body(
            sign(make_claims(nonces[code])), access_token=f"ya29.{tag}-ACCESS", refresh_token=f"1//{tag}-REFRESH"
        )

    http.route(JWKS_URL, jwks())
    http.route(TOKEN_URL, token)
    return a_pk, b_pk


def google_actions(owner):
    return [r["action"] for r in audit_rows(owner) if r["action"].startswith("google.")]


def blocked_backends():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
        )
        return cursor.fetchone()[0]


def test_two_concurrent_first_connects_leave_one_connection_and_fail_the_loser(owner, two_attempts, spawn, monkeypatch):
    a_pk, b_pk = two_attempts
    inserted, release = threading.Event(), threading.Event()
    seen = []
    lock = threading.Lock()
    real_record = services.record

    def paused_record(*args, **kwargs):
        # runs after the connection INSERT, in the same transaction, before commit
        with lock:
            first = not seen
            seen.append(1)
        if first:
            inserted.set()
            assert release.wait(30)
        return real_record(*args, **kwargs)

    monkeypatch.setattr(services, "record", paused_record)

    thread_a = spawn(lambda: services.exchange_attempt(a_pk), owner.merchant_id)
    assert inserted.wait(30), "thread A never reached its connection INSERT"
    thread_b = spawn(lambda: services.exchange_attempt(b_pk), owner.merchant_id)
    deadline = time.monotonic() + 15
    while not blocked_backends() and thread_b.thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    b_was_blocked = blocked_backends() > 0
    release.set()
    thread_a.join()
    thread_b.join()

    assert b_was_blocked, "B's INSERT did not wait on A's uncommitted row"
    (conn,) = connection_rows(owner)
    a_row, b_row = attempt_row(owner, a_pk), attempt_row(owner, b_pk)
    assert (a_row["status"], a_row["google_connection_id"]) == ("COMPLETED", conn["id"])
    assert (b_row["status"], b_row["failure_code"], b_row["code_enc"]) == ("FAILED", "GOOGLE_ACCOUNT_ALREADY_CONNECTED", None)
    assert (decrypt(conn["access_token_enc"]), decrypt(conn["refresh_token_enc"])) == ("ya29.A-ACCESS", "1//A-REFRESH")
    stored = db_text(owner.merchant_id)
    assert "ya29.B-ACCESS" not in stored and "1//B-REFRESH" not in stored
    assert google_actions(owner) == ["google.connection_added"]


class _EmptyRead:
    """What `select_for_update().filter(...)` returns while the existence read is
    forced to find no row. `after` runs once the read has happened."""

    def __init__(self, after):
        self.after = after

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        if self.after:
            self.after()
        return None


class _HideExisting:
    def __init__(self, real, after=None):
        self.real, self.after = real, after

    def select_for_update(self, *args, **kwargs):
        return _EmptyRead(self.after)

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_the_unique_violation_path_without_timing_leaves_the_winner_untouched(owner, two_attempts, monkeypatch):
    a_pk, b_pk = two_attempts
    exchange(owner, a_pk)  # the winner commits
    (winner_before,) = connection_rows(owner)
    monkeypatch.setattr(GoogleConnection, "objects", _HideExisting(GoogleConnection.objects))

    exchange(owner, b_pk)  # its existence read finds nothing, so it INSERTs

    (winner_after,) = connection_rows(owner)
    assert winner_after == winner_before  # status, expires_at, ciphertexts, updated_at: byte-identical
    b_row = attempt_row(owner, b_pk)
    assert (b_row["status"], b_row["failure_code"], b_row["code_enc"]) == ("FAILED", "GOOGLE_ACCOUNT_ALREADY_CONNECTED", None)
    assert attempt_row(owner, a_pk)["status"] == "COMPLETED"
    assert "ya29.B-ACCESS" not in db_text(owner.merchant_id) and "1//B-REFRESH" not in db_text(owner.merchant_id)
    assert google_actions(owner) == ["google.connection_added"]


def test_a_terminal_attempt_is_not_overwritten_by_the_unique_violation_handler(owner, two_attempts, spawn, monkeypatch):
    a_pk, b_pk = two_attempts
    exchange(owner, a_pk)

    def sweep_expires_b():
        def run():
            with tenant_atomic():
                GoogleOAuthState.objects.filter(pk=b_pk).update(
                    status="FAILED", failure_code="EXCHANGE_INTERRUPTED", completed_at=timezone.now()
                )

        spawn(run, owner.merchant_id).join()

    monkeypatch.setattr(GoogleConnection, "objects", _HideExisting(GoogleConnection.objects, after=sweep_expires_b))

    exchange(owner, b_pk)

    b_row = attempt_row(owner, b_pk)
    assert (b_row["status"], b_row["failure_code"]) == ("FAILED", "EXCHANGE_INTERRUPTED")
    assert len(connection_rows(owner)) == 1


def test_an_integrity_error_from_another_constraint_is_not_mapped_to_already_connected(owner, two_attempts, monkeypatch):
    a_pk, _ = two_attempts

    def other_constraint(*args, **kwargs):
        raise IntegrityError("a different constraint")

    monkeypatch.setattr(services, "_upsert_connection", other_constraint)

    with pytest.raises(IntegrityError):
        exchange(owner, a_pk)

    assert attempt_row(owner, a_pk)["failure_code"] != "GOOGLE_ACCOUNT_ALREADY_CONNECTED"
    assert connection_rows(owner) == []
