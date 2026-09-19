"""
RLS policy pattern for tenant tables (FINAL-ARCHITECTURE-REVIEW.md §8).
Use in each tenant table's migration. Table/column names come from
migration code only, never from user input.
"""
from django.db import migrations

# NULLIF: on a pooled connection the setting reads '' (not NULL) after the
# transaction that set it ends; ''::uuid would error instead of matching nothing.
CURRENT_MERCHANT = "NULLIF(current_setting('app.current_merchant_id', true), '')::uuid"


def _tenant_policy(table, predicate):
    return migrations.RunSQL(
        sql=(
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; "
            # FORCE: the app role owns its tables, and owners skip RLS otherwise.
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY; "
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate});"
        ),
        reverse_sql=(
            f"DROP POLICY tenant_isolation ON {table}; "
            f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY; "
            f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;"
        ),
    )


def rls_direct(table):
    """Policy for tables with their own merchant_id column."""
    return _tenant_policy(table, f"merchant_id = {CURRENT_MERCHANT}")


def rls_via_parent(table, fk_column, parent_table):
    """Policy for tables reaching the tenant through a parent FK.

    The parent is read under its own RLS policy, so this chains to any depth.
    """
    return _tenant_policy(
        table, f"EXISTS (SELECT 1 FROM {parent_table} p WHERE p.id = {table}.{fk_column})"
    )
