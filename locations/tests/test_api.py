"""API tests for /locations (API-Specification.md §Locations; spec Definition
of done: Location CRUD, Pagination, Permissions, MANAGER scoping, Tenant
isolation, Principal-derived merchant, CSRF)."""
import uuid

import pytest
from rest_framework.test import APIClient

from accounts.models import TeamMemberLocation
from accounts.services import set_team_member_locations
from core.tenancy import tenant_atomic, tenant_context
from locations.models import Location

pytestmark = pytest.mark.django_db

LOCATIONS = "/api/v1/locations"


def _detail(pk):
    return f"{LOCATIONS}/{pk}"


def _post(client, data):
    return client.post(LOCATIONS, data, format="json", HTTP_X_CSRFTOKEN=client.csrf)


def _patch(client, pk, data):
    return client.patch(_detail(pk), data, format="json", HTTP_X_CSRFTOKEN=client.csrf)


def _delete(client, pk):
    return client.delete(_detail(pk), HTTP_X_CSRFTOKEN=client.csrf)


def _assign(owner, manager, locations):
    with tenant_context(owner.merchant_id), tenant_atomic():
        set_team_member_locations(
            actor=owner, member_id=manager.id, location_ids=[loc.id for loc in locations]
        )


# --- Location CRUD -----------------------------------------------------


