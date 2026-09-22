import uuid

import pytest
from django.db import Error, connection, transaction
from django.utils import timezone as dj_timezone

from auditlog.models import AuditLog
from auditlog.services import record
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db(transaction=True)


def _ids(sql):
    with connection.cursor() as cur:
        cur.execute(sql)
        return {str(r[0]) for r in cur.fetchall()}


def test_raw_sql_in_merchant_context_returns_only_that_merchants_audit_rows(member_a, member_b):
    with tenant_context(member_a.merchant_id):
        a_row = record("a.action")
    with tenant_context(member_b.merchant_id):
        b_row = record("b.action")
    with tenant_context(member_a.merchant_id), tenant_atomic():
        ids = _ids("SELECT id FROM auditlog_auditlog")
    assert ids == {str(a_row.id)}
    assert str(b_row.id) not in ids


def test_raw_sql_without_context_returns_no_audit_rows(member_a):
    with tenant_context(member_a.merchant_id):
        record("a.action")
    assert _ids("SELECT id FROM auditlog_auditlog") == set()


def test_insert_audit_row_for_other_merchant_fails_with_check(member_a, member_b):
    with tenant_context(member_a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            AuditLog.objects.create(merchant_id=member_b.merchant_id, action="forged")


def test_insert_audit_row_with_null_merchant_fails_with_check(member_a):
    with tenant_context(member_a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            AuditLog(merchant_id=None, action="platform.action", created_at=dj_timezone.now()).save(
                force_insert=True
            )


def test_null_merchant_rows_are_invisible_in_tenant_context(member_a):
    # Cannot be inserted under RLS; assert none are visible to a tenant either way.
    with tenant_context(member_a.merchant_id), tenant_atomic():
        assert not AuditLog.objects.filter(merchant__isnull=True).exists()
        assert _ids("SELECT id FROM auditlog_auditlog WHERE merchant_id IS NULL") == set()


def test_unknown_merchant_context_sees_nothing(member_a):
    with tenant_context(member_a.merchant_id):
        record("a.action")
    with tenant_context(uuid.uuid4()), tenant_atomic():
        assert _ids("SELECT id FROM auditlog_auditlog") == set()
