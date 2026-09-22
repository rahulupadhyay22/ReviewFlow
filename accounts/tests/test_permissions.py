from types import SimpleNamespace

import pytest
from django.contrib.auth.models import AnonymousUser
from rest_framework.test import APIClient

from accounts.models import TeamMember, User
from accounts.permissions import IsMerchantMember, IsOwner, IsOwnerOrAdmin
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db


def _request(user, merchant_id):
    return SimpleNamespace(user=user, merchant_id=merchant_id)


def test_is_merchant_member_denies_anonymous():
    assert not IsMerchantMember().has_permission(_request(AnonymousUser(), None), None)


def test_is_merchant_member_denies_without_merchant_id(make_merchant):
    member = make_merchant()
    assert not IsMerchantMember().has_permission(_request(member.user, None), None)


def test_is_merchant_member_denies_user_of_a_different_merchant(make_merchant):
    a, b = make_merchant(), make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        assert not IsMerchantMember().has_permission(_request(b.user, a.merchant_id), None)


def test_is_merchant_member_denies_unaccepted_membership(make_merchant, add_member, password):
    a = make_merchant()
    pending = User.objects.create_user("pending@example.com", password)
    add_member(a.merchant, "VIEWER", None, accepted=False, user=pending)
    with tenant_context(a.merchant_id), tenant_atomic():
        assert not IsMerchantMember().has_permission(_request(pending, a.merchant_id), None)


@pytest.mark.parametrize(
    "perm,allowed",
    [
        (IsOwner, {"OWNER"}),
        (IsOwnerOrAdmin, {"OWNER", "ADMIN"}),
    ],
)
@pytest.mark.parametrize("role", ["OWNER", "ADMIN", "MANAGER", "VIEWER"])
def test_role_permission_classes_allow_only_declared_roles(
    perm, allowed, role, make_merchant, add_member
):
    a = make_merchant()
    email = f"{role.lower()}-perm@example.com"
    member = add_member(a.merchant, role, email)
    with tenant_context(a.merchant_id), tenant_atomic():
        result = perm().has_permission(_request(member.user, a.merchant_id), None)
    assert result is (role in allowed)


def test_default_permission_is_is_merchant_member(settings):
    from rest_framework.settings import api_settings

    assert list(api_settings.DEFAULT_PERMISSION_CLASSES) == [IsMerchantMember]


def test_session_authentication_is_the_only_default_authentication():
    from rest_framework.authentication import SessionAuthentication
    from rest_framework.settings import api_settings

    assert list(api_settings.DEFAULT_AUTHENTICATION_CLASSES) == [SessionAuthentication]


def test_bearer_api_key_is_not_accepted_on_dashboard_endpoint():
    client = APIClient()
    resp = client.get("/api/v1/merchant", HTTP_AUTHORIZATION="Bearer rf_live_fake")
    assert resp.status_code in (401, 403)


def test_admin_login_page_and_user_list_work_for_staff_superuser(client, password):
    User.objects.create_superuser("staff@example.com", password)
    assert client.login(email="staff@example.com", password=password)
    resp = client.get("/admin/accounts/user/")
    assert resp.status_code == 200
    assert b"staff@example.com" in resp.content


def test_admin_merchant_is_read_only(client, password, merchant_a):
    User.objects.create_superuser("staff2@example.com", password)
    client.login(email="staff2@example.com", password=password)
    assert client.get("/admin/accounts/merchant/").status_code == 200
    assert client.get("/admin/accounts/merchant/add/").status_code == 403
    change = client.get(f"/admin/accounts/merchant/{merchant_a.id}/change/")
    # Django view permission: the change page renders read-only; saving is forbidden.
    assert change.status_code == 200
    original_name = merchant_a.name
    save = client.post(
        f"/admin/accounts/merchant/{merchant_a.id}/change/", {"name": "hacked", "timezone": "UTC"}
    )
    assert save.status_code == 403
    merchant_a.refresh_from_db()
    assert merchant_a.name == original_name
    assert client.post(f"/admin/accounts/merchant/{merchant_a.id}/delete/", {"post": "yes"}).status_code == 403


def test_teammember_and_auditlog_not_registered_in_admin():
    from django.contrib import admin

    assert TeamMember not in admin.site._registry
    from auditlog.models import AuditLog

    assert AuditLog not in admin.site._registry
