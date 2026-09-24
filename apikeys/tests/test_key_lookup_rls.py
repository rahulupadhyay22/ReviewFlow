"""ApiKey model, migrations, and RLS (spec .claude/specs/05-public-api-keys.md
§"Models & database changes", Multi-Tenancy.md §"API key lookup". Mirrors
accounts/tests/test_rls.py's pattern for the self_membership policy, applied
here to the signed-off api_key_lookup policy."""
import uuid

import pytest
from django.contrib import admin
from django.db import Error, IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from apikeys.models import ApiKey
from core.exceptions import TenantContextError
from core.tenancy import api_key_lookup_atomic, tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db(transaction=True)


def _ids(sql, params=()):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return {str(r[0]) for r in cur.fetchall()}


def _setting(name):
    with connection.cursor() as cur:
        cur.execute("SELECT current_setting(%s, true)", [name])
        return cur.fetchone()[0]


def _make_key(owner):
    from apikeys import services

    with tenant_context(owner.merchant_id):
        return services.create_api_key(actor=owner, scopes=["sales:write"])


# --- Migrations -------------------------------------------------------


def test_apikeys_migrations_apply_and_reverse_cleanly():
    executor = MigrationExecutor(connection)
    leaf = executor.loader.graph.leaf_nodes("apikeys")
    try:
        executor.migrate([("apikeys", None)])
        with connection.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('apikeys_apikey')"
            )
            assert cur.fetchone()[0] is None
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf)
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass('apikeys_apikey')")
        assert cur.fetchone()[0] is not None


# --- RLS policy shape ---------------------------------------------------


def test_apikeys_apikey_rls_enabled_forced_with_exactly_two_policies():
    with connection.cursor() as cur:
        cur.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'apikeys_apikey'"
        )
        row_security, force_security = cur.fetchone()
        assert row_security is True
        assert force_security is True

        cur.execute(
            "SELECT policyname, cmd FROM pg_policies WHERE tablename = 'apikeys_apikey'"
        )
        policies = dict(cur.fetchall())
    assert policies == {"tenant_isolation": "ALL", "api_key_lookup": "SELECT"}


# --- tenant_isolation policy ---------------------------------------------


def test_raw_sql_in_merchant_context_returns_only_that_merchants_keys(merchant_a, merchant_b, add_member):
    from accounts.models import TeamMember

    owner_a = add_member(merchant_a, TeamMember.Role.OWNER, "owner-a-rls@example.com")
    owner_b = add_member(merchant_b, TeamMember.Role.OWNER, "owner-b-rls@example.com")
    key_a, _ = _make_key(owner_a)
    key_b, _ = _make_key(owner_b)

    with tenant_context(merchant_a.id), tenant_atomic():
        ids = _ids("SELECT id FROM apikeys_apikey")
    assert ids == {str(key_a.id)}
    assert str(key_b.id) not in ids


def test_raw_sql_with_no_context_returns_zero_rows(merchant_a, add_member):
    from accounts.models import TeamMember

    owner_a = add_member(merchant_a, TeamMember.Role.OWNER, "owner-a-nocontext@example.com")
    _make_key(owner_a)
    assert _ids("SELECT id FROM apikeys_apikey") == set()


