"""GET/POST /api-keys, DELETE /api-keys/{id} (spec §"API endpoints",
Definition of done §"Key management API"). Session + CSRF, OWNER/ADMIN only
(Decision 10)."""
import hashlib
import re

import pytest
from rest_framework.test import APIClient

from accounts.models import TeamMember
from apikeys.models import ApiKey
from auditlog.models import AuditLog
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

API_KEYS = "/api/v1/api-keys"
KEY_RE = re.compile(r"^rf_live_[A-Za-z0-9_-]{32}$")


def _member(merchant, add_member, role, email):
    return add_member(merchant, role, email)


# --- POST /api-keys ----------------------------------------------------


@pytest.mark.parametrize("role", [TeamMember.Role.OWNER, TeamMember.Role.ADMIN])
def test_post_api_keys_as_owner_or_admin_returns_201_with_plaintext_key(merchant_a, add_member, session_client, role):
    _member(merchant_a, add_member, role, f"post-{role}@example.com")
    client = session_client(f"post-{role}@example.com")
    resp = client.post(API_KEYS, {"scopes": ["sales:write"]}, format="json", HTTP_X_CSRFTOKEN=client.csrf)
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"id", "scopes", "is_active", "created_at", "last_used_at", "key"}
    assert KEY_RE.match(body["key"])
    assert body["scopes"] == ["sales:write"]
    assert body["is_active"] is True


@pytest.mark.parametrize("role", [TeamMember.Role.MANAGER, TeamMember.Role.VIEWER])
def test_post_api_keys_as_manager_or_viewer_returns_403(merchant_a, add_member, session_client, role):
    _member(merchant_a, add_member, role, f"post-denied-{role}@example.com")
    client = session_client(f"post-denied-{role}@example.com")
    resp = client.post(API_KEYS, {"scopes": ["sales:write"]}, format="json", HTTP_X_CSRFTOKEN=client.csrf)
    assert resp.status_code == 403


def test_post_api_keys_without_csrf_returns_403(merchant_a, add_member, login):
    _member(merchant_a, add_member, TeamMember.Role.OWNER, "post-nocsrf@example.com")
    client, resp = login("post-nocsrf@example.com")
    assert resp.status_code == 200
    post_resp = client.post(API_KEYS, {"scopes": ["sales:write"]}, format="json")
    assert post_resp.status_code == 403


def test_post_api_keys_with_api_key_and_no_session_returns_403(merchant_a, add_member, make_key):
    owner = _member(merchant_a, add_member, TeamMember.Role.OWNER, "post-viakey@example.com")
    _, raw = make_key(owner)
    client = APIClient()
    resp = client.post(
        API_KEYS, {"scopes": ["sales:write"]}, format="json", HTTP_AUTHORIZATION=f"Bearer {raw}"
    )
    assert resp.status_code == 403
    with tenant_context(merchant_a.id), tenant_atomic():
        assert ApiKey.objects.count() == 1  # only the key used to make the call


def test_post_api_keys_ignores_client_supplied_identity_fields(merchant_a, merchant_b, add_member, session_client):
    _member(merchant_a, add_member, TeamMember.Role.OWNER, "post-ignore@example.com")
    client = session_client("post-ignore@example.com")
    resp = client.post(
        API_KEYS,
        {
            "scopes": ["sales:write"],
            "merchant_id": str(merchant_b.id),
            "id": "00000000-0000-0000-0000-000000000000",
            "is_active": False,
            "key_hash": "z" * 64,
        },
        format="json",
        HTTP_X_CSRFTOKEN=client.csrf,
    )
    assert resp.status_code == 201
    with tenant_context(merchant_a.id), tenant_atomic():
        key = ApiKey.objects.get(id=resp.json()["id"])
    assert key.merchant_id == merchant_a.id
    assert key.is_active is True
    assert key.key_hash == hashlib.sha256(resp.json()["key"].encode()).hexdigest()


@pytest.mark.parametrize(
    "scopes",
    [[], "sales:write", ["not_a_real_scope"], ["sales:write", "sales:write"]],
)
def test_post_api_keys_with_invalid_scopes_returns_422(merchant_a, add_member, session_client, scopes):
    _member(merchant_a, add_member, TeamMember.Role.OWNER, "post-invalid-scopes@example.com")
    client = session_client("post-invalid-scopes@example.com")
    resp = client.post(API_KEYS, {"scopes": scopes}, format="json", HTTP_X_CSRFTOKEN=client.csrf)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert "scopes" in error["field_errors"]


# --- GET /api-keys ----------------------------------------------------


def test_get_api_keys_never_includes_key_or_key_hash(merchant_a, add_member, session_client, make_key):
    owner = _member(merchant_a, add_member, TeamMember.Role.OWNER, "get-noleak@example.com")
    make_key(owner)
    client = session_client("get-noleak@example.com")
    resp = client.get(API_KEYS)
    assert resp.status_code == 200
    for row in resp.json()["results"]:
        assert "key" not in row
        assert "key_hash" not in row


