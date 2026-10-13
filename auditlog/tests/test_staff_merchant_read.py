"""auditlog.services.staff_merchant_read -- the audited privileged per-merchant
read path (spec 16 D1; plan step 5).

transaction=True throughout (the test_platform_write.py convention): "the
audit row is committed before the read", "the row survives a failed read" and
"SET LOCAL does not outlive the block" only exist when the test is not itself
wrapped in a transaction."""
import uuid

import pytest
from django.db import DatabaseError, connection, transaction

from accounts.models import Merchant, TeamMember, User
from auditlog import services
from auditlog.models import AuditLog
from auditlog.services import AUDIT_STAFF_DATA_VIEWED, StaffAccessDenied, staff_merchant_read
from core.exceptions import TenantContextError
from core.tenancy import get_current_merchant_id, tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db(transaction=True)

RESOURCE = "test.resource"


@pytest.fixture
def staff():
    return User.objects.create_user("staff@example.com", "x", is_staff=True)


def audit_rows(merchant_id):
    with tenant_context(merchant_id), tenant_atomic():
        return list(
            AuditLog.objects.filter(action=AUDIT_STAFF_DATA_VIEWED).values(
                "actor_user_id", "target_type", "target_id", "metadata_json"
            )
        )


def raw(sql):
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


def test_one_audit_row_is_committed_before_the_body_runs(staff, member_a):
    seen = {}
    with staff_merchant_read(staff_user=staff, merchant_id=member_a.merchant_id, resource=RESOURCE):
        # A second connection sees the row now: it was committed, not just
        # written inside the read transaction.
        from django.db import connections

        other = connections.create_connection("default")
        try:
            with other.cursor() as cursor:
                cursor.execute("BEGIN")
                cursor.execute("SET LOCAL app.current_merchant_id = %s", [str(member_a.merchant_id)])
                cursor.execute("SELECT count(*) FROM auditlog_auditlog WHERE action = %s", [AUDIT_STAFF_DATA_VIEWED])
                seen["committed"] = cursor.fetchone()[0]
                cursor.execute("ROLLBACK")
        finally:
            other.close()
    assert seen["committed"] == 1
    assert audit_rows(member_a.merchant_id) == [
        {
            "actor_user_id": staff.id,
            "target_type": "accounts.merchant",
            "target_id": str(member_a.merchant_id),
            "metadata_json": {"resource": RESOURCE},
        }
    ]


def test_the_row_survives_a_read_that_raises(staff, member_a):
    with pytest.raises(RuntimeError, match="read failed"):
        with staff_merchant_read(staff_user=staff, merchant_id=member_a.merchant_id, resource=RESOURCE):
            raise RuntimeError("read failed")
    assert len(audit_rows(member_a.merchant_id)) == 1


def test_a_failed_audit_insert_means_the_body_never_runs(staff, member_a, monkeypatch):
    def boom(*args, **kwargs):
        raise DatabaseError("audit insert failed")

    monkeypatch.setattr(services.AuditLog.objects, "create", boom)
    ran = []
    with pytest.raises(DatabaseError):
        with staff_merchant_read(staff_user=staff, merchant_id=member_a.merchant_id, resource=RESOURCE):
            ran.append(True)
    assert ran == []
    assert get_current_merchant_id() is None


@pytest.mark.parametrize("extra", [{"is_staff": False}, {"is_staff": True, "is_active": False}])
def test_non_staff_and_inactive_users_are_refused_with_no_row(member_a, extra):
    user = User.objects.create_user("u@example.com", "x", **extra)
    with pytest.raises(StaffAccessDenied):
        with staff_merchant_read(staff_user=user, merchant_id=member_a.merchant_id, resource=RESOURCE):
            pytest.fail("body must not run")
    assert audit_rows(member_a.merchant_id) == []


def test_an_active_tenant_context_is_refused(staff, member_a, member_b):
    with tenant_context(member_b.merchant_id):
        with pytest.raises(TenantContextError):
            with staff_merchant_read(staff_user=staff, merchant_id=member_a.merchant_id, resource=RESOURCE):
                pytest.fail("body must not run")
    assert audit_rows(member_a.merchant_id) == []


def test_an_unknown_merchant_is_refused(staff):
    with pytest.raises(Merchant.DoesNotExist):
        with staff_merchant_read(staff_user=staff, merchant_id=uuid.uuid4(), resource=RESOURCE):
            pytest.fail("body must not run")


def test_inside_another_transaction_it_raises_before_audit_or_read(staff, member_a):
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            with staff_merchant_read(staff_user=staff, merchant_id=member_a.merchant_id, resource=RESOURCE):
                pytest.fail("body must not run")
    assert audit_rows(member_a.merchant_id) == []


def test_the_context_and_the_setting_are_cleared_after_success_and_after_an_exception(staff, member_a):
    with staff_merchant_read(staff_user=staff, merchant_id=member_a.merchant_id, resource=RESOURCE):
        assert get_current_merchant_id() == member_a.merchant_id
    assert get_current_merchant_id() is None
    assert raw("SELECT current_setting('app.current_merchant_id', true)")[0][0] in ("", None)
    # Pooled-connection safety: the next statement on this connection is
    # outside any tenant, so RLS shows it no tenant rows.
    assert raw("SELECT count(*) FROM accounts_teammember")[0][0] == 0
    assert raw("SELECT count(*) FROM events_integrationevent")[0][0] == 0

    with pytest.raises(ValueError):
        with staff_merchant_read(staff_user=staff, merchant_id=member_a.merchant_id, resource=RESOURCE):
            raise ValueError("boom")
    assert get_current_merchant_id() is None
    assert raw("SELECT current_setting('app.current_merchant_id', true)")[0][0] in ("", None)


def test_inside_the_block_orm_and_raw_sql_see_only_the_selected_merchant(staff, member_a, member_b):
    with staff_merchant_read(staff_user=staff, merchant_id=member_a.merchant_id, resource=RESOURCE):
        orm = set(TeamMember.objects.values_list("merchant_id", flat=True))
        rows = {r[0] for r in raw("SELECT merchant_id FROM accounts_teammember")}
        unscoped = {r[0] for r in raw(f"SELECT merchant_id FROM accounts_teammember WHERE merchant_id = '{member_b.merchant_id}'")}
    assert orm == {member_a.merchant_id}
    assert rows == {member_a.merchant_id}
    assert unscoped == set()  # RLS backstop: B is invisible even when asked for by id
