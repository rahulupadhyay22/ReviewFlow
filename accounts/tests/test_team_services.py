import threading

import pytest
from django.core.exceptions import ValidationError
from django.db import connection

from accounts import services
from accounts.exceptions import (
    AlreadyMember,
    InvalidInvite,
    LastOwner,
    TeamMemberNotFound,
    TeamPermissionDenied,
)
from accounts.models import TeamMember, User
from auditlog.models import AuditLog
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

STRONG_PASSWORD = "Br1ght-Falcon-Meadow-7!"


def _owner(merchant):
    return TeamMember.objects.get(merchant=merchant, role="OWNER")


def _audit_rows(merchant_id, action=None):
    qs = AuditLog.objects.filter(merchant_id=merchant_id)
    if action:
        qs = qs.filter(action=action)
    return list(qs)


# --- list_team_members -------------------------------------------------


def test_list_team_members_orders_by_created_at_and_is_tenant_scoped(make_merchant, add_member):
    a = make_merchant()
    add_member(a.merchant, "ADMIN", "admin@example.com")
    b = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        members = list(services.list_team_members())
    assert [m.role for m in members] == ["OWNER", "ADMIN"]
    assert str(b.merchant_id) not in [str(m.merchant_id) for m in members]


# --- invite_team_member --------------------------------------------------


def test_invite_new_email_creates_user_with_unusable_password_and_pending_member(make_merchant):
    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        member, token = services.invite_team_member(actor=_owner(a.merchant), email="New@X.com", role="ADMIN")
    with tenant_context(a.merchant_id), tenant_atomic():
        member.refresh_from_db()
        assert member.accepted_at is None
        assert member.invited_at is not None
        assert member.user.email == "new@x.com"
        assert not member.user.has_usable_password()
        assert token
        assert _audit_rows(a.merchant_id, "team_member.invited")


def test_invite_reuses_existing_user_from_another_merchant(make_merchant):
    a, b = make_merchant(), make_merchant()
    with tenant_context(b.merchant_id), tenant_atomic():
        services.invite_team_member(actor=_owner(b.merchant), email="shared@x.com", role="VIEWER")
    existing_user = User.objects.get(email="shared@x.com")
    with tenant_context(a.merchant_id), tenant_atomic():
        member, _ = services.invite_team_member(actor=_owner(a.merchant), email="shared@x.com", role="ADMIN")
    assert member.user_id == existing_user.id


def test_reinviting_pending_member_refreshes_role_invited_at_and_token(make_merchant):
    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        member1, token1 = services.invite_team_member(actor=_owner(a.merchant), email="p@x.com", role="VIEWER")
        member2, token2 = services.invite_team_member(actor=_owner(a.merchant), email="p@x.com", role="ADMIN")
    assert member1.pk == member2.pk
    assert member2.role == "ADMIN"
    assert token1 != token2
    with tenant_context(a.merchant_id), tenant_atomic():
        assert TeamMember.objects.filter(merchant=a.merchant, user=member1.user).count() == 1
        assert len(_audit_rows(a.merchant_id, "team_member.invited")) == 2


def test_reinvite_rotates_token_old_token_fails_new_token_accepts(make_merchant):
    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        member, old_token = services.invite_team_member(actor=_owner(a.merchant), email="rot@x.com", role="VIEWER")
        _, new_token = services.invite_team_member(actor=_owner(a.merchant), email="rot@x.com", role="ADMIN")

    with pytest.raises(InvalidInvite):
        services.accept_invite(token=old_token, password=STRONG_PASSWORD)
    with tenant_context(a.merchant_id), tenant_atomic():
        member.refresh_from_db()
        assert member.accepted_at is None

    services.accept_invite(token=new_token, password=STRONG_PASSWORD)
    with tenant_context(a.merchant_id), tenant_atomic():
        member.refresh_from_db()
        assert member.accepted_at is not None


def test_inviting_accepted_member_raises_already_member_and_keeps_one_row(make_merchant, add_member):
    a = make_merchant()
    accepted = add_member(a.merchant, "VIEWER", "accepted@x.com")
    with tenant_context(a.merchant_id), tenant_atomic(), pytest.raises(AlreadyMember):
        services.invite_team_member(actor=_owner(a.merchant), email="accepted@x.com", role="ADMIN")
    with tenant_context(a.merchant_id), tenant_atomic():
        assert TeamMember.objects.filter(merchant=a.merchant, user=accepted.user).count() == 1
        assert not _audit_rows(a.merchant_id, "team_member.invited")


