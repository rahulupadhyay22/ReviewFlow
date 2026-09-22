"""Unit tests for locations/services.py (spec §Services, Definition of done
"Location CRUD" and "MANAGER scoping")."""
import pytest
from django.core.exceptions import ValidationError

from accounts.models import TeamMember
from accounts.services import set_team_member_locations
from core.tenancy import tenant_atomic, tenant_context
from locations import services
from locations.exceptions import LocationNotFound

pytestmark = pytest.mark.django_db


def _assign(owner, manager, locations):
    with tenant_context(owner.merchant_id), tenant_atomic():
        set_team_member_locations(
            actor=owner, member_id=manager.id, location_ids=[loc.id for loc in locations]
        )


# --- accessible_locations / get_accessible_location -------------------------


def test_accessible_locations_for_non_manager_returns_all_tenant_locations(make_merchant, make_location):
    owner = make_merchant("A")
    l1 = make_location(owner.merchant, name="L1")
    l2 = make_location(owner.merchant, name="L2")
    with tenant_context(owner.merchant_id), tenant_atomic():
        ids = {loc.id for loc in services.accessible_locations(owner)}
    assert ids == {l1.id, l2.id}


def test_accessible_locations_for_manager_returns_only_assigned(make_merchant, add_member, make_location):
    owner = make_merchant("A")
    l1 = make_location(owner.merchant, name="L1")
    make_location(owner.merchant, name="L2")
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    _assign(owner, manager, [l1])

    with tenant_context(owner.merchant_id), tenant_atomic():
        manager = TeamMember.objects.get(pk=manager.pk)
        ids = {loc.id for loc in services.accessible_locations(manager)}
    assert ids == {l1.id}


def test_accessible_locations_for_manager_with_no_assignments_is_empty(make_merchant, add_member):
    owner = make_merchant("A")
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    with tenant_context(owner.merchant_id), tenant_atomic():
        manager = TeamMember.objects.get(pk=manager.pk)
        assert list(services.accessible_locations(manager)) == []


def test_get_accessible_location_raises_not_found_for_missing_id(make_merchant):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(LocationNotFound):
            services.get_accessible_location(owner, "00000000-0000-0000-0000-000000000000")


def test_get_accessible_location_raises_not_found_for_cross_merchant_id(make_merchant, make_location):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    other = make_location(owner_b.merchant, name="B loc")
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(LocationNotFound):
            services.get_accessible_location(owner_a, other.id)


def test_get_accessible_location_raises_not_found_for_unassigned_manager(
    make_merchant, add_member, make_location
):
    owner = make_merchant("A")
    l1 = make_location(owner.merchant, name="L1")
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    with tenant_context(owner.merchant_id), tenant_atomic():
        manager = TeamMember.objects.get(pk=manager.pk)
        with pytest.raises(LocationNotFound):
            services.get_accessible_location(manager, l1.id)


def test_get_accessible_location_returns_assigned_location_for_manager(
    make_merchant, add_member, make_location
):
    owner = make_merchant("A")
    l1 = make_location(owner.merchant, name="L1")
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    _assign(owner, manager, [l1])
    with tenant_context(owner.merchant_id), tenant_atomic():
        manager = TeamMember.objects.get(pk=manager.pk)
        found = services.get_accessible_location(manager, l1.id)
    assert found.id == l1.id


# --- create_location ----------------------------------------------------


def test_create_location_sets_merchant_from_context_and_is_active_true(make_merchant):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id):
        loc = services.create_location(name="Cafe")
    assert loc.merchant_id == owner.merchant_id
    assert loc.is_active is True


def test_create_location_with_invalid_timezone_raises_validation_error(make_merchant):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id):
        with pytest.raises(ValidationError):
            services.create_location(name="Cafe", timezone="Not/AZone")


def test_create_location_with_valid_timezone_is_stored(make_merchant):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id):
        loc = services.create_location(name="Cafe", timezone="Asia/Kolkata")
    assert loc.timezone == "Asia/Kolkata"


# --- update_location ------------------------------------------------------


def test_update_location_only_changes_given_fields(make_merchant, make_location):
    owner = make_merchant("A")
    loc = make_location(owner.merchant, name="Original", address="Addr 1")
    with tenant_context(owner.merchant_id), tenant_atomic():
        updated = services.update_location(loc, name="Renamed")
    assert updated.name == "Renamed"
    assert updated.address == "Addr 1"


def test_update_location_with_null_clears_address_phone_timezone(make_merchant, make_location):
    owner = make_merchant("A")
    loc = make_location(
        owner.merchant, name="Original", address="Addr 1", phone="+911234", timezone="Asia/Kolkata"
    )
    with tenant_context(owner.merchant_id), tenant_atomic():
        updated = services.update_location(loc, address=None, phone=None, timezone=None)
    assert updated.address is None
    assert updated.phone is None
    assert updated.timezone is None


def test_update_location_with_invalid_timezone_raises_validation_error(make_merchant, make_location):
    owner = make_merchant("A")
    loc = make_location(owner.merchant, name="Original")
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(ValidationError):
            services.update_location(loc, timezone="Not/AZone")


# --- deactivate_location ----------------------------------------------------


def test_deactivate_location_sets_is_active_false(make_merchant, make_location):
    owner = make_merchant("A")
    loc = make_location(owner.merchant, name="Original")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.deactivate_location(loc)
    assert loc.is_active is False


def test_deactivate_location_is_idempotent_and_touches_no_other_row(make_merchant, make_location):
    owner = make_merchant("A")
    loc = make_location(owner.merchant, name="Original")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.deactivate_location(loc)
        updated_at_after_first = loc.updated_at
        services.deactivate_location(loc)
    assert loc.is_active is False
    assert loc.updated_at == updated_at_after_first