def test_get_api_keys_lists_only_the_callers_merchant(merchant_a, merchant_b, add_member, session_client, make_key):
    owner_a = _member(merchant_a, add_member, TeamMember.Role.OWNER, "get-scoped-a@example.com")
    owner_b = _member(merchant_b, add_member, TeamMember.Role.OWNER, "get-scoped-b@example.com")
    make_key(owner_a)
    make_key(owner_b)
    client = session_client("get-scoped-a@example.com")
    resp = client.get(API_KEYS)
    assert len(resp.json()["results"]) == 1


def test_get_api_keys_limit_is_capped_at_100(merchant_a, add_member, session_client, make_key):
    owner = _member(merchant_a, add_member, TeamMember.Role.OWNER, "get-cap@example.com")
    for _ in range(101):
        make_key(owner)
    client = session_client("get-cap@example.com")
    resp = client.get(f"{API_KEYS}?limit=500")
    body = resp.json()
    assert len(body["results"]) == 100
    assert body["next_cursor"]


@pytest.mark.parametrize("role", [TeamMember.Role.MANAGER, TeamMember.Role.VIEWER])
def test_get_api_keys_as_manager_or_viewer_returns_403(merchant_a, add_member, session_client, role):
    _member(merchant_a, add_member, role, f"get-denied-{role}@example.com")
    client = session_client(f"get-denied-{role}@example.com")
    resp = client.get(API_KEYS)
    assert resp.status_code == 403


# --- DELETE /api-keys/{id} ---------------------------------------------


def test_delete_api_key_returns_204_and_revokes(merchant_a, add_member, session_client, make_key):
    owner = _member(merchant_a, add_member, TeamMember.Role.OWNER, "delete-basic@example.com")
    key, _ = make_key(owner)
    client = session_client("delete-basic@example.com")
    resp = client.delete(f"{API_KEYS}/{key.id}", HTTP_X_CSRFTOKEN=client.csrf)
    assert resp.status_code == 204
    with tenant_context(merchant_a.id), tenant_atomic():
        assert ApiKey.objects.get(id=key.id).is_active is False


def test_delete_api_key_is_idempotent(merchant_a, add_member, session_client, make_key):
    owner = _member(merchant_a, add_member, TeamMember.Role.OWNER, "delete-idempotent@example.com")
    key, _ = make_key(owner)
    client = session_client("delete-idempotent@example.com")
    first = client.delete(f"{API_KEYS}/{key.id}", HTTP_X_CSRFTOKEN=client.csrf)
    second = client.delete(f"{API_KEYS}/{key.id}", HTTP_X_CSRFTOKEN=client.csrf)
    assert first.status_code == second.status_code == 204
    with tenant_context(merchant_a.id), tenant_atomic():
        assert AuditLog.objects.filter(action="api_key.revoked").count() == 1


def test_delete_another_merchants_key_returns_404_never_403(merchant_a, merchant_b, add_member, session_client, make_key):
    owner_a = _member(merchant_a, add_member, TeamMember.Role.OWNER, "delete-cross-a@example.com")
    owner_b = _member(merchant_b, add_member, TeamMember.Role.OWNER, "delete-cross-b@example.com")
    key_b, _ = make_key(owner_b)
    client = session_client("delete-cross-a@example.com")
    resp = client.delete(f"{API_KEYS}/{key_b.id}", HTTP_X_CSRFTOKEN=client.csrf)
    assert resp.status_code == 404
    with tenant_context(merchant_b.id), tenant_atomic():
        assert ApiKey.objects.get(id=key_b.id).is_active is True


def test_delete_unknown_key_returns_404(merchant_a, add_member, session_client):
    _member(merchant_a, add_member, TeamMember.Role.OWNER, "delete-unknown@example.com")
    client = session_client("delete-unknown@example.com")
    resp = client.delete(f"{API_KEYS}/00000000-0000-0000-0000-000000000000", HTTP_X_CSRFTOKEN=client.csrf)
    assert resp.status_code == 404


@pytest.mark.parametrize("role", [TeamMember.Role.MANAGER, TeamMember.Role.VIEWER])
def test_delete_api_key_as_manager_or_viewer_returns_403(merchant_a, add_member, session_client, make_key, role):
    owner = _member(merchant_a, add_member, TeamMember.Role.OWNER, f"delete-denied-owner-{role}@example.com")
    key, _ = make_key(owner)
    _member(merchant_a, add_member, role, f"delete-denied-{role}@example.com")
    client = session_client(f"delete-denied-{role}@example.com")
    resp = client.delete(f"{API_KEYS}/{key.id}", HTTP_X_CSRFTOKEN=client.csrf)
    assert resp.status_code == 403
