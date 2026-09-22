"""RLS backstop and self_membership policy. SET LOCAL semantics need real
transaction boundaries, so these use transaction=True."""
import uuid

import pytest
from django.db import Error, connection, transaction
from django.utils import timezone as dj_timezone

from accounts.models import TeamMember
from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context, user_lookup_atomic

pytestmark = pytest.mark.django_db(transaction=True)


def _ids(sql, params=()):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return {str(r[0]) for r in cur.fetchall()}


def _setting(name):
    with connection.cursor() as cur:
        cur.execute("SELECT current_setting(%s, true)", [name])
        return cur.fetchone()[0]


def test_raw_sql_in_merchant_context_returns_only_that_merchants_teammembers(make_merchant):
    a, b = make_merchant(), make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        ids = _ids("SELECT id FROM accounts_teammember")
    assert ids == {str(a.id)}
    assert str(b.id) not in ids


def test_raw_sql_without_context_returns_no_teammembers(make_merchant):
    make_merchant()
    assert _ids("SELECT id FROM accounts_teammember") == set()


def test_insert_teammember_for_other_merchant_fails_with_check(make_merchant, add_member):
    a, b = make_merchant(), make_merchant()
    intruder = a.user.__class__.objects.create_user("intruder@example.com", "Tr1cky-Horse-Battery-Staple!")
    with tenant_context(a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            TeamMember(
                merchant_id=b.merchant_id,
                user=intruder,
                role=TeamMember.Role.OWNER,
                accepted_at=dj_timezone.now(),
            ).save(force_insert=True)


def test_self_membership_returns_exactly_the_users_rows_across_merchants(make_merchant, add_member):
    a, b, c = make_merchant(), make_merchant(), make_merchant()
    extra = add_member(b.merchant, TeamMember.Role.VIEWER, None, user=a.user)
    with user_lookup_atomic(a.user_id):
        ids = _ids("SELECT id FROM accounts_teammember")
    assert ids == {str(a.id), str(extra.id)}
    assert str(b.id) not in ids and str(c.id) not in ids


def test_self_membership_insert_is_rejected(make_merchant):
    a, b = make_merchant(), make_merchant()
    with user_lookup_atomic(a.user_id):
        with pytest.raises(Error), transaction.atomic():
            TeamMember(
                merchant_id=b.merchant_id,
                user_id=a.user_id,
                role=TeamMember.Role.OWNER,
                accepted_at=dj_timezone.now(),
            ).save(force_insert=True)


def test_self_membership_update_affects_zero_rows(make_merchant):
    a = make_merchant()
    with user_lookup_atomic(a.user_id):
        with connection.cursor() as cur:
            cur.execute("UPDATE accounts_teammember SET role = 'VIEWER' WHERE user_id = %s", [str(a.user_id)])
            assert cur.rowcount == 0
    with tenant_context(a.merchant_id), tenant_atomic():
        assert TeamMember.objects.get(id=a.id).role == TeamMember.Role.OWNER


def test_self_membership_policy_is_select_only():
    with connection.cursor() as cur:
        cur.execute(
            "SELECT policyname, cmd FROM pg_policies WHERE tablename = 'accounts_teammember'"
        )
        policies = dict(cur.fetchall())
    assert policies.get("self_membership") == "SELECT"
    assert "tenant_isolation" in policies
    assert all(cmd == "SELECT" for name, cmd in policies.items() if name != "tenant_isolation")


def test_user_lookup_setting_is_reset_after_transaction(make_merchant):
    a = make_merchant()
    with user_lookup_atomic(a.user_id):
        assert _setting("app.current_user_id") == str(a.user_id)
    assert _setting("app.current_user_id") in (None, "")


def test_user_lookup_setting_is_reset_after_failed_transaction(make_merchant):
    a = make_merchant()
    with pytest.raises(RuntimeError):
        with user_lookup_atomic(a.user_id):
            raise RuntimeError("boom")
    assert _setting("app.current_user_id") in (None, "")


def test_user_lookup_atomic_rejects_bad_user_id():
    with pytest.raises(ValueError):
        with user_lookup_atomic("not-a-uuid"):
            pass


def test_user_lookup_atomic_inside_tenant_context_raises_tenant_context_error(make_merchant):
    a = make_merchant()
    with tenant_context(a.merchant_id):
        with pytest.raises(TenantContextError):
            with user_lookup_atomic(a.user_id):
                pass


def test_user_lookup_atomic_inside_tenant_atomic_raises_and_sets_nothing(make_merchant):
    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        with pytest.raises(TenantContextError):
            with user_lookup_atomic(a.user_id):
                pass
        assert _setting("app.current_user_id") in (None, "")


def test_merchant_context_never_sees_users_membership_in_other_merchant(make_merchant, add_member):
    a, b = make_merchant(), make_merchant()
    extra = add_member(b.merchant, TeamMember.Role.VIEWER, None, user=a.user)
    with tenant_context(a.merchant_id), tenant_atomic():
        ids = _ids("SELECT id FROM accounts_teammember")
    assert str(extra.id) not in ids


def test_user_lookup_with_nonexistent_user_returns_nothing(make_merchant):
    make_merchant()
    with user_lookup_atomic(uuid.uuid4()):
        assert _ids("SELECT id FROM accounts_teammember") == set()


def test_user_lookup_atomic_inside_other_transaction_raises_and_sets_nothing(make_merchant):
    """Durable guard: even with no merchant context, the lookup must be its own
    outermost transaction so SET LOCAL can never outlive it."""
    a = make_merchant()
    with transaction.atomic():
        with pytest.raises(RuntimeError):
            with user_lookup_atomic(a.user_id):
                pass
        assert _setting("app.current_user_id") in (None, "")
