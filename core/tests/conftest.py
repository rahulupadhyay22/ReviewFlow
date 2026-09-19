"""
Feature-local fixtures for core tenancy tests.

Throwaway tenant tables are declared *inside* this fixture (never at module
level) so they never register in the global Django app registry — otherwise
pytest's module-import-time collection would make
`config/tests/test_scaffold.py::test_no_missing_migrations` (Phase 00) see a
spurious migration. `isolate_apps("core")` gives them a private, temporary
app registry; the real `core.rls` SQL is applied by hand afterwards so the
actual helpers are what gets exercised.
"""
import uuid

import pytest
from django.db import connection, models
from django.test.utils import isolate_apps

from core.managers import TenantScopedManager
from core.rls import rls_direct, rls_via_parent


@pytest.fixture
def tenant_models():
    """A `Parent` (direct tenant table) and `Child` (transitive tenant table
    via `parent__merchant_id`), with the real RLS policies applied."""
    with isolate_apps("core"):

        class Parent(models.Model):
            id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
            merchant_id = models.UUIDField()

            objects = TenantScopedManager()

            class Meta:
                app_label = "core"

        class Child(models.Model):
            id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)

            objects = TenantScopedManager()
            tenant_field = "parent__merchant_id"

            class Meta:
                app_label = "core"

        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(Parent)
            schema_editor.create_model(Child)

        parent_table = Parent._meta.db_table
        child_table = Child._meta.db_table

        with connection.cursor() as cursor:
            cursor.execute(rls_direct(parent_table).sql)
            cursor.execute(rls_via_parent(child_table, "parent_id", parent_table).sql)

        yield Parent, Child