def test_admin_inviting_owner_role_raises_team_permission_denied(make_merchant, add_member):
    a = make_merchant()
    admin = add_member(a.merchant, "ADMIN", "admin@x.com")
    with tenant_context(a.merchant_id), tenant_atomic(), pytest.raises(TeamPermissionDenied):
        services.invite_team_member(actor=admin, email="wannabe-owner@x.com", role="OWNER")
    with tenant_context(a.merchant_id), tenant_atomic():
        assert not _audit_rows(a.merchant_id, "team_member.invited")


def test_owner_inviting_owner_role_succeeds(make_merchant):
    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        member, _ = services.invite_team_member(actor=_owner(a.merchant), email="co-owner@x.com", role="OWNER")
    assert member.role == "OWNER"


@pytest.mark.django_db(transaction=True)
def test_concurrent_invites_of_same_email_create_one_member_two_audit_rows_one_valid_token(make_merchant):
    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        owner = _owner(a.merchant)
    merchant_id = a.merchant_id
    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def worker(role):
        try:
            barrier.wait(timeout=5)
            with tenant_context(merchant_id), tenant_atomic():
                member, token = services.invite_team_member(actor=owner, email="race@x.com", role=role)
            with lock:
                results.append(token)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(role,)) for role in ("VIEWER", "ADMIN")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    with tenant_context(merchant_id), tenant_atomic():
        assert TeamMember.objects.filter(merchant_id=merchant_id, user__email="race@x.com").count() == 1
        assert len(_audit_rows(merchant_id, "team_member.invited")) == 2

    accepted = 0
    for token in results:
        try:
            services.accept_invite(token=token, password=STRONG_PASSWORD)
            accepted += 1
        except InvalidInvite:
            pass
    assert accepted == 1


# invite_team_member() only normalizes the email (UserManager.normalize_email)
# and does not validate email format or role membership in Role.choices --
# that validation is the serializer/API boundary's job, covered by
# test_post_invite_invalid_role_or_email_returns_422 in test_team_api.py.


# --- change_team_member_role ---------------------------------------------


def test_owner_can_change_any_other_members_role(make_merchant, add_member):
    a = make_merchant()
    target = add_member(a.merchant, "VIEWER", "v@x.com")
    with tenant_context(a.merchant_id), tenant_atomic():
        updated = services.change_team_member_role(actor=_owner(a.merchant), member_id=target.pk, role="ADMIN")
    assert updated.role == "ADMIN"
    with tenant_context(a.merchant_id), tenant_atomic():
        assert _audit_rows(a.merchant_id, "team_member.role_changed")


def test_admin_can_change_manager_or_viewer_role(make_merchant, add_member):
    a = make_merchant()
    admin = add_member(a.merchant, "ADMIN", "admin@x.com")
    target = add_member(a.merchant, "VIEWER", "v@x.com")
    with tenant_context(a.merchant_id), tenant_atomic():
        updated = services.change_team_member_role(actor=admin, member_id=target.pk, role="MANAGER")
    assert updated.role == "MANAGER"


def test_admin_targeting_owner_raises_team_permission_denied(make_merchant, add_member):
    a = make_merchant()
    admin = add_member(a.merchant, "ADMIN", "admin@x.com")
    with tenant_context(a.merchant_id), tenant_atomic():
        owner = _owner(a.merchant)
    with tenant_context(a.merchant_id), tenant_atomic(), pytest.raises(TeamPermissionDenied):
        services.change_team_member_role(actor=admin, member_id=owner.pk, role="ADMIN")


def test_admin_granting_owner_role_raises_team_permission_denied(make_merchant, add_member):
    a = make_merchant()
    admin = add_member(a.merchant, "ADMIN", "admin@x.com")
    target = add_member(a.merchant, "VIEWER", "v@x.com")
    with tenant_context(a.merchant_id), tenant_atomic(), pytest.raises(TeamPermissionDenied):
        services.change_team_member_role(actor=admin, member_id=target.pk, role="OWNER")


def test_changing_own_role_raises_team_permission_denied(make_merchant):
    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        owner = _owner(a.merchant)
    with tenant_context(a.merchant_id), tenant_atomic(), pytest.raises(TeamPermissionDenied):
        services.change_team_member_role(actor=owner, member_id=owner.pk, role="ADMIN")


