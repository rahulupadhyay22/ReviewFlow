import uuid

import pytest
from django.db import IntegrityError, transaction

from accounts.models import Merchant, TeamMember, User
from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context, user_lookup_atomic

pytestmark = pytest.mark.django_db


def test_user_email_unique_case_insensitively():
    User.objects.create_user("A@x.com", "pw-Unused-123!")
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create_user("a@x.com", "pw-Unused-123!")


def test_user_password_is_hashed_and_id_is_uuid():
    user = User.objects.create_user("hash@x.com", "plain-Secret-123!")
    user.refresh_from_db()
    assert user.password != "plain-Secret-123!"
    assert "plain-Secret-123!" not in user.password
    assert user.check_password("plain-Secret-123!")
    assert isinstance(user.id, uuid.UUID)


def test_create_superuser_with_email_only_sets_staff_flags():
    user = User.objects.create_superuser("root@x.com", "Root-Passw0rd-xyz!")
    assert user.is_staff and user.is_superuser


def test_teammember_unique_merchant_user_rejected_by_database(make_merchant):
    member = make_merchant()
    with tenant_context(member.merchant_id), tenant_atomic():
        with pytest.raises(IntegrityError), transaction.atomic():
            TeamMember.objects.create(
                merchant=member.merchant, user=member.user, role=TeamMember.Role.VIEWER
            )


def test_merchant_status_defaults_to_active(merchant_a):
    assert merchant_a.status == Merchant.Status.ACTIVE
    assert isinstance(merchant_a.id, uuid.UUID)


def test_teammember_manager_raises_without_context():
    with pytest.raises(TenantContextError):
        list(TeamMember.objects.all())


def test_teammember_manager_never_returns_other_merchants_rows(make_merchant):
    a, b = make_merchant(), make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        ids = set(TeamMember.objects.values_list("id", flat=True))
    assert ids == {a.id}
    assert b.id not in ids


def test_for_lookup_user_raises_outside_user_lookup(make_merchant):
    member = make_merchant()
    with pytest.raises(TenantContextError):
        TeamMember.objects.for_lookup_user(member.user_id)


def test_for_lookup_user_raises_for_different_user_id(make_merchant):
    a, b = make_merchant(), make_merchant()
    with user_lookup_atomic(a.user_id):
        with pytest.raises(TenantContextError):
            TeamMember.objects.for_lookup_user(b.user_id)


def test_for_lookup_user_returns_only_that_users_rows_across_merchants(make_merchant, add_member):
    a, b = make_merchant(), make_merchant()
    add_member(b.merchant, TeamMember.Role.VIEWER, None, user=a.user)
    with user_lookup_atomic(a.user_id):
        rows = list(TeamMember.objects.for_lookup_user(a.user_id))
    assert {r.merchant_id for r in rows} == {a.merchant_id, b.merchant_id}
    assert all(r.user_id == a.user_id for r in rows)
