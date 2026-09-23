"""API tests for /integrations (API-Specification.md §Integrations; spec
Definition of done: "Integrations API and services")."""
import pytest
from rest_framework.test import APIClient

from core.crypto import decrypt
from core.tenancy import tenant_atomic, tenant_context
from integrations import services

pytestmark = pytest.mark.django_db

INTEGRATIONS = "/api/v1/integrations"


def _connect_url(provider):
    return f"{INTEGRATIONS}/{provider}/connect"


def _detail(pk):
    return f"{INTEGRATIONS}/{pk}"


def _locations_url(pk):
    return f"{INTEGRATIONS}/{pk}/locations"


def _post(client, url, data):
    return client.post(url, data, format="json", HTTP_X_CSRFTOKEN=client.csrf)


def _patch(client, url, data):
    return client.patch(url, data, format="json", HTTP_X_CSRFTOKEN=client.csrf)


def _put(client, url, data):
    return client.put(url, data, format="json", HTTP_X_CSRFTOKEN=client.csrf)


def _delete(client, url):
    return client.delete(url, HTTP_X_CSRFTOKEN=client.csrf)


def _connect(owner, provider="webhook", **kwargs):
    with tenant_context(owner.merchant_id), tenant_atomic():
        return services.connect_integration(actor=owner, provider=provider, **kwargs)


# --- connect -------------------------------------------------------------


@pytest.mark.parametrize("role", ["OWNER", "ADMIN"])
def test_owner_and_admin_can_connect(role, make_merchant, add_member, session_client, register_webhook_provider):
    owner = make_merchant("A")
    if role == "OWNER":
        client = session_client(owner.user.email)
    else:
        add_member(owner.merchant, "ADMIN", "admin@example.com")
        client = session_client("admin@example.com")

    resp = _post(client, _connect_url("webhook"), {"credentials": {"api_key": "secret-123"}})
    assert resp.status_code == 201
    body = resp.json()
    assert body["provider"] == "webhook"
    assert body["status"] == "CONNECTED"
    assert body["has_credentials"] is True
    assert "credentials" not in body
    assert "credentials_encrypted" not in body
    assert body["mappings"] == []

    with tenant_context(owner.merchant_id), tenant_atomic():
        integration = services.get_integration(body["id"])
    assert "secret-123" not in integration.credentials_encrypted
    assert decrypt(integration.credentials_encrypted) == '{"api_key": "secret-123"}'


def test_connect_rejects_unregistered_provider(make_merchant, session_client):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = _post(client, _connect_url("webhook"), {})
    assert resp.status_code == 422


def test_connect_rejects_unknown_provider(make_merchant, session_client, register_webhook_provider):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = _post(client, _connect_url("not-a-provider"), {})
    assert resp.status_code == 422


def test_connect_rejects_non_object_credentials(make_merchant, session_client, register_webhook_provider):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = _post(client, _connect_url("webhook"), {"credentials": "not-an-object"})
    assert resp.status_code == 422


# --- permissions -----------------------------------------------------------


def test_manager_and_viewer_forbidden_on_every_integrations_endpoint(
    make_merchant, add_member, session_client, make_location, register_webhook_provider
):
    owner = make_merchant("A")
    integration = _connect(owner)
    location = make_location(owner.merchant)
    add_member(owner.merchant, "MANAGER", "manager@example.com")
    add_member(owner.merchant, "VIEWER", "viewer@example.com")

    for email in ("manager@example.com", "viewer@example.com"):
        client = session_client(email)
        assert _post(client, _connect_url("webhook"), {}).status_code == 403
        assert client.get(INTEGRATIONS).status_code == 403
        assert _patch(client, _detail(integration.id), {"config_json": {}}).status_code == 403
        assert _post(client, _locations_url(integration.id), {"location_id": str(location.id)}).status_code == 403
        assert _put(client, _locations_url(integration.id), {"mappings": []}).status_code == 403
        assert _delete(client, _detail(integration.id)).status_code == 403


