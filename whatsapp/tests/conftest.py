"""Feature-local fixtures for the whatsapp tests (Fixture Rules: pytest fixtures
do not cross app test-package boundaries, so the merchant/session helpers are
copied from locations/tests/conftest.py).

The only external boundary mocked is Meta's HTTP call: `meta_http` replaces
`urllib.request.urlopen` as seen by whatsapp/providers/meta_cloud.py. No
service, task or view under test is mocked.
"""
import io
import json
import urllib.error
from pathlib import Path

import pytest
from django.core.cache import cache
from django.utils import timezone as dj_timezone
from rest_framework.settings import api_settings as drf_api_settings
from rest_framework.test import APIClient

from accounts import services as account_services
from accounts.models import TeamMember, User
from core.tenancy import tenant_atomic, tenant_context
from customers import services as customer_services
from locations import services as location_services
from whatsapp import services as whatsapp_services
from whatsapp.models import WhatsAppAccount
from whatsapp.tests.helpers import (
    ACCESS_TOKEN,
    APP_SECRET,
    SHARED_PHONE_NUMBER_ID,
    SHARED_WABA_ID,
    VERIFY_TOKEN,
    clear_platform_write_setting,
)

PASSWORD = "Tr1cky-Horse-Battery-Staple!"
FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def local_cache(settings):
    """Throttle counters must not leak through Redis between tests."""
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def make_merchant():
    counter = iter(range(1, 1000))

    def _make(name="Merchant", email=None):
        n = next(counter)
        return account_services.create_merchant_with_owner(
            name=f"{name} {n}",
            timezone="Asia/Kolkata",
            owner_email=email or f"wa-owner{n}@example.com",
            owner_password=PASSWORD,
        )

    return _make


@pytest.fixture
def add_member():
    def _add(merchant, role, email, *, accepted=True):
        user = User.objects.create_user(email, PASSWORD)
        with tenant_context(merchant.id), tenant_atomic():
            return TeamMember.objects.create(
                merchant=merchant,
                user=user,
                role=role,
                accepted_at=dj_timezone.now() if accepted else None,
            )

    return _add


@pytest.fixture
def session_client():
    """Factory: authenticated CSRF-enforcing client for a user email."""

    def _make(email):
        client = APIClient(enforce_csrf_checks=True)
        client.get("/api/v1/auth/login")
        token = client.cookies["csrftoken"].value
        resp = client.post(
            "/api/v1/auth/login",
            {"email": email, "password": PASSWORD},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        assert resp.status_code == 200, resp.content
        client.csrf = client.cookies["csrftoken"].value
        return client

    return _make


@pytest.fixture
def make_location():
    def _make(merchant, **kwargs):
        kwargs.setdefault("name", "Test Location")
        with tenant_context(merchant.id):
            return location_services.create_location(**kwargs)

    return _make


@pytest.fixture
def assign_manager():
    def _assign(owner, manager, *locations):
        with tenant_context(owner.merchant_id):
            return account_services.set_team_member_locations(
                actor=owner, member_id=manager.id, location_ids=[loc.id for loc in locations]
            )

    return _assign


@pytest.fixture
def meta_settings(settings):
    settings.META_APP_SECRET = APP_SECRET
    settings.META_WEBHOOK_VERIFY_TOKEN = VERIFY_TOKEN
    settings.META_SHARED_POOL_ACCESS_TOKEN = ACCESS_TOKEN
    return settings


@pytest.fixture
def make_shared_account(meta_settings):
    """The shared row, written through the audited platform path (never a raw
    ORM write)."""

    def _make(status="ACTIVE"):
        account = whatsapp_services.upsert_shared_pool_account(
            phone_number_id=SHARED_PHONE_NUMBER_ID, business_account_id=SHARED_WABA_ID, status=status
        )
        clear_platform_write_setting()
        return account

    return _make


@pytest.fixture
def shared_account(make_shared_account):
    return make_shared_account("ACTIVE")


@pytest.fixture
def make_own_account():
    """An OWN_NUMBER row for a merchant. This spec never creates one through a
    service, so it is written under that merchant's own context (allowed by
    the tenant_insert policy), standing in for the second spec."""
    counter = iter(range(1, 1000))

    def _make(merchant, status="ACTIVE"):
        n = next(counter)
        with tenant_context(merchant.id), tenant_atomic():
            return WhatsAppAccount.objects.create(
                merchant=merchant,
                sender_type=WhatsAppAccount.SenderType.OWN_NUMBER,
                provider=WhatsAppAccount.Provider.META_CLOUD,
                phone_number_id=f"OWN-PHONE-ID-{n}",
                business_account_id=f"OWN-WABA-ID-{n}",
                status=status,
            )

    return _make


@pytest.fixture
def make_customer():
    def _make(merchant, phone, name="Test Customer"):
        with tenant_context(merchant.id), tenant_atomic():
            customer, _ = customer_services.get_or_create_customer(phone=phone, name=name)
            return customer

    return _make


@pytest.fixture
def map_to_shared(make_location, shared_account):
    """Create a location for the merchant and map it to the shared account."""

    def _map(merchant, name="Mapped Location"):
        location = make_location(merchant, name=name)
        with tenant_context(merchant.id):
            whatsapp_services.set_location_sender(location=location, account=shared_account, actor=None)
        return location

    return _map


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def http_error(code: int, body: bytes = b'{"error": {"message": "SECRET-BODY-MARKER"}}'):
    return urllib.error.HTTPError("https://graph.facebook.com/test", code, "error", {}, io.BytesIO(body))


class _FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeMeta:
    """Stands in for urllib.request.urlopen. `on(substring, item)` answers every
    request whose URL contains the substring; `queue` answers the rest in order.
    An item is a JSON-able payload, or an Exception instance that is raised."""

    def __init__(self):
        self.routes = []
        self.queue = []
        self.requests = []
        self.timeouts = []

    def on(self, substring, item):
        self.routes.append((substring, item))

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        self.timeouts.append(timeout)
        item = None
        for substring, candidate in self.routes:
            if substring in req.full_url:
                item = candidate
                break
        else:
            if not self.queue:
                raise AssertionError(f"Unexpected Meta call: {req.get_method()} {req.full_url}")
            item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _FakeResponse(item)

    def urls(self):
        return [r.full_url for r in self.requests]

    def body(self, index=-1):
        return json.loads(self.requests[index].data)


@pytest.fixture
def meta_http(monkeypatch, meta_settings):
    fake = FakeMeta()
    monkeypatch.setattr("whatsapp.providers.meta_cloud.urllib.request.urlopen", fake)
    return fake


@pytest.fixture
def throttle_rates(monkeypatch):
    """Deterministically overrides the DRF rate the template-write throttle
    resolves at instantiation (same pattern as apikeys/tests/conftest.py:
    SimpleRateThrottle reads THROTTLE_RATES[scope] per instance, and
    override_settings cannot reach the dict bound at import time). Patched on
    the one subclass only, so every other throttle keeps its production rate;
    monkeypatch restores it after the test."""
    from whatsapp.views import TemplateWriteRateThrottle

    def _set(**rates):
        merged = {**drf_api_settings.DEFAULT_THROTTLE_RATES, **rates}
        monkeypatch.setattr(TemplateWriteRateThrottle, "THROTTLE_RATES", merged)

    return _set
