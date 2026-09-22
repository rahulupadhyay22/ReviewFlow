import uuid
import zoneinfo
from datetime import datetime, timedelta

from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone as dj_timezone

from accounts.exceptions import (
    AlreadyMember,
    InvalidCredentials,
    InvalidInvite,
    LastOwner,
    TeamMemberNotFound,
    TeamPermissionDenied,
)
from accounts.models import Merchant, TeamMember, User, UserManager
from auditlog.services import record
from core.tenancy import get_current_merchant_id, tenant_atomic, tenant_context, user_lookup_atomic

_UNSET = object()

# Canonical audit action strings (Audit-Logging.md, this spec's "Canonical
# audit actions" table). Never write a variant of these.
AUDIT_INVITED = "team_member.invited"
AUDIT_ROLE_CHANGED = "team_member.role_changed"
AUDIT_REMOVED = "team_member.removed"
AUDIT_ACCEPTED = "team_member.accepted"

INVITE_MAX_AGE = timedelta(days=7)
_invite_signer = TimestampSigner(salt="accounts.invite")


def validate_timezone(value: str) -> None:
    if value not in zoneinfo.available_timezones():
        raise ValidationError({"timezone": ["Must be a valid IANA time zone name."]})


def create_merchant_with_owner(
    *,
    name: str,
    timezone: str,
    owner_email: str,
    owner_password: str,
    business_type: str | None = None,
) -> TeamMember:
    """Create a Merchant with its OWNER in one transaction. Used by tests and
    the Phase 03 seed command; there is no signup endpoint in the API spec."""
    validate_timezone(timezone)
    email = UserManager.normalize_email(owner_email)
    validate_password(owner_password, user=User(email=email))
    duplicate = ValidationError({"owner_email": ["A user with this email already exists."]})
    if User.objects.filter(email=email).exists():
        raise duplicate  # friendly message; the unique constraint is the real guard

    merchant_id = uuid.uuid4()
    try:
        with tenant_context(merchant_id), tenant_atomic():
            user = User.objects.create_user(email, owner_password)
            merchant = Merchant.objects.create(
                id=merchant_id, name=name, timezone=timezone, business_type=business_type
            )
            return TeamMember.objects.create(
                merchant=merchant,
                user=user,
                role=TeamMember.Role.OWNER,
                accepted_at=dj_timezone.now(),
            )
    except IntegrityError:
        if User.objects.filter(email=email).exists():
            raise duplicate
        raise


def resolve_login_membership(user: User) -> TeamMember | None:
    """The user's accepted membership in an ACTIVE merchant, found before any
    tenant context exists (self_membership RLS policy)."""
    with user_lookup_atomic(user.id):
        # ponytail: oldest membership wins; add merchant switching when the API defines it.
        return (
            TeamMember.objects.for_lookup_user(user.id)
            .filter(accepted_at__isnull=False, merchant__status=Merchant.Status.ACTIVE)
            .select_related("merchant", "user")
            .order_by("created_at")
            .first()
        )


def authenticate_login(*, request: HttpRequest | None, email: str, password: str) -> TeamMember:
    """Raises InvalidCredentials for every failure reason."""
    # ModelBackend rejects inactive users and hashes even for unknown emails.
    user = authenticate(request, email=email, password=password)
    if user is None:
        raise InvalidCredentials()
    membership = resolve_login_membership(user)
    if membership is None:
        raise InvalidCredentials()
    return membership


def get_active_membership(user: User) -> TeamMember | None:
    """The user's accepted membership in the current (ACTIVE) merchant.
    Requires a tenant context."""
    return (
        TeamMember.objects.select_related("merchant", "user")
        .filter(user=user, accepted_at__isnull=False, merchant__status=Merchant.Status.ACTIVE)
        .first()
    )


def update_merchant(
    merchant: Merchant,
    *,
    name: str = _UNSET,
    business_type: str | None = _UNSET,
    timezone: str = _UNSET,
) -> Merchant:
    fields = {}
    if name is not _UNSET:
        fields["name"] = name
    if business_type is not _UNSET:
        fields["business_type"] = business_type
    if timezone is not _UNSET:
        validate_timezone(timezone)
        fields["timezone"] = timezone
    for field, value in fields.items():
        setattr(merchant, field, value)
    if fields:
        merchant.save(update_fields=[*fields, "updated_at"])
    return merchant


