import pytest

from auditlog.models import AuditLog
from auditlog.services import ActorNotMember, record
from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db


def test_record_writes_row_with_current_merchant_target_label_and_id(member_a):
    with tenant_context(member_a.merchant_id):
        row = record(
            "team_member.invited",
            actor=member_a.user,
            target=member_a,
            metadata={"role": "VIEWER"},
        )
    assert row.merchant_id == member_a.merchant_id
    assert row.actor_user_id == member_a.user_id
    assert row.action == "team_member.invited"
    assert row.target_type == "accounts.teammember"
    assert row.target_id == str(member_a.pk)
    assert row.metadata_json == {"role": "VIEWER"}


def test_record_without_target_or_actor_stores_nulls(member_a):
    with tenant_context(member_a.merchant_id):
        row = record("merchant.updated")
    assert row.target_type is None and row.target_id is None and row.actor_user_id is None


def test_record_without_tenant_context_raises():
    with pytest.raises(TenantContextError):
        record("merchant.updated")


def test_record_rejects_merchant_keyword_argument(member_a):
    with tenant_context(member_a.merchant_id):
        with pytest.raises(TypeError):
            record("x", merchant_id=member_a.merchant_id)


def test_record_rejects_actor_who_only_belongs_to_another_merchant(member_a, member_b):
    with tenant_context(member_a.merchant_id):
        with pytest.raises(ActorNotMember):
            record("merchant.updated", actor=member_b.user)
    with tenant_context(member_a.merchant_id), tenant_atomic():
        assert not AuditLog.objects.exists()


def test_a_scoped_audit_rows_are_invisible_under_merchant_b(member_a, member_b):
    with tenant_context(member_a.merchant_id):
        row = record("merchant.updated", actor=member_a.user)
    with tenant_context(member_b.merchant_id), tenant_atomic():
        assert not AuditLog.objects.filter(id=row.id).exists()
        assert AuditLog.objects.count() == 0
    with tenant_context(member_a.merchant_id), tenant_atomic():
        assert AuditLog.objects.filter(id=row.id).exists()


def test_audit_manager_raises_without_context():
    with pytest.raises(TenantContextError):
        list(AuditLog.objects.all())


def test_auditlog_has_no_updated_at_and_no_update_or_delete_services():
    from auditlog import services

    assert not hasattr(AuditLog, "updated_at")
    assert not [n for n in dir(services) if n.startswith(("update", "delete", "remove"))]


def test_actor_not_member_maps_to_403_error_shape():
    from core.api import exception_handler

    resp = exception_handler(ActorNotMember("Audit actor is not a member."), {})
    assert resp.status_code == 403
    assert resp.data == {
        "error": {"code": "actor_not_member", "message": "Audit actor is not a member."}
    }
