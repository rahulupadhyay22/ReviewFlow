"""
Tests for core.rls (Postgres RLS policy helpers) and the non-superuser,
non-BYPASSRLS application role, per .claude/specs/01-tenant-core.md,
docs/02-architecture/Multi-Tenancy.md (§No Standing Privileged Role,
Required Tests 2), and docs/10-development/Testing-Strategy.md
(§Final Consistency Tests Added → RLS).
"""
import uuid
from pathlib import Path

import pytest
from django.conf import settings
from django.db import DatabaseError, connection, models, transaction
from django.test.utils import isolate_apps

from core.managers import TenantScopedManager
from core.rls import rls_direct, rls_via_parent
from core.tenancy import tenant_atomic, tenant_context

BASE_DIR = Path(settings.BASE_DIR)

# ---------------------------------------------------------------------------
# No standing privileged role
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_application_role_is_not_superuser_and_not_bypassrls():
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        rolsuper, rolbypassrls = cursor.fetchone()

    assert (rolsuper, rolbypassrls) == (False, False)


# ---------------------------------------------------------------------------
# RLS backstop (Multi-Tenancy.md Required Test 2): raw SQL that bypasses
# TenantScopedManager entirely.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_rls_direct_table_raw_query_returns_only_current_merchants_rows(tenant_models):
    Parent, _Child = tenant_models
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()
    table = Parent._meta.db_table

    with tenant_context(merchant_a), tenant_atomic():
        Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_a)
    with tenant_context(merchant_b), tenant_atomic():
        Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_b)

    with tenant_context(merchant_a), tenant_atomic():
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT merchant_id FROM {table}")
            rows = cursor.fetchall()

    assert rows == [(merchant_a,)]


@pytest.mark.django_db
def test_rls_via_parent_table_raw_query_returns_only_current_merchants_rows(tenant_models):
    Parent, Child = tenant_models
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()
    child_table = Child._meta.db_table

    with tenant_context(merchant_a), tenant_atomic():
        parent_a = Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_a)
        child_a = Child.objects.create(id=uuid.uuid4(), parent=parent_a)
    with tenant_context(merchant_b), tenant_atomic():
        parent_b = Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_b)
        Child.objects.create(id=uuid.uuid4(), parent=parent_b)

    with tenant_context(merchant_a), tenant_atomic():
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT id FROM {child_table}")
            rows = cursor.fetchall()

    assert rows == [(child_a.id,)]


@pytest.mark.django_db
def test_rls_raw_query_without_context_returns_zero_rows(tenant_models):
    Parent, Child = tenant_models
    merchant_a = uuid.uuid4()

    with tenant_context(merchant_a), tenant_atomic():
        parent_a = Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_a)
        Child.objects.create(id=uuid.uuid4(), parent=parent_a)

    # SET LOCAL lasts until the enclosing transaction ends, not the savepoint:
    # the released tenant_atomic() savepoint above leaves it set for the rest
    # of the outer test transaction. Clear it to simulate "no tenant context".
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.current_merchant_id', '', true)")
        cursor.execute(f"SELECT * FROM {Parent._meta.db_table}")
        parent_rows = cursor.fetchall()
        cursor.execute(f"SELECT * FROM {Child._meta.db_table}")
        child_rows = cursor.fetchall()

    assert parent_rows == []
    assert child_rows == []


# ---------------------------------------------------------------------------
# RLS WITH CHECK (fails closed on mismatched writes)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_rls_with_check_rejects_insert_with_mismatched_merchant_id(tenant_models):
    Parent, _Child = tenant_models
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()

    with tenant_context(merchant_a), tenant_atomic():
        with pytest.raises(DatabaseError):
            # Nested atomic() so the failed statement doesn't poison the
            # outer tenant_atomic()/test transaction.
            with transaction.atomic():
                Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_b)


@pytest.mark.django_db
def test_rls_with_check_rejects_child_row_pointing_at_another_merchants_parent(tenant_models):
    Parent, Child = tenant_models
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()

    with tenant_context(merchant_b), tenant_atomic():
        parent_b = Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_b)

    with tenant_context(merchant_a), tenant_atomic():
        with pytest.raises(DatabaseError):
            with transaction.atomic():
                Child.objects.create(id=uuid.uuid4(), parent=parent_b)