# --- Team member management -------------------------------------------------
#
# Every mutation below locks the current Merchant row first, inside its
# tenant_atomic() transaction, to serialize concurrent team changes on the
# same merchant (e.g. two OWNERs demoting each other, or two invites of the
# same email racing). Merchant is GLOBAL with no RLS, so locking it in tenant
# context is legal.


def _lock_team(actor: TeamMember) -> TeamMember:
    """Lock the Merchant row, then re-read the actor's own membership under
    that lock. IsOwnerOrAdmin resolved the actor's role before the lock,
    so two concurrent requests could both see a stale OWNER role; this
    re-read makes the permission check race-safe."""
    Merchant.objects.select_for_update().get(pk=get_current_merchant_id())
    try:
        return TeamMember.objects.select_related("user").get(
            pk=actor.pk, accepted_at__isnull=False
        )
    except TeamMember.DoesNotExist:
        raise TeamPermissionDenied() from None


def _check_can_manage(actor: TeamMember, target: TeamMember, *, new_role: str | None = None) -> None:
    """Raises TeamPermissionDenied for a self target, or when a non-OWNER
    actor targets an OWNER or grants OWNER. Only an OWNER touches OWNER."""
    if target.pk == actor.pk:
        raise TeamPermissionDenied()
    if actor.role != TeamMember.Role.OWNER and (
        target.role == TeamMember.Role.OWNER or new_role == TeamMember.Role.OWNER
    ):
        raise TeamPermissionDenied()


def _check_can_grant_owner(actor: TeamMember, role: str) -> None:
    """Same OWNER-only rule as _check_can_manage, for invite (no target
    TeamMember exists yet)."""
    if role == TeamMember.Role.OWNER and actor.role != TeamMember.Role.OWNER:
        raise TeamPermissionDenied()


def _would_orphan_owners(target: TeamMember) -> bool:
    """True if target is the merchant's last accepted OWNER.

    Defensive invariant only: under the current rules no legitimate request
    reaches a True result. Self role-change/revoke is 403, and only an
    accepted OWNER can target another OWNER, so the actor is always a
    remaining OWNER. The last OWNER is therefore protected by the self-target
    403. Kept so a future rule change cannot silently orphan a merchant.
    """
    if target.role != TeamMember.Role.OWNER:
        return False
    return (
        not TeamMember.objects.filter(role=TeamMember.Role.OWNER, accepted_at__isnull=False)
        .exclude(pk=target.pk)
        .exists()
    )


def _lock_target(member_id: uuid.UUID | str) -> TeamMember:
    """Locks the target TeamMember row only (not the joined User). A missing
    or other-merchant id is TeamMemberNotFound (404, never 403)."""
    try:
        return (
            TeamMember.objects.select_related("user")
            .select_for_update(of=("self",))
            .get(pk=member_id)
        )
    except TeamMember.DoesNotExist:
        raise TeamMemberNotFound() from None


def list_team_members() -> QuerySet[TeamMember]:
    return TeamMember.objects.select_related("user").order_by("created_at")


def invite_team_member(*, actor: TeamMember, email: str, role: str) -> tuple[TeamMember, str]:
    """Invite (or re-invite) a person by email. Returns (member, invite_token).

    Re-inviting a still-pending member refreshes role and invited_at, which
    invalidates any earlier token (its `iat` no longer matches).
    """
    email = UserManager.normalize_email(email)

    with tenant_atomic():
        # Decide on the membership re-read under the Merchant lock, never the
        # pre-lock actor: a concurrently demoted OWNER must not grant OWNER.
        actor = _lock_team(actor)
        _check_can_grant_owner(actor, role)

        try:
            with transaction.atomic():
                user = User.objects.create_user(email, None)
        except IntegrityError:
            user = User.objects.get(email=email)

        now = dj_timezone.now()
        try:
            with transaction.atomic():
                member = TeamMember.objects.create(
                    merchant_id=get_current_merchant_id(),
                    user=user,
                    role=role,
                    invited_at=now,
                )
        except IntegrityError:
            member = TeamMember.objects.select_related("user").get(user=user)
            if member.accepted_at is not None:
                raise AlreadyMember() from None
            member.role = role
            member.invited_at = now
            member.save(update_fields=["role", "invited_at", "updated_at"])

        record(AUDIT_INVITED, actor=actor.user, target=member, metadata={"role": role})
        return member, make_invite_token(member)


