"""Per-provider connect/PATCH rules for the Phase 06 adapters
(spec .claude/specs/06-priority-integrations.md Decisions 4, 5, 6).
Exercises the real, production-registered webhook/csv/api adapters."""
import json

import pytest
from django.core.cache import cache

from core.crypto import decrypt
from core.tenancy import tenant_atomic, tenant_context
from integrations import services
from integrations.exceptions import IntegrationExists
from integrations.models import Integration

pytestmark = pytest.mark.django_db

INTEGRATIONS = "/api/v1/integrations"


def _connect_url(provider):
    return f"{INTEGRATIONS}/{provider}/connect"


def _detail(pk):
    return f"{INTEGRATIONS}/{pk}"


def _post(client, url, data):
    return client.post(url, data, format="json", HTTP_X_CSRFTOKEN=client.csrf)


def _patch(client, url, data):
    return client.patch(url, data, format="json", HTTP_X_CSRFTOKEN=client.csrf)


@pytest.fixture(autouse=True)
def local_cache(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    yield
    cache.clear()


VALID_FIELD_MAP = {
    "field_map": {
        "external_transaction_id": "id",
        "amount": "total",
        "currency": "currency",
        "occurred_at": "completed_at",
    }
}


# --- Generic Webhook: issued secret ---------------------------------------


def test_connecting_webhook_returns_201_with_server_issued_secret(make_merchant, session_client):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = _post(client, _connect_url("webhook"), {"config_json": VALID_FIELD_MAP})
    assert resp.status_code == 201
    body = resp.json()
    assert body["webhook_secret"].startswith("whsec_")

    with tenant_context(owner.merchant_id), tenant_atomic():
        integration = services.get_integration(body["id"])
    stored = json.loads(decrypt(integration.credentials_encrypted))
    assert stored["webhook_secret"] == body["webhook_secret"]


def test_webhook_secret_is_never_returned_again(make_merchant, session_client):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = _post(client, _connect_url("webhook"), {"config_json": VALID_FIELD_MAP})
    integration_id = resp.json()["id"]

    detail_resp = client.get(_detail(integration_id))
    assert "webhook_secret" not in detail_resp.json()
    assert "credentials_encrypted" not in detail_resp.json()

    list_resp = client.get(INTEGRATIONS)
    for row in list_resp.json()["results"]:
        assert "webhook_secret" not in row


def test_connecting_webhook_with_client_credentials_returns_422(make_merchant, session_client):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = _post(
        client,
        _connect_url("webhook"),
        {"credentials": {"webhook_secret": "attacker-chosen"}, "config_json": VALID_FIELD_MAP},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "config_json",
    [
        None,
        {},
        {"field_map": {}},
        {"field_map": {"amount": "total"}},  # missing external_transaction_id/occurred_at
        {"field_map": {"external_transaction_id": "id", "amount": "total", "occurred_at": "at"}},  # no currency
    ],
)
def test_connecting_webhook_with_invalid_field_map_returns_422(make_merchant, session_client, config_json):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = _post(client, _connect_url("webhook"), {"config_json": config_json})
    assert resp.status_code == 422


def test_patch_webhook_config_with_invalid_field_map_returns_422_and_changes_nothing(
    make_merchant, session_client
):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = _post(client, _connect_url("webhook"), {"config_json": VALID_FIELD_MAP})
    integration_id = resp.json()["id"]

    bad_resp = _patch(client, _detail(integration_id), {"config_json": {"field_map": {}}})
    assert bad_resp.status_code == 422

    with tenant_context(owner.merchant_id), tenant_atomic():
        integration = services.get_integration(integration_id)
    assert integration.config_json == VALID_FIELD_MAP


# --- CSV / API: no client credentials --------------------------------


@pytest.mark.parametrize("provider", ["csv", "api"])
def test_connecting_csv_or_api_with_any_credentials_returns_422(make_merchant, session_client, provider):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = _post(client, _connect_url(provider), {"credentials": {"anything": "x"}})
    assert resp.status_code == 422


@pytest.mark.parametrize("provider", ["csv", "api"])
def test_connecting_csv_or_api_without_credentials_returns_201(make_merchant, session_client, provider):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = _post(client, _connect_url(provider), {})
    assert resp.status_code == 201
    assert resp.json()["has_credentials"] is False


# --- api: at most one CONNECTED per merchant -------------------------


def test_connecting_a_second_api_integration_returns_409(make_merchant, session_client):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    first = _post(client, _connect_url("api"), {})
    assert first.status_code == 201
    second = _post(client, _connect_url("api"), {})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "integration_exists"


def test_reconnecting_api_after_disconnect_succeeds(make_merchant, session_client):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    first = _post(client, _connect_url("api"), {})
    integration_id = first.json()["id"]
    client.delete(_detail(integration_id), HTTP_X_CSRFTOKEN=client.csrf)

    second = _post(client, _connect_url("api"), {})
    assert second.status_code == 201


def test_two_merchants_can_each_connect_one_api_integration(make_merchant, session_client):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    client_a = session_client(owner_a.user.email)
    client_b = session_client(owner_b.user.email)
    assert _post(client_a, _connect_url("api"), {}).status_code == 201
    assert _post(client_b, _connect_url("api"), {}).status_code == 201


def test_connect_integration_service_raises_integration_exists_for_duplicate_api(make_merchant):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.connect_integration(actor=owner, provider="api")
        with pytest.raises(IntegrationExists):
            services.connect_integration(actor=owner, provider="api")


# --- shopify / woocommerce remain unregistered in this workstream ----


@pytest.mark.parametrize("provider", ["shopify", "woocommerce", "petpooja", "gofrugal", "zapier", "make"])
def test_phase17_and_shopify_providers_are_unregistered(make_merchant, session_client, provider):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = _post(client, _connect_url(provider), {})
    assert resp.status_code == 422


def test_shopify_patch_always_returns_422_once_registered_is_not_applicable_yet(make_merchant):
    """Documents the Phase 06/A boundary: shopify has no adapter here, so
    connect_integration() itself rejects it with 422 -- the Shopify-specific
    "PATCH always 422" rule is exercised in the 06-shopify-app workstream."""
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(Exception):
            services.connect_integration(actor=owner, provider="shopify")