def test_noop_role_change_writes_no_audit_row(make_merchant, add_member):
    a = make_merchant()
    target = add_member(a.merchant, "VIEWER", "v@x.com")
    with tenant_context(a.merchant_id), tenant_atomic():
        services.change_team_member_role(actor=_owner(a.merchant), member_id=target.pk, role="VIEWER")
    with tenant_context(a.merchant_id), tenant_atomic():
        assert not _audit_rows(a.merchant_id, "team_member.role_changed")


@pytest.mark.django_db(transaction=True)
def test_two_owners_demoting_each_other_concurrently_leave_at_least_one_owner(make_merchant, add_member):
    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        owner1 = _owner(a.merchant)
    owner2 = add_member(a.merchant, "OWNER", "owner2@x.com")
    merchant_id = a.merchant_id
    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def worker(actor_id, target_id):
        try:
            barrier.wait(timeout=5)
            with tenant_context(merchant_id), tenant_atomic():
                actor = TeamMember.objects.get(pk=actor_id)
                try:
                    services.change_team_member_role(actor=actor, member_id=target_id, role="ADMIN")
                    with lock:
                        outcomes.append("ok")
                except (LastOwner, TeamPermissionDenied):
                    # The Merchant row lock serializes the two requests: whichever
                    # commits second re-reads its own (now-changed) role and is
                    # rejected, one way or another. The one invariant that must
                    # hold either way is that at least one accepted OWNER remains.
                    with lock:
                        outcomes.append("blocked")
        finally:
            connection.close()

    threads = [
        threading.Thread(target=worker, args=(owner1.pk, owner2.pk)),
        threading.Thread(target=worker, args=(owner2.pk, owner1.pk)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with tenant_context(merchant_id), tenant_atomic():
        accepted_owners = TeamMember.objects.filter(
            merchant_id=merchant_id, role="OWNER", accepted_at__isnull=False
        ).count()
    assert accepted_owners >= 1
    assert outcomes.count("ok") <= 1
    assert "blocked" in outcomes


# --- revoke_team_member ----------------------------------------------------


def test_revoke_deletes_row_but_keeps_user(make_merchant, add_member):
    a = make_merchant()
    target = add_member(a.merchant, "VIEWER", "v@x.com")
    user_id = target.user_id
    with tenant_context(a.merchant_id), tenant_atomic():
        services.revoke_team_member(actor=_owner(a.merchant), member_id=target.pk)
    with tenant_context(a.merchant_id), tenant_atomic():
        assert not TeamMember.objects.filter(pk=target.pk).exists()
        assert _audit_rows(a.merchant_id, "team_member.removed")
    assert User.objects.filter(pk=user_id).exists()


def test_self_revoke_raises_team_permission_denied(make_merchant):
    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        owner = _owner(a.merchant)
    with tenant_context(a.merchant_id), tenant_atomic(), pytest.raises(TeamPermissionDenied):
        services.revoke_team_member(actor=owner, member_id=owner.pk)


def test_admin_revoking_owner_raises_team_permission_denied(make_merchant, add_member):
    a = make_merchant()
    admin = add_member(a.merchant, "ADMIN", "admin@x.com")
    with tenant_context(a.merchant_id), tenant_atomic():
        owner = _owner(a.merchant)
    with tenant_context(a.merchant_id), tenant_atomic(), pytest.raises(TeamPermissionDenied):
        services.revoke_team_member(actor=admin, member_id=owner.pk)


def test_revoking_pending_member_makes_their_token_invalid(make_merchant):
    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        member, token = services.invite_team_member(actor=_owner(a.merchant), email="p@x.com", role="VIEWER")
        services.revoke_team_member(actor=_owner(a.merchant), member_id=member.pk)

    with pytest.raises(InvalidInvite):
        services.accept_invite(token=token, password=STRONG_PASSWORD)


# --- accept_invite -----------------------------------------------------


def test_accept_invite_new_user_sets_password_and_accepted_at(make_merchant):
    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        member, token = services.invite_team_member(actor=_owner(a.merchant), email="new@x.com", role="VIEWER")

    services.accept_invite(token=token, password=STRONG_PASSWORD)

    with tenant_context(a.merchant_id), tenant_atomic():
        member.refresh_from_db()
        assert member.accepted_at is not None
        assert _audit_rows(a.merchant_id, "team_member.accepted")
    member.user.refresh_from_db()
    assert member.user.has_usable_password()
    assert member.user.check_password(STRONG_PASSWORD)


def test_accept_invite_weak_password_raises_validation_error_and_leaves_state_unchanged(make_merchant):
    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        member, token = services.invite_team_member(actor=_owner(a.merchant), email="weak@x.com", role="VIEWER")

    with pytest.raises(ValidationError):
        services.accept_invite(token=token, password="123")

    with tenant_context(a.merchant_id), tenant_atomic():
        member.refresh_from_db()
        assert member.accepted_at is None
    member.user.refresh_from_db()
    assert not member.user.has_usable_password()


def test_accept_invite_existing_user_wrong_password_is_account_takeover_guard(make_merchant, add_member):
    a, b = make_merchant(), make_merchant()
    existing = add_member(b.merchant, "VIEWER", "known@x.com", accepted=True)
    original_hash = existing.user.password
    with tenant_context(a.merchant_id), tenant_atomic():
        member, token = services.invite_team_member(actor=_owner(a.merchant), email="known@x.com", role="VIEWER")

    with pytest.raises(InvalidInvite):
        services.accept_invite(token=token, password="totally-wrong-password")

    with tenant_context(a.merchant_id), tenant_atomic():
        member.refresh_from_db()
        assert member.accepted_at is None
    existing.user.refresh_from_db()
    assert existing.user.password == original_hash


def test_accept_invite_existing_user_correct_password_accepts_without_changing_hash(
    make_merchant, add_member, password
):
    a, b = make_merchant(), make_merchant()
    existing = add_member(b.merchant, "VIEWER", "known2@x.com", accepted=True)
    original_hash = existing.user.password
    with tenant_context(a.merchant_id), tenant_atomic():
        member, token = services.invite_team_member(actor=_owner(a.merchant), email="known2@x.com", role="ADMIN")

    services.accept_invite(token=token, password=password)

    with tenant_context(a.merchant_id), tenant_atomic():
        member.refresh_from_db()
        assert member.accepted_at is not None
    existing.user.refresh_from_db()
    assert existing.user.password == original_hash


def test_uninitialized_shared_account_second_merchant_invite_accepts_and_first_stays_pending(make_merchant):
    """A User created by merchant B's still-pending invite, then invited by
    merchant A, accepts A's token. B's membership stays pending."""
    a, b = make_merchant(), make_merchant()
    with tenant_context(b.merchant_id), tenant_atomic():
        b_member, _ = services.invite_team_member(actor=_owner(b.merchant), email="shared3@x.com", role="VIEWER")
    with tenant_context(a.merchant_id), tenant_atomic():
        a_member, a_token = services.invite_team_member(actor=_owner(a.merchant), email="shared3@x.com", role="ADMIN")

    services.accept_invite(token=a_token, password=STRONG_PASSWORD)

    with tenant_context(a.merchant_id), tenant_atomic():
        a_member.refresh_from_db()
        assert a_member.accepted_at is not None
    with tenant_context(b.merchant_id), tenant_atomic():
        b_member.refresh_from_db()
        assert b_member.accepted_at is None
    assert a_member.user.check_password(STRONG_PASSWORD)


def test_uninitialized_shared_account_weak_password_leaves_password_unusable(make_merchant):
    a, b = make_merchant(), make_merchant()
    with tenant_context(b.merchant_id), tenant_atomic():
        services.invite_team_member(actor=_owner(b.merchant), email="shared4@x.com", role="VIEWER")
    with tenant_context(a.merchant_id), tenant_atomic():
        a_member, a_token = services.invite_team_member(actor=_owner(a.merchant), email="shared4@x.com", role="ADMIN")

    with pytest.raises(ValidationError):
        services.accept_invite(token=a_token, password="123")

    with tenant_context(a.merchant_id), tenant_atomic():
        a_member.refresh_from_db()
        assert a_member.accepted_at is None
    a_member.user.refresh_from_db()
    assert not a_member.user.has_usable_password()


def test_cross_merchant_token_does_not_touch_other_merchants_membership(make_merchant):
    """Accepting merchant A's token for a user who also has a pending
    membership in merchant B must not affect B's row or write a B audit row."""
    a, b = make_merchant(), make_merchant()
    with tenant_context(b.merchant_id), tenant_atomic():
        b_member, _ = services.invite_team_member(actor=_owner(b.merchant), email="cross@x.com", role="VIEWER")
    b_role, b_invited_at = b_member.role, b_member.invited_at
    with tenant_context(a.merchant_id), tenant_atomic():
        a_member, a_token = services.invite_team_member(actor=_owner(a.merchant), email="cross@x.com", role="ADMIN")

    services.accept_invite(token=a_token, password=STRONG_PASSWORD)

    with tenant_context(a.merchant_id), tenant_atomic():
        a_member.refresh_from_db()
        assert a_member.accepted_at is not None
    with tenant_context(b.merchant_id), tenant_atomic():
        b_member.refresh_from_db()
        assert b_member.accepted_at is None
        assert b_member.role == b_role
        assert b_member.invited_at == b_invited_at
        assert not _audit_rows(b.merchant_id, "team_member.accepted")


def test_token_payload_pointing_at_different_member_id_fails_signature_check(make_merchant):
    from django.core import signing

    a, b = make_merchant(), make_merchant()
    with tenant_context(b.merchant_id), tenant_atomic():
        b_member, _ = services.invite_team_member(actor=_owner(b.merchant), email="edit-target@x.com", role="VIEWER")
    with tenant_context(a.merchant_id), tenant_atomic():
        a_member, a_token = services.invite_team_member(
            actor=_owner(a.merchant), email="edit-payload@x.com", role="VIEWER"
        )

    # Forge a token for A's signer that points at B's member id, without A's
    # signing key: unsign A's own valid token, edit the payload, and re-sign
    # with a *different* signer to simulate an attacker without the secret.
    payload = services._invite_signer.unsign_object(a_token, max_age=services.INVITE_MAX_AGE)
    payload["tm"] = str(b_member.pk)
    forged_signer = signing.TimestampSigner(key="attacker-does-not-have-this-key", salt="accounts.invite")
    forged_token = forged_signer.sign_object(payload)

    with pytest.raises(InvalidInvite):
        services.accept_invite(token=forged_token, password=STRONG_PASSWORD)


def test_expired_token_raises_invalid_invite(make_merchant):
    from unittest import mock

    from django.utils import timezone as dj_timezone

    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        member, token = services.invite_team_member(actor=_owner(a.merchant), email="expired@x.com", role="VIEWER")

    future = dj_timezone.now() + services.INVITE_MAX_AGE + services.INVITE_MAX_AGE
    with mock.patch("django.core.signing.time.time", return_value=future.timestamp()):
        with pytest.raises(InvalidInvite):
            services.accept_invite(token=token, password=STRONG_PASSWORD)


def test_non_active_merchant_raises_invalid_invite(make_merchant):
    from accounts.models import Merchant

    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        member, token = services.invite_team_member(actor=_owner(a.merchant), email="suspended@x.com", role="VIEWER")
    Merchant.objects.filter(id=a.merchant_id).update(status=Merchant.Status.SUSPENDED)

    with pytest.raises(InvalidInvite):
        services.accept_invite(token=token, password=STRONG_PASSWORD)
    with tenant_context(a.merchant_id), tenant_atomic():
        member.refresh_from_db()
        assert member.accepted_at is None


def test_accepting_same_token_twice_results_in_one_acceptance_and_one_audit_row(make_merchant):
    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        member, token = services.invite_team_member(actor=_owner(a.merchant), email="dup@x.com", role="VIEWER")

    services.accept_invite(token=token, password=STRONG_PASSWORD)

    with pytest.raises(InvalidInvite):
        services.accept_invite(token=token, password=STRONG_PASSWORD)

    with tenant_context(a.merchant_id), tenant_atomic():
        assert len(_audit_rows(a.merchant_id, "team_member.accepted")) == 1


@pytest.mark.django_db(transaction=True)
def test_accept_invite_leaves_no_tenant_context_set_on_the_connection(make_merchant):
    """accept_invite's write happens inside its own tenant_context/tenant_atomic,
    separate from the read-only user_lookup_atomic; after it returns, no
    merchant context lingers on the connection."""
    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        member, token = services.invite_team_member(actor=_owner(a.merchant), email="setlocal@x.com", role="VIEWER")

    services.accept_invite(token=token, password=STRONG_PASSWORD)

    with connection.cursor() as cur:
        cur.execute("SELECT current_setting('app.current_merchant_id', true)")
        merchant_after = cur.fetchone()[0]
    assert merchant_after in (None, "")


# --- code-review regressions ----------------------------------------------


def test_invite_owner_grant_uses_role_re_read_under_merchant_lock(make_merchant, add_member):
    """An OWNER demoted after IsOwnerOrAdmin resolved their role (stale actor
    object) must not be able to grant OWNER: the check uses the membership
    re-read under the Merchant row lock."""
    a = make_merchant()
    second = add_member(a.merchant, "OWNER", "owner2@x.com")
    with tenant_context(a.merchant_id), tenant_atomic():
        stale_actor = TeamMember.objects.get(pk=second.pk)  # role OWNER, as the view saw it
        services.change_team_member_role(actor=a, member_id=second.pk, role="ADMIN")

    with pytest.raises(TeamPermissionDenied):
        with tenant_context(a.merchant_id), tenant_atomic():
            services.invite_team_member(actor=stale_actor, email="escalate@x.com", role="OWNER")

    with tenant_context(a.merchant_id), tenant_atomic():
        assert not TeamMember.objects.filter(user__email="escalate@x.com").exists()
        assert _audit_rows(a.merchant_id, services.AUDIT_INVITED) == []


def test_would_orphan_owners_true_only_for_the_last_accepted_owner(make_merchant, add_member):
    """Direct unit test of the defensive invariant check. No legitimate
    request reaches the True case today (self-target is 403), so it is
    exercised here rather than through the API."""
    a = make_merchant()
    admin = add_member(a.merchant, "ADMIN", "admin@x.com")
    pending_owner = add_member(a.merchant, "OWNER", "pending@x.com", accepted=False)
    with tenant_context(a.merchant_id), tenant_atomic():
        sole = TeamMember.objects.get(
            merchant=a.merchant, role="OWNER", accepted_at__isnull=False
        )
        assert services._would_orphan_owners(sole) is True  # a pending OWNER doesn't count
        assert services._would_orphan_owners(admin) is False  # non-OWNER target
        assert services._would_orphan_owners(pending_owner) is False  # an accepted OWNER remains

    second = add_member(a.merchant, "OWNER", "owner2@x.com")
    with tenant_context(a.merchant_id), tenant_atomic():
        sole.refresh_from_db()
        assert services._would_orphan_owners(sole) is False
        assert services._would_orphan_owners(second) is False


def test_change_role_unknown_id_raises_team_member_not_found(make_merchant, add_member):
    a, b = make_merchant(), make_merchant()
    b_member = add_member(b.merchant, "VIEWER", "b@x.com")
    with tenant_context(a.merchant_id), tenant_atomic():
        actor = TeamMember.objects.get(merchant=a.merchant, role="OWNER")
    with pytest.raises(TeamMemberNotFound):
        with tenant_context(a.merchant_id), tenant_atomic():
            services.change_team_member_role(actor=actor, member_id=b_member.pk, role="ADMIN")
    with pytest.raises(TeamMemberNotFound):
        with tenant_context(a.merchant_id), tenant_atomic():
            services.revoke_team_member(actor=actor, member_id=b_member.pk)


def test_accept_invite_merchant_suspended_between_lookup_and_write_raises_invalid_invite(
    make_merchant, monkeypatch
):
    from accounts.models import Merchant

    a = make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        actor = TeamMember.objects.get(merchant=a.merchant, role="OWNER")
        member, token = services.invite_team_member(actor=actor, email="race@x.com", role="VIEWER")

    real_tenant_context = services.tenant_context

    def suspend_then_enter(merchant_id):
        # Runs after the user_lookup_atomic read saw an ACTIVE merchant,
        # right before the tenant write phase.
        Merchant.objects.filter(id=merchant_id).update(status=Merchant.Status.SUSPENDED)
        return real_tenant_context(merchant_id)

    monkeypatch.setattr(services, "tenant_context", suspend_then_enter)
    with pytest.raises(InvalidInvite):
        services.accept_invite(token=token, password=STRONG_PASSWORD)
    monkeypatch.undo()

    with tenant_context(a.merchant_id), tenant_atomic():
        member.refresh_from_db()
        assert member.accepted_at is None
        assert _audit_rows(a.merchant_id, services.AUDIT_ACCEPTED) == []
