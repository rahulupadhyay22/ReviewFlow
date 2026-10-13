"""The staff_auth platform-write scope (spec 16 LC-1, approved 2026-10-13;
plan step 3b).

- It can write a platform audit row, but it can never insert, update or delete
  the shared WhatsApp sender row: those policies require the literal
  'whatsapp_shared_pool'.
- Exactly two call sites may open it.

transaction=True, like test_platform_write.py: the properties under test exist
only when the test is not itself wrapped in a transaction."""
import ast
from pathlib import Path

import pytest
from django.conf import settings
from django.db import DatabaseError, connection, transaction

from auditlog.services import record_platform
from core.tenancy import platform_write_atomic
from whatsapp.models import WhatsAppAccount
from whatsapp.tests.helpers import platform_audit_rows

pytestmark = pytest.mark.django_db(transaction=True)

SCOPE = "staff_auth"


def _create_shared(phone="SA-TEST-PHONE"):
    return WhatsAppAccount._base_manager.create(
        merchant=None,
        sender_type="SHARED_POOL",
        provider="meta_cloud",
        phone_number_id=phone,
        business_account_id="SA-TEST-WABA",
        status="ACTIVE",
    )


def _raw(sql):
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return cursor.rowcount


def test_staff_auth_can_write_a_platform_audit_row():
    with platform_write_atomic(SCOPE):
        record_platform("user.totp_enabled")
    assert [r["action"] for r in platform_audit_rows()] == ["user.totp_enabled"]


def test_staff_auth_cannot_insert_the_shared_whatsapp_row():
    with platform_write_atomic(SCOPE):
        with pytest.raises(DatabaseError), transaction.atomic():
            _create_shared()
    assert WhatsAppAccount.objects.shared_pool().count() == 0


def test_staff_auth_cannot_update_or_delete_the_shared_whatsapp_row():
    with platform_write_atomic("whatsapp_shared_pool"):
        _create_shared()

    with platform_write_atomic(SCOPE):
        assert _raw("UPDATE whatsapp_whatsappaccount SET status = 'SUSPENDED' WHERE sender_type = 'SHARED_POOL'") == 0
        assert _raw("DELETE FROM whatsapp_whatsappaccount WHERE sender_type = 'SHARED_POOL'") == 0

    assert WhatsAppAccount.objects.shared_pool().get().status == "ACTIVE"


def _staff_auth_calls(tree):
    """(enclosing function, call) for every platform_write_atomic(...) call
    whose scope argument is "staff_auth" or the STAFF_AUTH_SCOPE name, and every
    call whose scope is not a plain literal (which could hide staff_auth)."""
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "platform_write_atomic"):
                continue
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and arg.value != "staff_auth":
                continue
            yield func.name, ast.unparse(arg) if arg is not None else ""


def test_only_the_two_approved_functions_open_the_staff_auth_scope():
    """LC-1: confirm_staff_totp_setup and the recovery branch of
    verify_staff_admin_login, both in accounts/services.py. Comments and
    docstrings do not count; tests are excluded (they exercise the scope
    directly). The core.tenancy definition itself takes `scope` as a
    parameter and is not a call."""
    base = Path(settings.BASE_DIR)
    hits = set()
    for path in base.rglob("*.py"):
        rel = path.relative_to(base).as_posix()
        if rel.startswith((".venv/", "venv/")) or "/tests/" in f"/{rel}":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits.update((rel, func, arg) for func, arg in _staff_auth_calls(tree))
    assert hits == {
        ("accounts/services.py", "confirm_staff_totp_setup", "STAFF_AUTH_SCOPE"),
        ("accounts/services.py", "verify_staff_admin_login", "STAFF_AUTH_SCOPE"),
    }