def test_insert_apikey_for_other_merchant_fails_with_check(merchant_a, merchant_b):
    with tenant_context(merchant_a.id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            ApiKey(
                merchant_id=merchant_b.id,
                key_hash="a" * 64,
                scopes_json=["sales:write"],
            ).save(force_insert=True)


# --- api_key_lookup policy ------------------------------------------------


def test_lookup_atomic_select_returns_only_the_matching_hash_row(merchant_a, merchant_b, add_member):
    from accounts.models import TeamMember

    owner_a = add_member(merchant_a, TeamMember.Role.OWNER, "owner-a-lookup@example.com")
    owner_b = add_member(merchant_b, TeamMember.Role.OWNER, "owner-b-lookup@example.com")
    key_a, _ = _make_key(owner_a)
    key_b, _ = _make_key(owner_b)

    with api_key_lookup_atomic(key_a.key_hash):
        ids = _ids("SELECT id FROM apikeys_apikey")
    assert ids == {str(key_a.id)}
    assert str(key_b.id) not in ids


def test_lookup_atomic_update_and_delete_affect_zero_rows(merchant_a, add_member):
    from accounts.models import TeamMember

    owner_a = add_member(merchant_a, TeamMember.Role.OWNER, "owner-a-nowrite@example.com")
    key_a, _ = _make_key(owner_a)

    with api_key_lookup_atomic(key_a.key_hash):
        with connection.cursor() as cur:
            cur.execute("UPDATE apikeys_apikey SET is_active = false WHERE id = %s", [str(key_a.id)])
            assert cur.rowcount == 0
            cur.execute("DELETE FROM apikeys_apikey WHERE id = %s", [str(key_a.id)])
            assert cur.rowcount == 0

    with tenant_context(merchant_a.id), tenant_atomic():
        assert ApiKey.objects.get(id=key_a.id).is_active is True


def test_lookup_atomic_setting_is_reset_after_transaction(merchant_a, add_member):
    from accounts.models import TeamMember

    owner_a = add_member(merchant_a, TeamMember.Role.OWNER, "owner-a-reset@example.com")
    key_a, _ = _make_key(owner_a)
    with api_key_lookup_atomic(key_a.key_hash):
        assert _setting("app.current_api_key_hash") == key_a.key_hash
    assert _setting("app.current_api_key_hash") in (None, "")


def test_lookup_atomic_setting_is_reset_after_failed_transaction():
    with pytest.raises(RuntimeError):
        with api_key_lookup_atomic("a" * 64):
            raise RuntimeError("boom")
    assert _setting("app.current_api_key_hash") in (None, "")


def test_lookup_atomic_with_a_wrong_hash_sees_no_rows(merchant_a, add_member):
    from accounts.models import TeamMember

    owner_a = add_member(merchant_a, TeamMember.Role.OWNER, "owner-a-wronghash@example.com")
    _make_key(owner_a)
    with api_key_lookup_atomic("b" * 64):
        assert _ids("SELECT id FROM apikeys_apikey") == set()


def test_lookup_atomic_insert_is_rejected(merchant_a, add_member):
    """No write policy is keyed on app.current_api_key_hash."""
    from accounts.models import TeamMember

    owner_a = add_member(merchant_a, TeamMember.Role.OWNER, "owner-a-noinsert@example.com")
    key_a, _ = _make_key(owner_a)
    with api_key_lookup_atomic(key_a.key_hash):
        with pytest.raises(Error), transaction.atomic():
            ApiKey(
                merchant_id=merchant_a.id, key_hash="d" * 64, scopes_json=["sales:write"]
            ).save(force_insert=True)


def test_lookup_atomic_raises_tenant_context_error_inside_active_merchant_context(merchant_a):
    with tenant_context(merchant_a.id):
        with pytest.raises(TenantContextError):
            with api_key_lookup_atomic("a" * 64):
                pass


def test_lookup_atomic_inside_tenant_atomic_raises_and_sets_nothing(merchant_a):
    with tenant_context(merchant_a.id), tenant_atomic():
        with pytest.raises(TenantContextError):
            with api_key_lookup_atomic("a" * 64):
                pass
        assert _setting("app.current_api_key_hash") in (None, "")


@pytest.mark.parametrize(
    "bad_hash",
    [
        "not-a-hex-hash",
        "a" * 63,
        "a" * 65,
        "A" * 64,  # uppercase hex is not the canonical digest
        "x'; DROP TABLE apikeys_apikey; --",  # SET LOCAL literal injection
        "",
        None,
    ],
)
def test_lookup_atomic_raises_valueerror_for_a_bad_hash(bad_hash):
    with pytest.raises(ValueError):
        with api_key_lookup_atomic(bad_hash):
            pass


def test_lookup_atomic_raises_runtimeerror_when_nested_in_another_atomic():
    with transaction.atomic():
        with pytest.raises(RuntimeError):
            with api_key_lookup_atomic("a" * 64):
                pass
        assert _setting("app.current_api_key_hash") in (None, "")


def test_for_lookup_hash_raises_outside_lookup_atomic(merchant_a, add_member):
    from accounts.models import TeamMember

    owner_a = add_member(merchant_a, TeamMember.Role.OWNER, "owner-a-outside@example.com")
    key_a, _ = _make_key(owner_a)
    with pytest.raises(TenantContextError):
        ApiKey.objects.for_lookup_hash(key_a.key_hash)


def test_for_lookup_hash_raises_for_a_different_hash(merchant_a, add_member):
    from accounts.models import TeamMember

    owner_a = add_member(merchant_a, TeamMember.Role.OWNER, "owner-a-diffhash@example.com")
    key_a, _ = _make_key(owner_a)
    with api_key_lookup_atomic("b" * 64):
        with pytest.raises(TenantContextError):
            ApiKey.objects.for_lookup_hash(key_a.key_hash)


def test_for_lookup_hash_returns_only_that_hashs_row(merchant_a, merchant_b, add_member):
    from accounts.models import TeamMember

    owner_a = add_member(merchant_a, TeamMember.Role.OWNER, "owner-a-orm-lookup@example.com")
    owner_b = add_member(merchant_b, TeamMember.Role.OWNER, "owner-b-orm-lookup@example.com")
    key_a, _ = _make_key(owner_a)
    _make_key(owner_b)
    with api_key_lookup_atomic(key_a.key_hash):
        rows = list(ApiKey.objects.for_lookup_hash(key_a.key_hash))
    assert [r.id for r in rows] == [key_a.id]


def test_apikey_objects_all_without_tenant_context_raises():
    with pytest.raises(TenantContextError):
        list(ApiKey.objects.all())


# --- Constraints ----------------------------------------------------------


def test_duplicate_key_hash_raises_integrity_error(merchant_a, merchant_b):
    shared_hash = "c" * 64
    with tenant_context(merchant_a.id), tenant_atomic():
        ApiKey.objects.create(merchant_id=merchant_a.id, key_hash=shared_hash, scopes_json=["sales:write"])
    with tenant_context(merchant_b.id), tenant_atomic():
        with pytest.raises(IntegrityError), transaction.atomic():
            ApiKey.objects.create(merchant_id=merchant_b.id, key_hash=shared_hash, scopes_json=["sales:write"])


def test_apikey_has_only_the_data_dictionary_fields():
    """No name/label/prefix field (Data-Dictionary.md §ApiKey)."""
    assert {f.name for f in ApiKey._meta.get_fields()} == {
        "id",
        "created_at",
        "updated_at",
        "merchant",
        "key_hash",
        "scopes_json",
        "is_active",
        "last_used_at",
    }


# --- Admin (D-A) ------------------------------------------------------


def test_apikey_is_not_registered_in_django_admin():
    assert admin.site.is_registered(ApiKey) is False
