"""Feature-local fixtures for the google_reviews tests (Fixture Rules: pytest
fixtures do not cross app test-package boundaries, so the merchant and session
helpers are copied from whatsapp/tests/conftest.py).

The only mocked boundary is Google: `fake_provider` replaces
`google_reviews.services.get_provider`. No service, task or view under test is
mocked. Tests that need real commits use django_db(transaction=True).
"""
import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from django.core.cache import cache
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from accounts import services as account_services
from core.crypto import encrypt
from core.tenancy import tenant_atomic, tenant_context
from google_reviews import services
from google_reviews.models import GoogleConnection, GoogleLocation, GoogleReview
from google_reviews.providers.base import ProviderLocation, ReviewPage
from locations import services as location_services

PASSWORD = "Tr1cky-Horse-Battery-Staple!"
ACCESS_TOKEN = "ACCESS-TOKEN-SENTINEL"
GOOGLE_A = "accounts/1/locations/A"
GOOGLE_B = "accounts/1/locations/B"


@pytest.fixture(autouse=True)
def local_cache(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def fernet_key(settings):
    settings.FERNET_KEY = Fernet.generate_key().decode()
    return settings.FERNET_KEY


@pytest.fixture
def make_merchant():
    counter = iter(range(1, 1000))

    def _make(name="Merchant"):
        n = next(counter)
        return account_services.create_merchant_with_owner(
            name=f"{name} {n}",
            timezone="Asia/Kolkata",
            owner_email=f"g-owner{n}@example.com",
            owner_password=PASSWORD,
        )

    return _make


@pytest.fixture
def session_client():
    def _make(email):
        client = APIClient(enforce_csrf_checks=True)
        client.get("/api/v1/auth/login")
        token = client.cookies["csrftoken"].value
        resp = client.post(
            "/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json", HTTP_X_CSRFTOKEN=token
        )
        assert resp.status_code == 200, resp.content
        client.csrf = client.cookies["csrftoken"].value
        return client

    return _make


@pytest.fixture
def make_location():
    def _make(merchant, name="Test Location"):
        with tenant_context(merchant.id):
            return location_services.create_location(name=name)

    return _make


@pytest.fixture
def make_connection():
    def _make(merchant, sub="sub-1"):
        with tenant_context(merchant.id), tenant_atomic():
            return GoogleConnection.objects.create(
                merchant=merchant,
                google_account_sub=sub,
                google_account_email=f"{sub}@example.com",
                access_token_enc=encrypt(ACCESS_TOKEN),
                refresh_token_enc=encrypt("REFRESH-TOKEN-SENTINEL"),
                expires_at=timezone.now() + timedelta(hours=1),
                status=GoogleConnection.Status.ACTIVE,
                connected_at=timezone.now(),
            )

    return _make


@pytest.fixture
def make_mapping():
    """A GoogleLocation for `location` with `reviews` synced reviews and set
    counters."""

    def _make(merchant, location, connection, google_location_id=GOOGLE_A, reviews=2):
        with tenant_context(merchant.id), tenant_atomic():
            mapping = GoogleLocation.objects.create(
                location=location,
                google_connection=connection,
                google_location_id=google_location_id,
                google_place_id="place-" + google_location_id[-1],
                review_link="https://g.page/r/" + google_location_id[-1] + "/review",
                average_rating=Decimal("4.5"),
                review_count=reviews,
                last_synced_at=timezone.now(),
            )
            for n in range(reviews):
                GoogleReview.objects.create(
                    google_location=mapping,
                    google_review_id=f"{google_location_id[-1]}-old-{n}",
                    rating=5,
                    review_created_at=timezone.now(),
                    fetched_at=timezone.now(),
                )
            return mapping

    return _make


def provider_location(google_location_id):
    suffix = google_location_id[-1]
    return ProviderLocation(
        google_location_id=google_location_id,
        title=f"Title {suffix}",
        google_place_id=f"place-{suffix}",
        review_uri=f"https://g.page/r/{suffix}/review",
    )


class FakeProvider:
    """Stands in for the Google HTTP boundary. Records whether each call ran
    inside a database transaction."""

    def __init__(self):
        self.locations = [provider_location(GOOGLE_A), provider_location(GOOGLE_B)]
        self.list_error = None
        self.list_in_atomic = []
        self.exchange_calls = 0
        self.exchange_error = None
        self.exchange_hook = None
        self.pages = {}
        self.on_list_reviews = None
        self._lock = threading.Lock()

    def list_locations(self, access_token):
        self.list_in_atomic.append(connection.in_atomic_block)
        if self.list_error is not None:
            raise self.list_error
        return list(self.locations)

    def list_reviews(self, access_token, google_location_id, page_token=None):
        if self.on_list_reviews is not None:
            self.on_list_reviews(google_location_id, page_token)
        return self.pages.get((google_location_id, page_token), ReviewPage([], None, None, None))

    def exchange_code(self, code):
        with self._lock:
            self.exchange_calls += 1
        if self.exchange_hook is not None:
            self.exchange_hook()
        raise self.exchange_error


@pytest.fixture
def fake_provider(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(services, "get_provider", lambda: provider)
    return provider


@pytest.fixture
def sync_enqueues(monkeypatch):
    """Records every sync_location.delay and whether it ran inside a
    transaction."""
    from google_reviews import tasks

    calls = []
    monkeypatch.setattr(
        tasks.sync_location, "delay", lambda *args: calls.append((args, connection.in_atomic_block))
    )
    return calls


class Spawned:
    def __init__(self, fn, merchant_id):
        self.result, self.error = None, None

        def run():
            try:
                with tenant_context(merchant_id):
                    self.result = fn()
            except BaseException as exc:  # captured and asserted by the test
                self.error = exc
            finally:
                connection.close()

        self.thread = threading.Thread(target=run)
        self.thread.start()

    def join(self, timeout=30):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "thread did not finish"
        assert self.error is None, self.error
        return self.result


@pytest.fixture
def spawn():
    """Run fn() in a thread with its own database connection, inside the given
    merchant's tenant context. The connection is closed when the thread ends."""
    return Spawned
