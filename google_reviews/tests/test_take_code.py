"""Take-code compare-and-swap (spec "OAuth attempt lifecycle", decision 1 of the
2026-10-30 review). Real PostgreSQL with real commits: the single-winner claim
rests on row-lock behaviour, so nothing about it is mocked. The only mock is
the Google HTTP boundary (`fake_provider`)."""
import threading
import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from core.crypto import encrypt
from core.tenancy import tenant_atomic, tenant_context
from google_reviews import services
from google_reviews.models import GoogleConnection, GoogleOAuthState
from google_reviews.providers.base import GoogleTransientError

pytestmark = pytest.mark.django_db(transaction=True)


def make_attempt(owner, *, status="PENDING", with_code=True, expires_in=600, failure_code=None):
    with tenant_context(owner.merchant_id), tenant_atomic():
        return GoogleOAuthState.objects.create(
            merchant_id=owner.merchant_id,
            user=owner.user,
            state_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            nonce_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            status=status,
            code_enc=encrypt("auth-code") if with_code else None,
            failure_code=failure_code,
            expires_at=timezone.now() + timedelta(seconds=expires_in),
        )


def snapshot(owner, attempt):
    with tenant_context(owner.merchant_id), tenant_atomic():
        return GoogleOAuthState.objects.filter(pk=attempt.pk).values(
            "status", "failure_code", "code_enc", "google_connection_id", "completed_at"
        ).get()


def take(owner, attempt):
    with tenant_context(owner.merchant_id):
        return services._take_code(attempt.pk)


class _ReadHook:
    def __init__(self, qs, after):
        self.qs, self.after = qs, after

    def values_list(self, *args, **kwargs):
        return _ReadHook(self.qs.values_list(*args, **kwargs), self.after)

    def first(self):
        row = self.qs.first()
        self.after()  # the read has happened, the UPDATE has not
        return row


class _ManagerProxy:
    """Lets the first read through, then runs `after` before returning it."""

    def __init__(self, real, after):
        self.real, self.after, self.calls = real, after, 0

    def filter(self, **kwargs):
        self.calls += 1
        qs = self.real.filter(**kwargs)
        return _ReadHook(qs, self.after) if self.calls == 1 else qs

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_stale_read_loses_with_zero_rows_and_makes_no_provider_call(make_merchant, fake_provider, spawn, monkeypatch):
    owner = make_merchant("A")
    attempt = make_attempt(owner)
    fake_provider.exchange_error = GoogleTransientError("unused")
    winner = []

    def other_worker_takes_it():
        winner.append(spawn(lambda: services._take_code(attempt.pk), owner.merchant_id).join())

    proxy = _ManagerProxy(GoogleOAuthState.objects, other_worker_takes_it)
    monkeypatch.setattr(GoogleOAuthState, "objects", proxy)

    # Worker A read the ciphertext first; worker B took it before A's UPDATE.
    assert take(owner, attempt) is None
    assert winner[0] is not None and winner[0][2] == owner.user.pk
    assert fake_provider.exchange_calls == 0
    assert snapshot(owner, attempt)["code_enc"] is None


def test_stale_read_through_exchange_attempt_makes_no_google_call(make_merchant, fake_provider, spawn, monkeypatch):
    owner = make_merchant("A")
    attempt = make_attempt(owner)
    proxy = _ManagerProxy(
        GoogleOAuthState.objects,
        lambda: spawn(lambda: services._take_code(attempt.pk), owner.merchant_id).join(),
    )
    monkeypatch.setattr(GoogleOAuthState, "objects", proxy)

    with tenant_context(owner.merchant_id):
        services.exchange_attempt(attempt.pk)

    assert fake_provider.exchange_calls == 0


def test_barrier_started_workers_give_one_winner_and_one_exchange(make_merchant, fake_provider, spawn):
    owner = make_merchant("A")
    attempt = make_attempt(owner)
    fake_provider.exchange_error = GoogleTransientError("boom")
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait(30)
        services.exchange_attempt(attempt.pk)

    workers = [spawn(worker, owner.merchant_id) for _ in range(6)]
    for w in workers:
        w.join()

    assert fake_provider.exchange_calls == 1
    row = snapshot(owner, attempt)
    assert row["code_enc"] is None
    assert (row["status"], row["failure_code"]) == ("FAILED", "EXCHANGE_FAILED")


def test_sequential_duplicate_delivery_makes_one_call(make_merchant, fake_provider):
    owner = make_merchant("A")
    attempt = make_attempt(owner)
    fake_provider.exchange_error = GoogleTransientError("boom")

    with tenant_context(owner.merchant_id):
        services.exchange_attempt(attempt.pk)
        services.exchange_attempt(attempt.pk)

    assert fake_provider.exchange_calls == 1


def test_the_loser_changes_nothing_and_writes_nothing(make_merchant, fake_provider):
    owner = make_merchant("A")
    attempt = make_attempt(owner)
    assert take(owner, attempt) is not None  # the winner
    before = snapshot(owner, attempt)

    assert take(owner, attempt) is None  # the loser

    assert snapshot(owner, attempt) == before
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert GoogleConnection.objects.count() == 0
    assert fake_provider.exchange_calls == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"expires_in": -5},
        {"with_code": False},
        {"status": "FAILED", "with_code": False, "failure_code": "EXCHANGE_FAILED"},
    ],
    ids=["expired", "no-code", "terminal"],
)
def test_an_unclaimable_attempt_makes_no_provider_call(make_merchant, fake_provider, kwargs):
    owner = make_merchant("A")
    attempt = make_attempt(owner, **kwargs)
    before = snapshot(owner, attempt)

    with tenant_context(owner.merchant_id):
        services.exchange_attempt(attempt.pk)

    assert fake_provider.exchange_calls == 0
    assert snapshot(owner, attempt) == before


def test_the_code_is_cleared_in_the_database_before_the_provider_is_called(make_merchant, fake_provider):
    owner = make_merchant("A")
    attempt = make_attempt(owner)
    fake_provider.exchange_error = GoogleTransientError("boom")
    seen = {}

    def at_call_time():
        seen["in_atomic_block"] = connection.in_atomic_block
        seen["code_enc"] = snapshot(owner, attempt)["code_enc"]

    fake_provider.exchange_hook = at_call_time

    with tenant_context(owner.merchant_id):
        services.exchange_attempt(attempt.pk)

    assert seen == {"in_atomic_block": False, "code_enc": None}
