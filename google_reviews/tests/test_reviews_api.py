"""GET /reviews (spec 09 "GET /reviews", OD-6): documented fields only, filters,
cursor pagination, MANAGER scoping, tenant isolation and API-key access."""
from datetime import datetime, timezone as dt_timezone

import pytest
from rest_framework.test import APIClient

from accounts import services as account_services
from accounts.models import TeamMember
from core.tenancy import tenant_atomic, tenant_context
from google_reviews.models import GoogleReview
from google_reviews.tests.conftest import GOOGLE_A, GOOGLE_B
from google_reviews.tests.helpers import add_member, make_key

pytestmark = pytest.mark.django_db(transaction=True)

URL = "/api/v1/reviews"
FIELDS = {"id", "location_id", "google_review_id", "reviewer_name", "rating", "text", "review_created_at"}

# (location index, review id, rating, created)
SPECS = [
    (0, "r1", 5, datetime(2026, 8, 1, 12, tzinfo=dt_timezone.utc)),
    (0, "r2", 2, datetime(2026, 8, 10, 18, tzinfo=dt_timezone.utc)),
    (0, "r3", 4, datetime(2026, 9, 1, 9, tzinfo=dt_timezone.utc)),
    (1, "r4", 1, datetime(2026, 8, 20, 9, tzinfo=dt_timezone.utc)),
    (1, "r5", 5, datetime(2026, 9, 5, 9, tzinfo=dt_timezone.utc)),
]
NEWEST_FIRST = ["r5", "r3", "r4", "r2", "r1"]


def add_reviews(owner, mapping, specs):
    with tenant_context(owner.merchant_id), tenant_atomic():
        for _, review_id, rating, created in specs:
            GoogleReview.objects.create(
                google_location=mapping,
                google_review_id=review_id,
                reviewer_name="Reviewer " + review_id,
                rating=rating,
                text="text " + review_id,
                review_created_at=created,
                fetched_at=created,
            )


@pytest.fixture
def world(make_merchant, make_location, make_connection, make_mapping, session_client):
    owner = make_merchant("A")
    conn = make_connection(owner.merchant)
    locations = [make_location(owner.merchant, "One"), make_location(owner.merchant, "Two")]
    mappings = [
        make_mapping(owner.merchant, locations[0], conn, GOOGLE_A, reviews=0),
        make_mapping(owner.merchant, locations[1], conn, GOOGLE_B, reviews=0),
    ]
    for index in (0, 1):
        add_reviews(owner, mappings[index], [s for s in SPECS if s[0] == index])

    other = make_merchant("B")
    other_location = make_location(other.merchant)
    other_mapping = make_mapping(other.merchant, other_location, make_connection(other.merchant), GOOGLE_A, reviews=0)
    add_reviews(other, other_mapping, [(0, "b-secret", 5, datetime(2026, 9, 6, tzinfo=dt_timezone.utc))])
    return {
        "owner": owner,
        "client": session_client(owner.user.email),
        "locations": locations,
        "other": other,
        "other_location": other_location,
        "session_client": session_client,
    }


def ids(response):
    assert response.status_code == 200, response.content
    return [r["google_review_id"] for r in response.json()["results"]]


def test_the_documented_fields_only_newest_first(world):
    response = world["client"].get(URL)

    assert ids(response) == NEWEST_FIRST
    for item in response.json()["results"]:
        assert set(item) == FIELDS  # no reply_*, no attribution field
    first = response.json()["results"][0]
    assert first["location_id"] == str(world["locations"][1].id)
    assert (first["rating"], first["reviewer_name"], first["text"]) == (5, "Reviewer r5", "text r5")


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"rating_min": 4}, ["r5", "r3", "r1"]),
        ({"date_from": "2026-08-15"}, ["r5", "r3", "r4"]),
        ({"date_to": "2026-08-10"}, ["r2", "r1"]),  # the whole day is included
        ({"date_from": "2026-08-10", "date_to": "2026-08-20", "rating_min": 1}, ["r4", "r2"]),
    ],
)
def test_filters_by_rating_and_date_range(world, params, expected):
    assert ids(world["client"].get(URL, params)) == expected


def test_filter_by_location(world):
    response = world["client"].get(URL, {"location_id": str(world["locations"][0].id)})
    assert ids(response) == ["r3", "r2", "r1"]


def test_an_out_of_range_rating_filter_is_a_validation_error(world):
    assert world["client"].get(URL, {"rating_min": 9}).status_code == 422


def test_cursor_pagination_walks_every_review_once(world):
    seen, cursor = [], None
    for _ in range(5):
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = world["client"].get(URL, params).json()
        seen.extend(r["google_review_id"] for r in body["results"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert seen == NEWEST_FIRST and cursor is None


# --- tenant isolation and roles ------------------------------------------------------------------------


def test_merchant_a_never_sees_merchant_bs_reviews(world):
    assert "b-secret" not in ids(world["client"].get(URL))
    foreign = world["client"].get(URL, {"location_id": str(world["other_location"].id)})
    assert ids(foreign) == []


def test_manager_sees_only_assigned_locations_and_an_unassigned_or_foreign_filter_is_an_empty_200(world):
    manager = add_member(world["owner"].merchant, TeamMember.Role.MANAGER, "manager@example.com")
    with tenant_context(world["owner"].merchant_id):
        account_services.set_team_member_locations(
            actor=world["owner"], member_id=manager.id, location_ids=[world["locations"][0].id]
        )
    client = world["session_client"](manager.user.email)

    assert ids(client.get(URL)) == ["r3", "r2", "r1"]
    unassigned = client.get(URL, {"location_id": str(world["locations"][1].id)})
    foreign = client.get(URL, {"location_id": str(world["other_location"].id)})
    assert ids(unassigned) == [] and unassigned.json()["next_cursor"] is None
    assert ids(foreign) == []


def test_viewer_may_read_every_review(world):
    viewer = add_member(world["owner"].merchant, TeamMember.Role.VIEWER, "viewer@example.com")
    assert ids(world["session_client"](viewer.user.email).get(URL)) == NEWEST_FIRST


def test_unauthenticated_requests_are_refused(world):
    assert APIClient().get(URL).status_code in (401, 403)


# --- API key (OD-6) ---------------------------------------------------------------------------------------


def bearer(raw):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def test_a_key_with_reviews_read_gets_every_review_of_its_merchant_only(world):
    _, raw = make_key(world["owner"], ["reviews:read"])
    _, other_raw = make_key(world["other"], ["reviews:read"])

    mine = APIClient().get(URL, **bearer(raw))
    theirs = APIClient().get(URL, **bearer(other_raw))

    assert ids(mine) == NEWEST_FIRST  # merchant-wide, no MANAGER scoping, nothing of B's
    assert ids(theirs) == ["b-secret"]


def test_a_valid_key_without_reviews_read_is_403(world):
    _, raw = make_key(world["owner"], ["sales:write"])
    assert APIClient().get(URL, **bearer(raw)).status_code == 403


def test_an_invalid_key_is_401_with_no_session_fallback_even_with_a_valid_session(world):
    response = world["client"].get(URL, **bearer("rf_live_this-key-does-not-exist"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_a_session_only_request_without_a_key_still_works_for_the_same_client(world):
    assert ids(world["client"].get(URL)) == NEWEST_FIRST
