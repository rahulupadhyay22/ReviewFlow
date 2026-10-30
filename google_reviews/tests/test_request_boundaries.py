"""P-1 / P-5: transaction boundaries and the no-in-request-refresh rule (OD-11) on
the two non-atomic endpoints (GET /google/connections/{id}/locations and PUT
/locations/{id}/google-location). Real PostgreSQL commits; the real provider runs
against the fake Google HTTP boundary, so "no token-endpoint call" is a real
assertion."""
from datetime import timedelta

import pytest
from django.db import connection as db_connection
from django.utils import timezone

from accounts import services as account_services
from accounts.models import TeamMember
from core.tenancy import tenant_atomic, tenant_context
from google_reviews import services, tasks
from google_reviews.models import GoogleConnection, GoogleLocation
from google_reviews.tests.helpers import (
    ACCOUNTS_URL,
    LOCATIONS_BASE,
    TOKEN_URL,
    add_member,
    fixture_json,
    google_settings,  # noqa: F401
    http,  # noqa: F401
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("google_settings", "http")]

FIRST = "accounts/100000000001/locations/200000000001"


@pytest.fixture
def world(make_merchant, make_location, make_connection, session_client, http):
    owner = make_merchant("A")
    http.route(ACCOUNTS_URL, fixture_json("accounts.json"))
    http.route(LOCATIONS_BASE, fixture_json("locations.json"))
    return {
        "owner": owner,
        "client": session_client(owner.user.email),
        "location": make_location(owner.merchant),
        "connection": make_connection(owner.merchant),
    }


@pytest.fixture
def refresh_calls(world, monkeypatch, spawn):
    """Records every refresh_connection_token.delay: its args, whether it ran
    inside a transaction, and whether a second database connection sees the
    connection row at that moment."""
    calls = []

    def seen_by_second_connection():
        def read():
            with tenant_atomic():
                return GoogleConnection.objects.filter(pk=world["connection"].pk).exists()

        return spawn(read, world["owner"].merchant_id).join()

    def record(*args):
        calls.append((args, db_connection.in_atomic_block, seen_by_second_connection()))

    monkeypatch.setattr(tasks.refresh_connection_token, "delay", record)
    return calls


def list_url(world):
    return f"/api/v1/google/connections/{world['connection'].id}/locations"


def put(world, client=None):
    client = client or world["client"]
    return client.put(
        f"/api/v1/locations/{world['location'].id}/google-location",
        {"google_connection_id": str(world["connection"].id), "google_location_id": FIRST},
        format="json",
        HTTP_X_CSRFTOKEN=client.csrf,
    )


def set_expiry(world, **delta):
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        GoogleConnection.objects.update(expires_at=timezone.now() + timedelta(**delta))


def mapping_count(world):
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        return GoogleLocation.objects.count()


# --- fresh token: read-only Google GETs, outside any transaction ----------------------------


def test_the_location_list_makes_only_read_only_gets_outside_a_transaction(world, http):
    response = world["client"].get(list_url(world))

    assert response.status_code == 200
    assert [(r["has_place_id"], r["has_review_link"]) for r in response.json()["results"]] == [
        (True, True),
        (False, True),
        (True, False),
    ]
    assert http.calls and {c.method for c in http.calls} == {"GET"}
    assert not http.to(TOKEN_URL)
    assert all(c.in_atomic is False for c in http.calls)


def test_the_mapping_put_makes_only_read_only_gets_outside_a_transaction(world, http, sync_enqueues):
    response = put(world)

    assert response.status_code == 200, response.content
    assert http.calls and {c.method for c in http.calls} == {"GET"}
    assert not http.to(TOKEN_URL)
    assert all(c.in_atomic is False for c in http.calls)
    assert mapping_count(world) == 1


def test_the_mapping_from_the_documented_payload_has_the_resource_name_place_id_and_new_review_uri(world, sync_enqueues):
    put(world)

    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        row = GoogleLocation.objects.get()
    assert (row.google_location_id, row.google_place_id, row.review_link) == (
        FIRST,
        "ChIJ-TEST-PLACE-1",
        "https://g.page/r/TEST-REVIEW-1/review",
    )


