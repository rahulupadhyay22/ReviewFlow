"""apikeys/services.py unit tests (spec §"Services & background tasks",
Definition of done §"Services")."""
import hashlib
import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.utils import timezone

from accounts.models import Merchant, TeamMember
from apikeys import services
from apikeys.exceptions import ApiKeyNotFound, InvalidApiKey, InvalidScopes
from apikeys.models import ApiKey
from auditlog.models import AuditLog
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

KEY_RE = re.compile(r"^rf_live_[A-Za-z0-9_-]{32}$")


def _owner(merchant_a, add_member):
    return add_member(merchant_a, TeamMember.Role.OWNER, "svc-owner@example.com")


# --- create_api_key ---------------------------------------------------


def test_create_api_key_returns_valid_plaintext_and_stores_only_its_hash(merchant_a, add_member):
    owner = _owner(merchant_a, add_member)
    with tenant_context(merchant_a.id):
        key, raw = services.create_api_key(actor=owner, scopes=["sales:write"])
    assert KEY_RE.match(raw)
    assert key.key_hash == hashlib.sha256(raw.encode()).hexdigest()
    with tenant_context(merchant_a.id), tenant_atomic():
        stored = ApiKey.objects.get(id=key.id)
        # The plaintext appears in no column of the stored row.
        with connection.cursor() as cur:
            cur.execute("SELECT row_to_json(t)::text FROM apikeys_apikey t WHERE id = %s", [str(key.id)])
            row_json = cur.fetchone()[0]
    assert stored.key_hash != raw
    assert raw not in row_json


def test_create_api_key_writes_one_audit_row_without_plaintext_or_hash(merchant_a, add_member):
    owner = _owner(merchant_a, add_member)
    with tenant_context(merchant_a.id):
        key, raw = services.create_api_key(actor=owner, scopes=["sales:write", "reviews:read"])
        with tenant_atomic():
            rows = list(AuditLog.objects.filter(action="api_key.created"))
    assert len(rows) == 1
    assert rows[0].target_type == "apikeys.apikey"
    assert rows[0].target_id == str(key.id)
    metadata = rows[0].metadata_json
    assert metadata == {"scopes": ["sales:write", "reviews:read"]}
    assert raw not in str(metadata)
    assert key.key_hash not in str(metadata)


def test_create_api_key_never_logs_plaintext_or_hash(merchant_a, add_member, caplog):
    owner = _owner(merchant_a, add_member)
    with caplog.at_level("DEBUG"):
        with tenant_context(merchant_a.id):
            key, raw = services.create_api_key(actor=owner, scopes=["sales:write"])
    assert raw not in caplog.text
    assert key.key_hash not in caplog.text


@pytest.mark.parametrize(
    "scopes",
    [[], "sales:write", ["not_a_real_scope"], ["sales:write", "sales:write"], None],
)
def test_create_api_key_rejects_invalid_scopes(merchant_a, add_member, scopes):
    owner = _owner(merchant_a, add_member)
    with tenant_context(merchant_a.id):
        with pytest.raises(InvalidScopes):
            services.create_api_key(actor=owner, scopes=scopes)
        with tenant_atomic():
            assert ApiKey.objects.count() == 0


# --- list_api_keys / get_api_key ------------------------------------------


def test_list_api_keys_is_tenant_scoped(merchant_a, merchant_b, add_member):
    owner_a = _owner(merchant_a, add_member)
    owner_b = add_member(merchant_b, TeamMember.Role.OWNER, "svc-owner-b@example.com")
    with tenant_context(merchant_a.id):
        services.create_api_key(actor=owner_a, scopes=["sales:write"])
    with tenant_context(merchant_b.id):
        services.create_api_key(actor=owner_b, scopes=["sales:write"])
        assert services.list_api_keys().count() == 1


def test_get_api_key_raises_not_found_for_unknown_id(merchant_a):
    with tenant_context(merchant_a.id):
        with pytest.raises(ApiKeyNotFound):
            services.get_api_key("00000000-0000-0000-0000-000000000000")


def test_get_api_key_raises_not_found_for_another_merchants_key(merchant_a, merchant_b, add_member):
    owner_b = add_member(merchant_b, TeamMember.Role.OWNER, "svc-owner-b-get@example.com")
    with tenant_context(merchant_b.id):
        key_b, _ = services.create_api_key(actor=owner_b, scopes=["sales:write"])
    with tenant_context(merchant_a.id):
        with pytest.raises(ApiKeyNotFound):
            services.get_api_key(key_b.id)


# --- revoke_api_key ---------------------------------------------------


def test_revoke_api_key_sets_inactive_with_one_audit_row(merchant_a, add_member):
    owner = _owner(merchant_a, add_member)
    with tenant_context(merchant_a.id):
        key, _ = services.create_api_key(actor=owner, scopes=["sales:write"])
        services.revoke_api_key(actor=owner, api_key=key)
        with tenant_atomic():
            key.refresh_from_db()
            revoked_rows = AuditLog.objects.filter(action="api_key.revoked").count()
    assert key.is_active is False
    assert revoked_rows == 1


