import uuid
import zoneinfo

from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import HttpRequest
from django.utils import timezone as dj_timezone

from accounts.exceptions import InvalidCredentials
from accounts.models import Merchant, TeamMember, User, UserManager
from core.tenancy import tenant_atomic, tenant_context, user_lookup_atomic

_UNSET = object()


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
