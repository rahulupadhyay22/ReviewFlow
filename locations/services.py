"""Location services (Coding-Standards.md §1: business logic lives here,
never in views/serializers)."""
import uuid

from django.db.models import QuerySet

from accounts.models import TeamMember
from accounts.services import validate_timezone
from core.tenancy import get_current_merchant_id, tenant_atomic
from locations.exceptions import LocationNotFound
from locations.models import Location

_UNSET = object()


def accessible_locations(actor: TeamMember) -> QuerySet[Location]:
    """The locations visible to actor. MANAGER sees only assigned
    locations; every other role sees every (tenant-scoped) location.

    This is *the* MANAGER location-scoping rule (Security-Architecture.md
    §Authorization). Later phases (campaigns, etc.) must call this rather
    than re-implementing assignment filtering inline.
    """
    if actor.role == TeamMember.Role.MANAGER:
        return Location.objects.filter(team_member_assignments__team_member=actor)
    return Location.objects.all()


def get_accessible_location(actor: TeamMember, location_id: uuid.UUID | str) -> Location:
    """Raises LocationNotFound (404) for a missing id, a cross-merchant id
    (TenantScopedManager/RLS already hide those), or a location not
    assigned to a MANAGER."""
    try:
        return accessible_locations(actor).get(pk=location_id)
    except Location.DoesNotExist:
        raise LocationNotFound() from None


def create_location(
    *,
    name: str,
    address: str | None = None,
    phone: str | None = None,
    timezone: str | None = None,
) -> Location:
    if timezone is not None:
        validate_timezone(timezone)
    with tenant_atomic():
        return Location.objects.create(
            merchant_id=get_current_merchant_id(),
            name=name,
            address=address,
            phone=phone,
            timezone=timezone,
        )


def update_location(
    location: Location,
    *,
    name: str = _UNSET,
    address: str | None = _UNSET,
    phone: str | None = _UNSET,
    timezone: str | None = _UNSET,
) -> Location:
    fields = {}
    if name is not _UNSET:
        fields["name"] = name
    if address is not _UNSET:
        fields["address"] = address
    if phone is not _UNSET:
        fields["phone"] = phone
    if timezone is not _UNSET:
        if timezone is not None:
            validate_timezone(timezone)
        fields["timezone"] = timezone
    for field, value in fields.items():
        setattr(location, field, value)
    if fields:
        location.save(update_fields=[*fields, "updated_at"])
    return location


def deactivate_location(location: Location) -> None:
    """Soft-delete: is_active = False. Idempotent; touches no other row."""
    if location.is_active:
        location.is_active = False
        location.save(update_fields=["is_active", "updated_at"])
