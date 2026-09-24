"""Feature-local fixtures for apikeys tests. Duplicates the accounts/tests/
conftest.py fixture shapes (make_merchant, add_member, session_client,
csrf_client, local_cache): pytest fixtures don't cross app test-package
boundaries, so each app's tests/ directory carries its own copy per the
Fixture Rules (locations/tests/conftest.py, integrations/tests/conftest.py)."""
import pytest
from django.core.cache import cache
from django.utils import timezone as dj_timezone
from rest_framework.settings import api_settings as drf_api_settings
from rest_framework.test import APIClient

from accounts import services as account_services
from accounts.models import TeamMember, User
from apikeys import services
from apikeys.throttling import ApiKeyIpRateThrottle, ApiKeyRateThrottle
from core.tenancy import tenant_atomic, tenant_context

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
def merchant_a(make_merchant):
    return make_merchant("A").merchant


@pytest.fixture
def merchant_b(make_merchant):
    return make_merchant("B").merchant


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
    """Logs in through the real endpoint and returns (client, csrf_token)."""

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
def make_key():
    """Creates an ApiKey through the real service (never a raw ORM write),
    inside the owning merchant's tenant context. owner is the TeamMember
    make_merchant() returns. Returns (key, raw)."""

    def _make(owner, scopes=("sales:write",)):
        with tenant_context(owner.merchant_id):
            return services.create_api_key(actor=owner, scopes=list(scopes))

    return _make


@pytest.fixture
def throttle_rates(monkeypatch):
    """Deterministically overrides the DRF throttle rate the two ApiKey
    throttle subclasses resolve at instantiation.

    SimpleRateThrottle.__init__ (rest_framework/throttling.py, installed
    djangorestframework==3.18.1) resolves the rate per instance through
    get_rate(), which reads self.THROTTLE_RATES[self.scope]. Patching
    THROTTLE_RATES on our subclasses is therefore picked up by every
    throttle instance created during a request, including the one
    apikeys.middleware.ApiKeyMiddleware constructs.

    override_settings(REST_FRAMEWORK=...) does NOT work here:
    SimpleRateThrottle.THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES
    is bound to that dict at class-definition (import) time, and
    api_settings.reload() builds a new dict object, so the class keeps
    pointing at the old one. A class-level `rate` patch is not used either:
    it bypasses get_rate() and the scope lookup entirely.

    Patching THROTTLE_RATES only on ApiKeyRateThrottle/ApiKeyIpRateThrottle
    (never the base SimpleRateThrottle.THROTTLE_RATES) leaves the Phase 02
    login/invite/totp throttles unaffected. monkeypatch restores the
    original class attribute after the test; production defaults (600/min,
    1200/min) are never touched.
    """

    def _set(**rates):
        merged = {**drf_api_settings.DEFAULT_THROTTLE_RATES, **rates}
        for cls in (ApiKeyRateThrottle, ApiKeyIpRateThrottle):
            monkeypatch.setattr(cls, "THROTTLE_RATES", merged)

    return _set