def test_unauthenticated_request_is_forbidden():
    client = APIClient()
    resp = client.get(INTEGRATIONS)
    assert resp.status_code == 403


def test_missing_csrf_token_is_forbidden(make_merchant, session_client, register_webhook_provider):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = client.post(_connect_url("webhook"), {}, format="json")  # no X-CSRFToken
    assert resp.status_code == 403


# --- list / detail -----------------------------------------------------


def test_list_integrations_scoped_to_merchant(make_merchant, session_client, register_webhook_provider):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_a = _connect(owner_a)
    _connect(owner_b)

    client = session_client(owner_a.user.email)
    resp = client.get(INTEGRATIONS)
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()["results"]}
    assert ids == {str(integration_a.id)}


def test_patch_updates_config_and_cross_merchant_id_is_404(
    make_merchant, session_client, register_webhook_provider
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_a = _connect(owner_a)
    integration_b = _connect(owner_b)

    client = session_client(owner_a.user.email)
    resp = _patch(client, _detail(integration_a.id), {"config_json": {"a": 1}})
    assert resp.status_code == 200
    assert resp.json()["config_json"] == {"a": 1}

    resp = _patch(client, _detail(integration_b.id), {"config_json": {}})
    assert resp.status_code == 404


def test_delete_disconnects_and_is_idempotent(make_merchant, session_client, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner, credentials={"k": "v"})
    client = session_client(owner.user.email)

    resp = _delete(client, _detail(integration.id))
    assert resp.status_code == 204

    resp = client.get(INTEGRATIONS)
    assert resp.json()["results"][0]["status"] == "DISCONNECTED"
    assert resp.json()["results"][0]["has_credentials"] is False

    resp = _delete(client, _detail(integration.id))
    assert resp.status_code == 204


# --- mappings ------------------------------------------------------------


def test_post_mapping_created_then_duplicate_returns_409(
    make_merchant, make_location, session_client, register_webhook_provider
):
    owner = make_merchant("A")
    integration = _connect(owner)
    location = make_location(owner.merchant)
    client = session_client(owner.user.email)

    resp = _post(client, _locations_url(integration.id), {"location_id": str(location.id)})
    assert resp.status_code == 201
    assert resp.json()["location_id"] == str(location.id)

    resp = _post(client, _locations_url(integration.id), {"location_id": str(location.id)})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "mapping_exists"


def test_post_mapping_unknown_and_cross_merchant_location_return_identical_422(
    make_merchant, make_location, session_client, register_webhook_provider
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration = _connect(owner_a)
    location_b = make_location(owner_b.merchant)
    client = session_client(owner_a.user.email)

    resp_cross = _post(client, _locations_url(integration.id), {"location_id": str(location_b.id)})
    resp_unknown = _post(
        client, _locations_url(integration.id), {"location_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert resp_cross.status_code == 422
    assert resp_unknown.status_code == 422
    assert resp_cross.json() == resp_unknown.json()


def test_post_mapping_malformed_uuid_returns_422(make_merchant, session_client, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    client = session_client(owner.user.email)
    resp = _post(client, _locations_url(integration.id), {"location_id": "not-a-uuid"})
    assert resp.status_code == 422


def test_put_mappings_replaces_atomically(make_merchant, make_location, session_client, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    loc1 = make_location(owner.merchant, name="L1")
    loc2 = make_location(owner.merchant, name="L2")
    client = session_client(owner.user.email)

    _post(client, _locations_url(integration.id), {"location_id": str(loc1.id)})

    resp = _put(client, _locations_url(integration.id), {"mappings": [{"location_id": str(loc2.id)}]})
    assert resp.status_code == 200
    mapping_locations = {m["location_id"] for m in resp.json()["mappings"]}
    assert mapping_locations == {str(loc2.id)}