def make_invite_token(member: TeamMember) -> str:
    """Signs {tm, u, iat} — UUIDs and a timestamp only, never the email."""
    return _invite_signer.sign_object(
        {"tm": str(member.pk), "u": str(member.user_id), "iat": member.invited_at.isoformat()}
    )


def change_team_member_role(*, actor: TeamMember, member_id: uuid.UUID | str, role: str) -> TeamMember:
    with tenant_atomic():
        actor = _lock_team(actor)
        member = _lock_target(member_id)

        _check_can_manage(actor, member, new_role=role)
        if member.role == role:
            return member  # no-op: no audit row
        if member.role == TeamMember.Role.OWNER and role != TeamMember.Role.OWNER:
            if _would_orphan_owners(member):
                raise LastOwner()

        role_from = member.role
        member.role = role
        member.save(update_fields=["role", "updated_at"])
        record(
            AUDIT_ROLE_CHANGED,
            actor=actor.user,
            target=member,
            metadata={"role_from": role_from, "role_to": role},
        )
        return member


def revoke_team_member(*, actor: TeamMember, member_id: uuid.UUID | str) -> None:
    with tenant_atomic():
        actor = _lock_team(actor)
        member = _lock_target(member_id)

        _check_can_manage(actor, member)
        if _would_orphan_owners(member):
            raise LastOwner()

        record(AUDIT_REMOVED, actor=actor.user, target=member, metadata={"role": member.role})
        member.delete()


def accept_invite(*, token: str, password: str) -> None:
    """Sets accepted_at (and, for an uninitialized account, the password).
    Never touches the password of a User that already has one — otherwise
    an OWNER/ADMIN of ANY merchant could invite a known email and use the
    resulting link to take over that account.

    Expected invite-state/security failures raise InvalidInvite. Anything
    else (DB errors, programming errors) propagates to the standard 500
    path; this never catches a bare Exception.
    """
    try:
        payload = _invite_signer.unsign_object(token, max_age=INVITE_MAX_AGE)
        member_id = uuid.UUID(payload["tm"])
        user_id = uuid.UUID(payload["u"])
        invited_at = datetime.fromisoformat(payload["iat"])
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        raise InvalidInvite() from None

    with user_lookup_atomic(user_id):
        merchant_id = (
            TeamMember.objects.for_lookup_user(user_id)
            .filter(id=member_id, accepted_at__isnull=True, merchant__status=Merchant.Status.ACTIVE)
            .values_list("merchant_id", flat=True)
            .first()
        )
    if merchant_id is None:
        raise InvalidInvite()

    with tenant_context(merchant_id), tenant_atomic():
        try:
            # Suspended/deleted between the lookup and this write -> same
            # generic invalid_invite, not a 500.
            Merchant.objects.select_for_update().get(pk=merchant_id, status=Merchant.Status.ACTIVE)
        except Merchant.DoesNotExist:
            raise InvalidInvite() from None
        try:
            member = TeamMember.objects.select_related("user").select_for_update(of=("self",)).get(
                pk=member_id, user_id=user_id, accepted_at__isnull=True
            )
        except TeamMember.DoesNotExist:
            raise InvalidInvite() from None
        if member.invited_at != invited_at:
            raise InvalidInvite()  # superseded by a re-invite

        user = User.objects.select_for_update().get(pk=user_id)
        if user.has_usable_password():
            if not user.check_password(password):
                raise InvalidInvite()
        else:
            validate_password(password, user=user)  # ValidationError -> 422
            user.set_password(password)
            user.save(update_fields=["password"])

        member.accepted_at = dj_timezone.now()
        member.save(update_fields=["accepted_at", "updated_at"])
        record(AUDIT_ACCEPTED, actor=user, target=member)
