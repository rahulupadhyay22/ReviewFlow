"""Feature-local fixtures for locations tests. No external providers are used.

These duplicate the accounts/tests/conftest.py fixture shapes (make_merchant,
add_member, session_client, csrf_client, local_cache): pytest fixtures don't
cross app test-package boundaries, so each app's tests/ directory carries its
own copy per the Fixture Rules (feature-local, not a shared conftest.py).
"""
import pytest
from django.core.cache import cache
from django.utils import timezone as dj_timezone
from rest_framework.test import APIClient

from accounts import services as account_services
from accounts.models import TeamMember, User
from core.tenancy import tenant_atomic, tenant_context
from locations import services as location_services

PASSWORD = "Tr1cky-Horse-Battery-Staple!"


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
    """Factory: authenticated CSRF-enforcing client for a user email."""

    def _make(email):
        client, resp = login(email)
        assert resp.status_code == 200, resp.content
        client.csrf = client.cookies["csrftoken"].value
        return client

    return _make


@pytest.fixture
def make_location():
    """Creates a Location through the real service (never a raw ORM write),
    inside the given merchant's tenant context."""

    def _make(merchant, **kwargs):
        kwargs.setdefault("name", "Test Location")
        with tenant_context(merchant.id):
            return location_services.create_location(**kwargs)

    return _make
