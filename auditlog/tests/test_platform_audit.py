"""auditlog_auditlog platform_insert policy and record_platform (spec 08
Change 1, Definition of done "Change 1, audit table"). Platform rows are
invisible to every tenant by design, so the tests read them through
`platform_audit_rows` (FORCE RLS lifted for one block, test only).

transaction=True: SET LOCAL app.platform_write must end with its own
outermost transaction, as in production."""
import pytest
from django.db import DatabaseError, connection, transaction

from accounts.models import User
from auditlog.models import AuditLog
from auditlog.services import record, record_platform
from core.tenancy import platform_write_atomic, tenant_atomic, tenant_context
from whatsapp.tests.helpers import platform_audit_rows

pytestmark = pytest.mark.django_db(transaction=True)

SCOPE = "whatsapp_shared_pool"


def raw(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount


def test_auditlog_policies_are_tenant_isolation_all_plus_platform_insert():
    with connection.cursor() as cursor:
        cursor.execute("SELECT policyname, cmd FROM pg_policies WHERE tablename = 'auditlog_auditlog'")
        rows = cursor.fetchall()

    assert sorted(rows) == [("platform_insert", "INSERT"), ("tenant_isolation", "ALL")]


def test_a_null_merchant_audit_insert_fails_outside_platform_write_atomic():
    with pytest.raises(DatabaseError), transaction.atomic():
        AuditLog._base_manager.create(merchant=None, action="forged.platform")

    assert platform_audit_rows() == []


def test_a_null_merchant_audit_insert_fails_inside_a_tenant_context(member_a):
    with tenant_context(member_a.merchant_id), tenant_atomic():
        with pytest.raises(DatabaseError), transaction.atomic():
            AuditLog._base_manager.create(merchant=None, action="forged.platform")

    assert platform_audit_rows() == []


def test_record_platform_outside_the_block_raises_and_writes_nothing():
    with pytest.raises(Exception):
        record_platform("whatsapp.shared_pool_configured", metadata={"status": "ACTIVE"})

    assert platform_audit_rows() == []


def test_record_platform_writes_one_row_with_a_null_merchant_and_no_membership_check_for_the_actor():
    staff = User.objects.create_user("staff@example.com", "Tr1cky-Horse-Battery-Staple!")

    with platform_write_atomic(SCOPE):
        record_platform("whatsapp.shared_pool_configured", actor=staff, metadata={"status": "ACTIVE"})

    [row] = platform_audit_rows()
    assert row["action"] == "whatsapp.shared_pool_configured"
    assert row["actor_user_id"] == staff.id
    assert row["metadata"] == {"status": "ACTIVE"}


def test_the_platform_insert_policy_accepts_only_null_merchant_rows(member_a):
    with platform_write_atomic(SCOPE):
        with pytest.raises(DatabaseError), transaction.atomic():
            AuditLog._base_manager.create(merchant=member_a.merchant, action="forged.tenant.row")

    assert not AuditLog._base_manager.filter(action="forged.tenant.row").exists()


def test_a_platform_row_cannot_be_read_updated_or_deleted_by_a_tenant_context(member_a):
    with platform_write_atomic(SCOPE):
        record_platform("whatsapp.shared_pool_configured", metadata={"status": "ACTIVE"})

    with tenant_context(member_a.merchant_id), tenant_atomic():
        assert not AuditLog.objects.filter(action="whatsapp.shared_pool_configured").exists()
        assert raw("SELECT 1 FROM auditlog_auditlog WHERE merchant_id IS NULL") == 0
        assert raw("UPDATE auditlog_auditlog SET action = 'tampered' WHERE merchant_id IS NULL") == 0
        assert raw("DELETE FROM auditlog_auditlog WHERE merchant_id IS NULL") == 0

    assert [r["action"] for r in platform_audit_rows()] == ["whatsapp.shared_pool_configured"]


def test_no_context_cannot_update_or_delete_a_platform_row_even_inside_the_block():
    with platform_write_atomic(SCOPE):
        record_platform("whatsapp.shared_pool_configured", metadata={"status": "ACTIVE"})

    with platform_write_atomic(SCOPE):
        assert raw("UPDATE auditlog_auditlog SET action = 'tampered' WHERE merchant_id IS NULL") == 0
        assert raw("DELETE FROM auditlog_auditlog WHERE merchant_id IS NULL") == 0

    assert [r["action"] for r in platform_audit_rows()] == ["whatsapp.shared_pool_configured"]


def test_the_existing_record_behaviour_is_unchanged(member_a):
    with tenant_context(member_a.merchant_id):
        row = record("a.action", actor=member_a.user, metadata={"k": "v"})

    assert row.merchant_id == member_a.merchant_id
    with tenant_context(member_a.merchant_id), tenant_atomic():
        assert AuditLog.objects.filter(action="a.action").count() == 1


def test_record_still_requires_a_tenant_context():
    with pytest.raises(Exception):
        record("a.action")