# --- token inside the refresh margin: 409, no token endpoint, one task after commit -----------------


@pytest.mark.parametrize("delta", [dict(seconds=60), dict(seconds=-60)], ids=["inside-margin", "already-expired"])
def test_the_location_list_answers_409_refresh_pending_and_enqueues_one_refresh_after_commit(
    world, http, refresh_calls, delta
):
    set_expiry(world, **delta)

    response = world["client"].get(list_url(world))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "google_token_refresh_pending"
    assert http.calls == []  # no token endpoint, no Google call at all
    assert refresh_calls == [
        ((str(world["owner"].merchant_id), str(world["connection"].pk)), False, True)
    ]


@pytest.mark.parametrize("delta", [dict(seconds=60), dict(seconds=-60)], ids=["inside-margin", "already-expired"])
def test_the_mapping_put_answers_409_refresh_pending_and_enqueues_one_refresh_after_commit(
    world, http, refresh_calls, sync_enqueues, delta
):
    set_expiry(world, **delta)

    response = put(world)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "google_token_refresh_pending"
    assert http.calls == []
    assert refresh_calls == [
        ((str(world["owner"].merchant_id), str(world["connection"].pk)), False, True)
    ]
    assert mapping_count(world) == 0 and sync_enqueues == []


@pytest.mark.parametrize("endpoint", ["list", "put"])
def test_no_refresh_is_enqueued_when_the_first_block_raises(world, http, refresh_calls, sync_enqueues, monkeypatch, endpoint):
    set_expiry(world, seconds=60)
    real = services._read_token_in_block

    def read_then_fail(connection):
        real(connection)  # registers the on_commit refresh inside block A
        raise RuntimeError("block A fails")

    monkeypatch.setattr(services, "_read_token_in_block", read_then_fail)
    world["client"].raise_request_exception = False

    response = world["client"].get(list_url(world)) if endpoint == "list" else put(world)

    assert response.status_code >= 500
    assert refresh_calls == []
    assert http.calls == []


# --- permissions: the membership read works under RLS --------------------------------------------------


def test_owner_and_admin_are_allowed_on_the_location_list_and_viewer_and_manager_are_not(world, session_client):
    merchant = world["owner"].merchant
    admin = add_member(merchant, TeamMember.Role.ADMIN, "admin@example.com")
    viewer = add_member(merchant, TeamMember.Role.VIEWER, "viewer@example.com")
    manager = add_member(merchant, TeamMember.Role.MANAGER, "manager@example.com")

    statuses = {
        "owner": world["client"].get(list_url(world)).status_code,
        "admin": session_client(admin.user.email).get(list_url(world)).status_code,
        "viewer": session_client(viewer.user.email).get(list_url(world)).status_code,
        "manager": session_client(manager.user.email).get(list_url(world)).status_code,
    }

    assert statuses == {"owner": 200, "admin": 200, "viewer": 403, "manager": 403}


def test_put_is_allowed_for_owner_admin_and_an_assigned_manager_and_denied_for_viewer(world, session_client, sync_enqueues):
    merchant = world["owner"].merchant
    admin = add_member(merchant, TeamMember.Role.ADMIN, "admin@example.com")
    viewer = add_member(merchant, TeamMember.Role.VIEWER, "viewer@example.com")
    manager = add_member(merchant, TeamMember.Role.MANAGER, "manager@example.com")
    with tenant_context(world["owner"].merchant_id):
        account_services.set_team_member_locations(
            actor=world["owner"], member_id=manager.id, location_ids=[world["location"].id]
        )

    statuses = {
        "viewer": put(world, session_client(viewer.user.email)).status_code,
        "owner": put(world).status_code,
        "admin": put(world, session_client(admin.user.email)).status_code,
        "manager": put(world, session_client(manager.user.email)).status_code,
    }

    assert statuses == {"viewer": 403, "owner": 200, "admin": 200, "manager": 200}