def test_revoke_api_key_twice_is_idempotent_with_no_second_audit_row(merchant_a, add_member):
    owner = _owner(merchant_a, add_member)
    with tenant_context(merchant_a.id):
        key, _ = services.create_api_key(actor=owner, scopes=["sales:write"])
        services.revoke_api_key(actor=owner, api_key=key)
        services.revoke_api_key(actor=owner, api_key=key)
        with tenant_atomic():
            revoked_rows = AuditLog.objects.filter(action="api_key.revoked").count()
    assert revoked_rows == 1


# --- authenticate_api_key --------------------------------------------


def test_authenticate_api_key_returns_the_key_for_a_valid_active_key(merchant_a, add_member):
    owner = _owner(merchant_a, add_member)
    with tenant_context(merchant_a.id):
        key, raw = services.create_api_key(actor=owner, scopes=["sales:write"])
    # Called with no tenant context: the lookup is pre-tenant.
    found = services.authenticate_api_key(raw)
    assert found.id == key.id
    assert found.merchant_id == merchant_a.id


def test_authenticate_api_key_rejects_malformed_string():
    with pytest.raises(InvalidApiKey):
        services.authenticate_api_key("not-a-key")


def test_authenticate_api_key_malformed_token_never_opens_the_lookup(monkeypatch):
    def _fail(key_hash):
        pytest.fail("api_key_lookup_atomic must not open for a malformed token")

    monkeypatch.setattr(services, "api_key_lookup_atomic", _fail)
    with pytest.raises(InvalidApiKey):
        services.authenticate_api_key("rf_live_short")


def test_authenticate_api_key_rejects_unknown_key():
    with pytest.raises(InvalidApiKey):
        services.authenticate_api_key("rf_live_" + "x" * 32)


def test_authenticate_api_key_rejects_revoked_key(merchant_a, add_member):
    owner = _owner(merchant_a, add_member)
    with tenant_context(merchant_a.id):
        key, raw = services.create_api_key(actor=owner, scopes=["sales:write"])
        services.revoke_api_key(actor=owner, api_key=key)
    with pytest.raises(InvalidApiKey):
        services.authenticate_api_key(raw)


def test_authenticate_api_key_rejects_key_of_suspended_merchant(merchant_a, add_member):
    owner = _owner(merchant_a, add_member)
    with tenant_context(merchant_a.id):
        key, raw = services.create_api_key(actor=owner, scopes=["sales:write"])
    with tenant_context(merchant_a.id), tenant_atomic():
        Merchant.objects.filter(id=merchant_a.id).update(status=Merchant.Status.SUSPENDED)
    with pytest.raises(InvalidApiKey):
        services.authenticate_api_key(raw)


def test_authenticate_api_key_rejects_key_of_deleted_merchant(merchant_a, add_member):
    owner = _owner(merchant_a, add_member)
    with tenant_context(merchant_a.id):
        key, raw = services.create_api_key(actor=owner, scopes=["sales:write"])
    with tenant_context(merchant_a.id), tenant_atomic():
        Merchant.objects.filter(id=merchant_a.id).update(status=Merchant.Status.DELETED)
    with pytest.raises(InvalidApiKey):
        services.authenticate_api_key(raw)


# --- touch_last_used ---------------------------------------------------


def test_touch_last_used_sets_last_used_at_when_null(merchant_a, add_member):
    owner = _owner(merchant_a, add_member)
    with tenant_context(merchant_a.id):
        key, _ = services.create_api_key(actor=owner, scopes=["sales:write"])
    assert key.last_used_at is None
    services.touch_last_used(key)
    with tenant_context(merchant_a.id), tenant_atomic():
        key.refresh_from_db()
    assert key.last_used_at is not None


def test_touch_last_used_does_not_change_within_60_seconds(merchant_a, add_member):
    owner = _owner(merchant_a, add_member)
    with tenant_context(merchant_a.id):
        key, _ = services.create_api_key(actor=owner, scopes=["sales:write"])
    services.touch_last_used(key)
    with tenant_context(merchant_a.id), tenant_atomic():
        key.refresh_from_db()
    first = key.last_used_at
    services.touch_last_used(key)
    with tenant_context(merchant_a.id), tenant_atomic():
        key.refresh_from_db()
    assert key.last_used_at == first


def test_touch_last_used_updates_after_60_seconds(merchant_a, add_member):
    owner = _owner(merchant_a, add_member)
    with tenant_context(merchant_a.id):
        key, _ = services.create_api_key(actor=owner, scopes=["sales:write"])
    services.touch_last_used(key)
    with tenant_context(merchant_a.id), tenant_atomic():
        key.refresh_from_db()
    first = key.last_used_at
    later = first + timedelta(seconds=61)
    with patch("apikeys.services.timezone.now", return_value=later):
        services.touch_last_used(key)
    with tenant_context(merchant_a.id), tenant_atomic():
        key.refresh_from_db()
    assert key.last_used_at == later