@pytest.mark.django_db
def test_rls_with_check_rejects_update_reassigning_merchant_id(tenant_models):
    # A's own row passes USING, so only WITH CHECK can stop it moving to B.
    Parent, _Child = tenant_models
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()

    with tenant_context(merchant_a), tenant_atomic():
        parent_a = Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_a)
        with pytest.raises(DatabaseError, match="row-level security policy"):
            with transaction.atomic():
                Parent.objects.filter(id=parent_a.id).update(merchant_id=merchant_b)

        assert Parent.objects.get(id=parent_a.id).merchant_id == merchant_a


# ---------------------------------------------------------------------------
# Reverse SQL
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_rls_direct_reverse_sql_removes_policy_and_disables_rls(tenant_models):
    Parent, _Child = tenant_models
    table = Parent._meta.db_table
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()

    with tenant_context(merchant_a), tenant_atomic():
        Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_a)
    with tenant_context(merchant_b), tenant_atomic():
        Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_b)

    with connection.cursor() as cursor:
        cursor.execute(rls_direct(table).reverse_sql)
        cursor.execute("SELECT set_config('app.current_merchant_id', '', true)")
        cursor.execute(f"SELECT merchant_id FROM {table}")
        rows = {row[0] for row in cursor.fetchall()}

    assert rows == {merchant_a, merchant_b}


@pytest.mark.django_db
def test_rls_via_parent_reverse_sql_removes_policy_and_disables_rls(tenant_models):
    Parent, Child = tenant_models
    child_table = Child._meta.db_table
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()

    with tenant_context(merchant_a), tenant_atomic():
        parent_a = Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_a)
        child_a = Child.objects.create(id=uuid.uuid4(), parent=parent_a)
    with tenant_context(merchant_b), tenant_atomic():
        parent_b = Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_b)
        child_b = Child.objects.create(id=uuid.uuid4(), parent=parent_b)

    with connection.cursor() as cursor:
        # Fire the deferred FK checks queued by the inserts; ALTER TABLE is refused while they're pending.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(rls_via_parent(child_table, "parent_id", Parent._meta.db_table).reverse_sql)
        cursor.execute("SELECT set_config('app.current_merchant_id', '', true)")
        cursor.execute(f"SELECT id FROM {child_table}")
        rows = {row[0] for row in cursor.fetchall()}

    assert rows == {child_a.id, child_b.id}


# ---------------------------------------------------------------------------
# Pooled connection / SET LOCAL (Testing-Strategy.md Final Consistency Tests
# Added → RLS): a committed tenant_atomic() must not leak its merchant
# context to a later transaction on the same (pooled) connection.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_pooled_connection_does_not_retain_merchant_context_after_commit():
    with isolate_apps("core"):

        class PooledParent(models.Model):
            id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
            merchant_id = models.UUIDField()

            objects = TenantScopedManager()

            class Meta:
                app_label = "core"

        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(PooledParent)

        table = PooledParent._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(rls_direct(table).sql)

        try:
            merchant_a = uuid.uuid4()
            with tenant_context(merchant_a), tenant_atomic():
                PooledParent.objects.create(id=uuid.uuid4(), merchant_id=merchant_a)
            # tenant_atomic()'s transaction.atomic() has now actually
            # committed (transaction=True, no surrounding test transaction).

            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('app.current_merchant_id', true)")
                setting_after_commit = cursor.fetchone()[0]

            # A later transaction with no tenant context sees zero rows.
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT * FROM {table}")
                rows = cursor.fetchall()
        finally:
            with connection.schema_editor() as schema_editor:
                schema_editor.delete_model(PooledParent)

    assert setting_after_commit == ""
    assert rows == []


# ---------------------------------------------------------------------------
# Database role / env configuration (Definition of done)
# ---------------------------------------------------------------------------


def test_env_example_database_url_uses_reviewflow_app_role():
    text = (BASE_DIR / ".env.example").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip().startswith("DATABASE_URL")]
    assert lines, "DATABASE_URL not found in .env.example"
    assert "reviewflow_app" in lines[0]


def test_development_setup_doc_uses_reviewflow_app_role():
    text = (BASE_DIR / "docs" / "10-development" / "Development-Setup.md").read_text(
        encoding="utf-8"
    )
    assert "reviewflow_app" in text
