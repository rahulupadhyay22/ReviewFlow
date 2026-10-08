"""Plain helpers shared by the spec-08 test modules (whatsapp, core, auditlog).

`rls_off` exists because the app role owns its tables with FORCE ROW LEVEL
SECURITY: some rows (platform audit rows, constraint-only checks) are
deliberately invisible or unreachable through RLS, so a test that must observe
them lifts FORCE for the duration of one block, inside the test transaction,
and restores it. Test-only; never used on a code path under test.
"""
import json
from contextlib import contextmanager

from django.db import connection, transaction

SHARED_PHONE_NUMBER_ID = "109999999999001"
SHARED_WABA_ID = "TEST-WABA-ID"
APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
ACCESS_TOKEN = "test-platform-token"
CUSTOMER_PHONE = "+919999990001"


@contextmanager
def rls_off(*tables):
    # One transaction: if the body fails, the DDL rolls back and FORCE is
    # restored; the lifted state is never visible to another connection.
    with transaction.atomic():
        with connection.cursor() as cursor:
            # Pending deferred FK checks would make ALTER TABLE fail.
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for table in tables:
                cursor.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        yield
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for table in tables:
                cursor.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")


def platform_audit_rows() -> list[dict]:
    """Every merchant_id IS NULL audit row, read past RLS (which hides them from
    every tenant context by design)."""
    with rls_off("auditlog_auditlog"):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT action, actor_user_id, target_type, target_id, metadata_json::text "
                "FROM auditlog_auditlog WHERE merchant_id IS NULL ORDER BY created_at"
            )
            rows = cursor.fetchall()
    return [
        {
            "action": action,
            "actor_user_id": actor,
            "target_type": target_type,
            "target_id": target_id,
            "metadata": json.loads(metadata) if metadata else None,
        }
        for action, actor, target_type, target_id, metadata in rows
    ]


def clear_platform_write_setting():
    """In a non-transactional test the SET LOCAL of a released savepoint lives
    on until the test transaction ends; clear it so RLS assertions that follow
    see a plain tenant/no-context session."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.platform_write', '', true)")