@pytest.mark.parametrize("role", ["OWNER", "ADMIN"])
def test_owner_and_admin_can_create_get_patch_delete(role, make_merchant, add_member, session_client):
    owner = make_merchant("A")
    if role == "OWNER":
        client = session_client(owner.user.email)
    else:
        add_member(owner.merchant, "ADMIN", "admin@example.com")
        client = session_client("admin@example.com")

    resp = _post(client, {"name": "Cafe"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["is_active"] is True
    loc_id = body["id"]

    resp = client.get(_detail(loc_id))
    assert resp.status_code == 200
    assert resp.json()["id"] == loc_id

    resp = _patch(client, loc_id, {"name": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"

    resp = _delete(client, loc_id)
    assert resp.status_code == 204

    resp = _delete(client, loc_id)
    assert resp.status_code == 204


def test_post_missing_or_blank_name_returns_422(make_merchant, session_client):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = _post(client, {"name": ""})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"

    resp = _post(client, {})
    assert resp.status_code == 422


def test_post_invalid_timezone_returns_422(make_merchant, session_client):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = _post(client, {"name": "Cafe", "timezone": "Not/AZone"})
    assert resp.status_code == 422


def test_delete_keeps_row_and_assignments_and_still_lists_with_is_active_false(
    make_merchant, add_member, session_client, make_location
):
    owner = make_merchant("A")
    loc = make_location(owner.merchant, name="Cafe")
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    _assign(owner, manager, [loc])

    client = session_client(owner.user.email)
    resp = _delete(client, loc.id)
    assert resp.status_code == 204

    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Location.objects.filter(pk=loc.id).exists()
        assert TeamMemberLocation.objects.filter(team_member=manager, location=loc).exists()

    resp = client.get(LOCATIONS)
    row = next(r for r in resp.json()["results"] if r["id"] == str(loc.id))
    assert row["is_active"] is False


def test_patch_with_null_clears_fields_and_ignores_is_active(make_merchant, session_client, make_location):
    owner = make_merchant("A")
    loc = make_location(owner.merchant, name="Cafe", address="Addr", phone="+911234", timezone="Asia/Kolkata")
    client = session_client(owner.user.email)

    resp = _patch(client, loc.id, {"address": None, "phone": None, "timezone": None, "is_active": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["address"] is None
    assert body["phone"] is None
    assert body["timezone"] is None
    assert body["is_active"] is True  # is_active is not writable via PATCH


# --- Pagination -----------------------------------------------------------


def test_get_locations_default_page_size_and_shape(make_merchant, session_client, make_location):
    owner = make_merchant("A")
    for i in range(30):
        make_location(owner.merchant, name=f"L{i}")
    client = session_client(owner.user.email)

    resp = client.get(LOCATIONS)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"results", "next_cursor"}
    assert len(body["results"]) == 25
    assert body["next_cursor"] is not None


def test_get_locations_limit_over_100_is_clamped(make_merchant, session_client, make_location):
    owner = make_merchant("A")
    for i in range(5):
        make_location(owner.merchant, name=f"L{i}")
    client = session_client(owner.user.email)

    resp = client.get(LOCATIONS, {"limit": 500})
    assert resp.status_code == 200
    # only 5 exist, but a limit above 100 must not error / must be clamped server-side
    assert len(resp.json()["results"]) == 5


def test_get_locations_pagination_has_no_duplicates_or_gaps_and_last_page_has_null_cursor(
    make_merchant, session_client, make_location
):
    owner = make_merchant("A")
    created_ids = {str(make_location(owner.merchant, name=f"L{i}").id) for i in range(30)}
    client = session_client(owner.user.email)

    seen = []
    resp = client.get(LOCATIONS, {"limit": 10})
    while True:
        body = resp.json()
        seen.extend(r["id"] for r in body["results"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        resp = client.get(LOCATIONS, {"limit": 10, "cursor": cursor})

    assert len(seen) == len(set(seen)) == 30
    assert set(seen) == created_ids


# --- Permissions --------------------------------------------------------


def test_viewer_can_get_list_and_detail_but_403_on_writes(make_merchant, add_member, session_client, make_location):
    owner = make_merchant("A")
    loc = make_location(owner.merchant, name="Cafe")
    add_member(owner.merchant, "VIEWER", "viewer@example.com")
    client = session_client("viewer@example.com")

    assert client.get(LOCATIONS).status_code == 200
    assert client.get(_detail(loc.id)).status_code == 200
    assert _post(client, {"name": "New"}).status_code == 403
    assert _patch(client, loc.id, {"name": "X"}).status_code == 403
    assert _delete(client, loc.id).status_code == 403


def test_manager_gets_403_on_post_and_delete(make_merchant, add_member, session_client, make_location):
    owner = make_merchant("A")
    loc = make_location(owner.merchant, name="Cafe")
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    _assign(owner, manager, [loc])
    client = session_client("manager@example.com")

    assert _post(client, {"name": "New"}).status_code == 403
    assert _delete(client, loc.id).status_code == 403


def test_unauthenticated_requests_get_403_on_every_endpoint(make_merchant, make_location):
    owner = make_merchant("A")
    loc = make_location(owner.merchant, name="Cafe")
    client = APIClient()
    assert client.get(LOCATIONS).status_code == 403
    assert client.get(_detail(loc.id)).status_code == 403
    assert client.post(LOCATIONS, {"name": "New"}, format="json").status_code == 403
    assert client.patch(_detail(loc.id), {"name": "X"}, format="json").status_code == 403
    assert client.delete(_detail(loc.id)).status_code == 403


# --- MANAGER scoping ------------------------------------------------------


def test_manager_assigned_to_l1_sees_only_l1_in_list(make_merchant, add_member, session_client, make_location):
    owner = make_merchant("A")
    l1 = make_location(owner.merchant, name="L1")
    make_location(owner.merchant, name="L2")
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    _assign(owner, manager, [l1])
    client = session_client("manager@example.com")

    resp = client.get(LOCATIONS)
    ids = {r["id"] for r in resp.json()["results"]}
    assert ids == {str(l1.id)}


def test_manager_gets_200_on_get_and_patch_for_assigned_location(
    make_merchant, add_member, session_client, make_location
):
    owner = make_merchant("A")
    l1 = make_location(owner.merchant, name="L1")
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    _assign(owner, manager, [l1])
    client = session_client("manager@example.com")

    assert client.get(_detail(l1.id)).status_code == 200
    assert _patch(client, l1.id, {"name": "Renamed"}).status_code == 200


def test_manager_gets_404_on_get_and_patch_for_unassigned_location_and_it_is_unchanged(
    make_merchant, add_member, session_client, make_location
):
    owner = make_merchant("A")
    l1 = make_location(owner.merchant, name="L1")
    l2 = make_location(owner.merchant, name="L2")
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    _assign(owner, manager, [l1])
    client = session_client("manager@example.com")

    resp = client.get(_detail(l2.id))
    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "not_found", "message": "Location not found."}}

    resp = _patch(client, l2.id, {"name": "Hacked"})
    assert resp.status_code == 404

    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Location.objects.get(pk=l2.id).name == "L2"


def test_manager_with_no_assignments_gets_empty_list(make_merchant, add_member, session_client, make_location):
    owner = make_merchant("A")
    make_location(owner.merchant, name="L1")
    add_member(owner.merchant, "MANAGER", "manager@example.com")
    client = session_client("manager@example.com")

    resp = client.get(LOCATIONS)
    assert resp.json()["results"] == []


# --- Tenant isolation, API --------------------------------------------------


def test_cross_merchant_id_returns_404_on_get_patch_delete_and_row_unchanged(
    make_merchant, session_client, make_location
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    b_loc = make_location(owner_b.merchant, name="B loc")
    client = session_client(owner_a.user.email)

    assert client.get(_detail(b_loc.id)).status_code == 404
    assert _patch(client, b_loc.id, {"name": "Hacked"}).status_code == 404
    assert _delete(client, b_loc.id).status_code == 404

    with tenant_context(owner_b.merchant_id), tenant_atomic():
        row = Location.objects.get(pk=b_loc.id)
        assert row.name == "B loc"
        assert row.is_active is True


def test_get_locations_never_includes_other_merchants_rows(make_merchant, session_client, make_location):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    make_location(owner_a.merchant, name="A loc")
    b_loc = make_location(owner_b.merchant, name="B loc")
    client = session_client(owner_a.user.email)

    resp = client.get(LOCATIONS)
    ids = {r["id"] for r in resp.json()["results"]}
    assert str(b_loc.id) not in ids


def test_post_with_other_merchants_id_in_body_creates_in_actors_merchant_only(make_merchant, session_client):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    client = session_client(owner_a.user.email)
    resp = _post(client, {"name": "Cafe", "merchant_id": str(owner_b.merchant_id)})
    assert resp.status_code == 201
    loc_id = resp.json()["id"]

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        assert Location.objects.filter(pk=loc_id).exists()
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        assert not Location.objects.filter(pk=loc_id).exists()


# --- CSRF -----------------------------------------------------------------


def test_post_patch_delete_without_csrf_return_403(make_merchant, session_client, make_location):
    owner = make_merchant("A")
    loc = make_location(owner.merchant, name="Cafe")
    client = session_client(owner.user.email)

    assert client.post(LOCATIONS, {"name": "New"}, format="json").status_code == 403
    assert client.patch(_detail(loc.id), {"name": "X"}, format="json").status_code == 403
    assert client.delete(_detail(loc.id)).status_code == 403

    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Location.objects.get(pk=loc.id).name == "Cafe"


def test_get_unknown_uuid_returns_404_not_500(make_merchant, session_client):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = client.get(_detail(uuid.uuid4()))
    assert resp.status_code == 404
