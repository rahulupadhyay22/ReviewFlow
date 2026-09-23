"""Feature-local fixtures for integrations tests. Duplicates the base
fixture shapes from locations/tests/conftest.py (Fixture Rules: pytest
fixtures do not cross app test-package boundaries)."""
import pytest
from django.core.cache import cache
from django.utils import timezone as dj_timezone
from rest_framework.test import APIClient

from accounts import services as account_services
from accounts.models import TeamMember, User
from core.tenancy import tenant_atomic, tenant_context
from integrations.core.adapters import BaseAdapter
from integrations.core.registry import ADAPTERS
from locations import services as location_services

PASSWORD = "Tr1cky-Horse-Battery-Staple!"


class _StubAdapter(BaseAdapter):
    """Minimal adapter double: just enough to be "registered" for
    connect_integration's provider check. Its parse/normalize are not
    exercised by the integrations app's own tests -- the pipeline itself
    (events app) uses its own richer FakeAdapter."""

    def verify(self, request):
        return True

    def parse(self, payload):
        return payload

    def normalize(self, parsed):
        raise NotImplementedError


@pytest.fixture(autouse=True)
def local_cache(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def register_webhook_provider(monkeypatch):
    """Registers "webhook" in the adapter registry, and only for the
    duration of the test -- the production registry stays empty."""
    monkeypatch.setitem(ADAPTERS, "webhook", _StubAdapter)


@pytest.fixture
def make_merchant():
    counter = iter(range(1, 1000))

    def _make(name="Merchant", email=None):
        n = next(counter)
        return account_services.create_merchant_with_owner(
            name=f"{name} {n}",
            timezone="Asia/Kolkata",
            owner_email=email or f"owner{n}@example.com",
            owner_password=PASSWORD,
        )

    return _make


@pytest.fixture
def add_member():
    def _add(merchant, role, email, *, accepted=True, user=None):
        user = user or User.objects.create_user(email, PASSWORD)
        with tenant_context(merchant.id), tenant_atomic():
            return TeamMember.objects.create(
                merchant=merchant,
                user=user,
                role=role,
                accepted_at=dj_timezone.now() if accepted else None,
            )

    return _add


@pytest.fixture
def csrf_client():
    return APIClient(enforce_csrf_checks=True)


@pytest.fixture
def login(csrf_client):
    def _login(email, password=PASSWORD, client=None):
        client = client or APIClient(enforce_csrf_checks=True)
        client.get("/api/v1/auth/login")
        token = client.cookies["csrftoken"].value
        resp = client.post(
            "/api/v1/auth/login",
            {"email": email, "password": password},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        return client, resp

    return _login


@pytest.fixture
def session_client(login):
    def _make(email):
        client, resp = login(email)
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
