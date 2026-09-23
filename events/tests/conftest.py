"""Feature-local fixtures for events tests (Fixture Rules: pytest fixtures
do not cross app test-package boundaries).

FakeAdapter is the adapter double the whole ingestion pipeline is tested
against -- no concrete adapter exists in Phase 04 (spec: "Tests drive the
pipeline with a fake adapter that is registered only in tests")."""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone as dj_timezone
from rest_framework.test import APIClient

from accounts import services as account_services
from accounts.models import TeamMember, User
from core.tenancy import tenant_atomic, tenant_context
from integrations import services as integration_services
from integrations.core.adapters import BaseAdapter
from integrations.core.events import SaleCreated
from integrations.core.registry import ADAPTERS
from locations import services as location_services

PASSWORD = "Tr1cky-Horse-Battery-Staple!"


class FakeAdapter(BaseAdapter):
    """parse() returns the stored payload unchanged. normalize() builds a
    SaleCreated from it, or raises a caller-chosen exception when the
    payload carries a "__raise__" instruction -- this is what drives the
    "PII cannot leak" and "unexpected exception" tests without needing a
    real provider."""

    def verify(self, request):
        return True

    def parse(self, payload):
        return dict(payload)

    def normalize(self, parsed):
        raise_kind = parsed.pop("__raise__", None)
        if raise_kind == "value_error":
            raise ValueError(parsed.pop("__raise_message__", "boom"))
        if raise_kind == "review_flow_error":
            from core.exceptions import ReviewFlowError

            raise ReviewFlowError(parsed.pop("__raise_message__", "boom"))

        occurred_at = parsed.get("occurred_at")
        if isinstance(occurred_at, str):
            occurred_at = datetime.fromisoformat(occurred_at)

        amount = parsed.get("amount")
        if amount is not None and not isinstance(amount, Decimal):
            amount = Decimal(str(amount))

        return SaleCreated(
            source=parsed.get("source", "webhook"),
            external_transaction_id=parsed["external_transaction_id"],
            amount=amount,
            currency=parsed.get("currency", "INR"),
            occurred_at=occurred_at,
            customer_phone=parsed.get("customer_phone"),
            customer_name=parsed.get("customer_name"),
            payment_method=parsed.get("payment_method"),
            external_location_id=parsed.get("external_location_id"),
        )


def make_payload(**overrides):
    fields = dict(
        external_transaction_id="INV-1",
        amount="100.00",
        currency="INR",
        occurred_at="2024-01-01T10:00:00+00:00",
    )
    fields.update(overrides)
    return fields


@pytest.fixture(autouse=True)
def local_cache(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def register_webhook_provider(monkeypatch):
    monkeypatch.setitem(ADAPTERS, "webhook", FakeAdapter)


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
def make_location():
    def _make(merchant, **kwargs):
        kwargs.setdefault("name", "Test Location")
        with tenant_context(merchant.id):
            return location_services.create_location(**kwargs)

    return _make


@pytest.fixture
def make_integration(register_webhook_provider):
    def _make(merchant, provider="webhook", **kwargs):
        with tenant_context(merchant.id), tenant_atomic():
            owner = TeamMember.objects.get(merchant=merchant, role=TeamMember.Role.OWNER)
            return integration_services.connect_integration(actor=owner, provider=provider, **kwargs)

    return _make


@pytest.fixture
def make_mapping():
    def _make(merchant, integration, location, **kwargs):
        with tenant_context(merchant.id), tenant_atomic():
            return integration_services.add_location_mapping(integration, location_id=location.id, **kwargs)

    return _make
