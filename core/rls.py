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


CURRENT_USER = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"


def rls_select_by_user(table, user_column="user_id"):
    """SELECT-only self_membership policy (login lookup, see
    core.tenancy.user_lookup_atomic). Run after rls_direct(), which enables
    and forces RLS. Never add a write policy keyed on app.current_user_id.
    """
    return migrations.RunSQL(
        sql=(
            f"CREATE POLICY self_membership ON {table} FOR SELECT "
            f"USING ({user_column} = {CURRENT_USER});"
        ),
        reverse_sql=f"DROP POLICY self_membership ON {table};",
    )


CURRENT_API_KEY_HASH = "NULLIF(current_setting('app.current_api_key_hash', true), '')"


def rls_select_by_key_hash(table, column="key_hash"):
    """SELECT-only api_key_lookup policy (pre-tenant public-API key lookup,
    see core.tenancy.api_key_lookup_atomic). Run after rls_direct(), which
    enables and forces RLS. Never add a write policy keyed on
    app.current_api_key_hash. Signed off 2026-09-24
    (Multi-Tenancy.md §"API key lookup").
    """
    return migrations.RunSQL(
        sql=(
            f"CREATE POLICY api_key_lookup ON {table} FOR SELECT "
            f"USING ({column} = {CURRENT_API_KEY_HASH});"
        ),
        reverse_sql=f"DROP POLICY api_key_lookup ON {table};",
    )


def rls_select_by_integration_id(table, column="id"):
    """SELECT-only integration_lookup policy (pre-tenant provider-webhook
    Integration lookup, see core.tenancy.integration_lookup_atomic). Run
    after rls_direct(), which enables and forces RLS. Never add a write
    policy keyed on app.current_integration_id. The id is not secret (it
    appears in the webhook URL); the lookup exists only so a receiver can
    identify its Integration before any merchant context exists
    (Phase 06 spec Decision 1 -- LOCKED DECISION CHANGE, approved).
    """
    current_integration = "NULLIF(current_setting('app.current_integration_id', true), '')::uuid"
    return migrations.RunSQL(
        sql=(
            f"CREATE POLICY integration_lookup ON {table} FOR SELECT "
            f"USING ({column} = {current_integration});"
        ),
        reverse_sql=f"DROP POLICY integration_lookup ON {table};",
    )


CURRENT_BILLING_REF = "NULLIF(current_setting('app.current_billing_ref', true), '')"


def _billing_ref_predicate(columns):
    return " OR ".join(f"{column} = {CURRENT_BILLING_REF}" for column in columns)


def rls_select_by_billing_ref(table, column="payment_provider_ref", columns=None):
    """SELECT-only billing_ref_lookup policy (pre-tenant Razorpay webhook
    Subscription lookup, see core.tenancy.billing_ref_lookup_atomic). Run
    after rls_direct(), which enables and forces RLS. Never add a write
    policy keyed on app.current_billing_ref. Signed off 2026-09-30
    (spec 07 Change 1 -- LOCKED DECISION CHANGE).

    `columns` (spec 07-plan-change-replacement) matches any of several ref
    columns; it defaults to `(column,)`, so migration 0002 is unchanged.
    """
    predicate = _billing_ref_predicate(columns or (column,))
    return migrations.RunSQL(
        sql=f"CREATE POLICY billing_ref_lookup ON {table} FOR SELECT USING ({predicate});",
        reverse_sql=f"DROP POLICY billing_ref_lookup ON {table};",
    )


def rls_replace_select_by_billing_ref(table, *, old_columns, new_columns):
    """Replaces the SELECT-only billing_ref_lookup policy (DROP then CREATE, in
    the migration's own transaction) so it matches `new_columns`; the reverse
    restores `old_columns`. Still SELECT-only: no write policy is keyed on
    app.current_billing_ref (spec 07-plan-change-replacement, LOCKED DECISION
    CHANGE, approved 2026-10-05; formal sign-off at PR time).
    """
    drop = f"DROP POLICY billing_ref_lookup ON {table};"

    def create(columns):
        return (
            f"CREATE POLICY billing_ref_lookup ON {table} FOR SELECT "
            f"USING ({_billing_ref_predicate(columns)});"
        )

    return migrations.RunSQL(
        sql=[drop, create(new_columns)],
        reverse_sql=[drop, create(old_columns)],
    )


def rls_via_parent(table, fk_column, parent_table):
    """Policy for tables reaching the tenant through a parent FK.

    The parent is read under its own RLS policy, so this chains to any depth.
    """
    return _tenant_policy(
        table, f"EXISTS (SELECT 1 FROM {parent_table} p WHERE p.id = {table}.{fk_column})"
    )
