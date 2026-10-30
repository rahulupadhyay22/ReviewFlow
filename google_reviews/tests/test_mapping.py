"""Mapping a Location to a Google location and the review link (spec 09
"Mapping & review link", RL-1, OD-3, OD-7, OD-12, P-4). Real PostgreSQL commits.
Google is replaced at the provider boundary (`fake_provider`); the review link
rules are exercised on the real validate_review_link."""
import re
from pathlib import Path

import pytest

from accounts import services as account_services
from accounts.models import TeamMember
from core.tenancy import tenant_atomic, tenant_context
from google_reviews import services
from google_reviews.exceptions import GoogleLocationNotAvailable, ReviewLinkMissing
from google_reviews.models import GoogleConnection, GoogleLocation
from google_reviews.providers.base import ProviderLocation
from google_reviews.tests.conftest import ACCESS_TOKEN, GOOGLE_A, GOOGLE_B
from google_reviews.tests.helpers import add_member

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def world(make_merchant, make_location, make_connection, session_client):
    owner = make_merchant("A")
    return {
        "owner": owner,
        "client": session_client(owner.user.email),
        "location": make_location(owner.merchant, "First"),
        "second": make_location(owner.merchant, "Second"),
        "connection": make_connection(owner.merchant),
    }


def put(world, google_location_id=GOOGLE_A, client=None, location=None, **extra):
    client = client or world["client"]
    location = location or world["location"]
    return client.put(
        f"/api/v1/locations/{location.id}/google-location",
        {"google_connection_id": str(world["connection"].id), "google_location_id": google_location_id, **extra},
        format="json",
        HTTP_X_CSRFTOKEN=client.csrf,
    )


def mappings(world):
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        return list(GoogleLocation.objects.order_by("created_at"))


# --- validate_review_link ----------------------------------------------------------------


@pytest.mark.parametrize("uri", ["https://g.page/r/ABC/review", "https://search.google.com/local/x?id=1&b=2"])
def test_validate_review_link_returns_a_good_link_unchanged(uri):
    assert services.validate_review_link(uri) == uri


@pytest.mark.parametrize(
    "uri",
    [
        None,
        "",
        123,
        "http://g.page/r/ABC/review",
        "ftp://g.page/r/ABC",
        "//g.page/r/ABC",
        "/relative/path",
        "https:///nohost",
        "https://user:pw@g.page/r/ABC",
        "https://g.page/r/ABC#fragment",
        " https://g.page/r/ABC",
        "https://g.page/r/AB C",
        "https://g.page/" + "a" * 2048,
    ],
)
def test_validate_review_link_refuses_everything_else(uri):
    with pytest.raises(ReviewLinkMissing):
        services.validate_review_link(uri)


def test_a_review_link_of_exactly_2048_characters_is_accepted():
    uri = "https://g.page/" + "a" * (2048 - len("https://g.page/"))
    assert services.validate_review_link(uri) == uri


def test_no_code_path_builds_a_review_url_from_an_identifier():
    root = Path(services.__file__).parent
    needle = "write" + "review"
    hits = [
        str(p)
        for p in root.rglob("*.py")
        if "tests" not in p.relative_to(root).parts and needle in p.read_text(encoding="utf-8").lower()
    ]
    assert hits == []


# --- the mapping PUT --------------------------------------------------------------------------


def test_mapping_stores_three_separate_values_straight_from_the_provider(world, fake_provider, sync_enqueues):
    response = put(world, GOOGLE_A)

    assert response.status_code == 200, response.content
    (row,) = mappings(world)
    assert re.fullmatch(r"accounts/[^/]+/locations/[^/]+", row.google_location_id)
    assert (row.google_location_id, row.google_place_id, row.review_link) == (
        GOOGLE_A,
        "place-A",
        "https://g.page/r/A/review",
    )
    assert len({row.google_location_id, row.google_place_id, row.review_link}) == 3
    assert row.google_connection_id == world["connection"].id
    body = response.json()
    assert body["google_location_id"] == GOOGLE_A and body["review_link"] == "https://g.page/r/A/review"
    assert body["connection_status"] == "ACTIVE"
    assert sync_enqueues == [((str(world["owner"].merchant_id), str(row.pk)), False)]


def test_a_client_supplied_place_id_or_link_is_ignored(world, fake_provider, sync_enqueues):
    response = put(world, GOOGLE_A, google_place_id="client-place", review_link="https://evil.example/review")

    assert response.status_code == 200
    (row,) = mappings(world)
    assert (row.google_place_id, row.review_link) == ("place-A", "https://g.page/r/A/review")


def test_a_location_without_a_place_id_is_422_and_nothing_is_written(world, fake_provider, sync_enqueues):
    fake_provider.locations = [ProviderLocation(GOOGLE_A, "T", None, "https://g.page/r/A/review")]

    response = put(world, GOOGLE_A)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "google_place_id_missing"
    assert mappings(world) == [] and sync_enqueues == []


@pytest.mark.parametrize(
    "uri",
    [
        None,
        "http://g.page/r/A/review",
        "https://user:pw@g.page/r/A/review",
        "https://g.page/r/A/review#frag",
        "https://g.page/" + "a" * 2050,
    ],
    ids=["absent", "http", "userinfo", "fragment", "too-long"],
)
def test_a_missing_or_unusable_review_uri_is_422_and_nothing_is_written(world, fake_provider, sync_enqueues, uri):
    fake_provider.locations = [ProviderLocation(GOOGLE_A, "T", "place-A", uri)]

    response = put(world, GOOGLE_A)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "google_review_link_missing"
    assert mappings(world) == [] and sync_enqueues == []


def test_a_second_location_cannot_map_to_the_same_google_location(world, fake_provider, sync_enqueues, make_mapping):
    first = make_mapping(world["owner"].merchant, world["location"], world["connection"], GOOGLE_A, reviews=1)

    response = put(world, GOOGLE_A, location=world["second"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "google_location_already_mapped"
    (row,) = mappings(world)
    assert row.pk == first.pk
    assert sync_enqueues == []


def test_an_unknown_google_location_and_another_merchants_one_get_identical_422_bytes(world, fake_provider, sync_enqueues):
    unknown = put(world, "accounts/1/locations/does-not-exist")
    elsewhere = put(world, "accounts/2/locations/only-another-merchants-connection-lists-this")

    assert unknown.status_code == elsewhere.status_code == 422
    assert unknown.json()["error"]["code"] == "google_location_not_available"
    assert unknown.content == elsewhere.content
    assert mappings(world) == [] and sync_enqueues == []


def test_the_three_way_merchant_invariant_rejects_before_any_write(world, make_merchant, make_connection, make_location):
    other = make_merchant("B")
    foreign_connection = make_connection(other.merchant)
    foreign_location = make_location(other.merchant)

    with tenant_context(world["owner"].merchant_id):
        for location, connection_ in (
            (world["location"], foreign_connection),
            (foreign_location, world["connection"]),
        ):
            with pytest.raises(GoogleLocationNotAvailable) as raised:
                services._assert_same_merchant(location, connection_)
            assert raised.value.code == "google_location_not_available" and raised.value.http_status == 422
    assert mappings(world) == []


def test_another_merchants_connection_or_location_is_404_and_writes_nothing(
    world, fake_provider, sync_enqueues, make_merchant, make_connection, make_location
):
    other = make_merchant("B")
    foreign_connection = make_connection(other.merchant)
    foreign_location = make_location(other.merchant)
    client = world["client"]

    wrong_connection = client.put(
        f"/api/v1/locations/{world['location'].id}/google-location",
        {"google_connection_id": str(foreign_connection.id), "google_location_id": GOOGLE_A},
        format="json",
        HTTP_X_CSRFTOKEN=client.csrf,
    )
    wrong_location = put(world, GOOGLE_A, location=foreign_location)

    assert wrong_connection.status_code == 404 and wrong_location.status_code == 404
    assert mappings(world) == [] and sync_enqueues == []


def test_a_failure_in_the_write_block_rolls_back_and_enqueues_nothing(world, fake_provider, sync_enqueues, monkeypatch):
    real = services._write_mapping

    def write_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("block B fails after writing")

    monkeypatch.setattr(services, "_write_mapping", write_then_fail)
    world["client"].raise_request_exception = False

    response = put(world, GOOGLE_A)

    assert response.status_code >= 500
    assert mappings(world) == [] and sync_enqueues == []


def test_a_needs_reauth_connection_is_409_on_the_put_and_the_list(world, fake_provider, sync_enqueues):
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        GoogleConnection.objects.update(status="NEEDS_REAUTH")

    mapped = put(world, GOOGLE_A)
    listed = world["client"].get(f"/api/v1/google/connections/{world['connection'].id}/locations")

    assert mapped.status_code == listed.status_code == 409
    assert mapped.json()["error"]["code"] == listed.json()["error"]["code"] == "google_needs_reauth"
    assert mappings(world) == [] and fake_provider.list_in_atomic == []


# --- roles ------------------------------------------------------------------------------------------


def test_manager_may_map_an_assigned_location_but_gets_404_for_an_unassigned_one(
    world, fake_provider, sync_enqueues, session_client
):
    manager = add_member(world["owner"].merchant, TeamMember.Role.MANAGER, "manager@example.com")
    with tenant_context(world["owner"].merchant_id):
        account_services.set_team_member_locations(
            actor=world["owner"], member_id=manager.id, location_ids=[world["location"].id]
        )
    client = session_client(manager.user.email)

    assigned = put(world, GOOGLE_A, client=client, location=world["location"])
    unassigned = put(world, GOOGLE_B, client=client, location=world["second"])
    mapped_view = client.get(f"/api/v1/locations/{world['second'].id}/google-location")

    assert assigned.status_code == 200
    assert unassigned.status_code == 404 and mapped_view.status_code == 404
    assert [m.location_id for m in mappings(world)] == [world["location"].id]


def test_viewer_gets_403_on_the_put_and_the_location_list_but_may_read_the_mapping(
    world, fake_provider, sync_enqueues, session_client
):
    viewer = add_member(world["owner"].merchant, TeamMember.Role.VIEWER, "viewer@example.com")
    client = session_client(viewer.user.email)

    assert put(world, GOOGLE_A, client=client).status_code == 403
    assert client.get(f"/api/v1/google/connections/{world['connection'].id}/locations").status_code == 403
    assert client.get(f"/api/v1/locations/{world['location'].id}/google-location").status_code == 200
    assert mappings(world) == []


# --- the read endpoints --------------------------------------------------------------------------------


def test_get_mapping_returns_null_when_unmapped_and_the_documented_fields_when_mapped(world, fake_provider, sync_enqueues):
    url = f"/api/v1/locations/{world['location'].id}/google-location"
    assert world["client"].get(url).json() == {"google_location": None}

    put(world, GOOGLE_A)
    body = world["client"].get(url).json()["google_location"]

    assert set(body) == {
        "id", "google_location_id", "google_place_id", "review_link", "average_rating",
        "review_count", "last_synced_at", "connection_status",
    }  # fmt: skip
    assert body["connection_status"] == "ACTIVE"


def test_the_location_list_reports_place_id_link_and_current_mapping(world, fake_provider, sync_enqueues, make_mapping):
    make_mapping(world["owner"].merchant, world["location"], world["connection"], GOOGLE_A, reviews=0)
    fake_provider.locations = [
        ProviderLocation(GOOGLE_A, "Title A", "place-A", "https://g.page/r/A/review"),
        ProviderLocation(GOOGLE_B, "Title B", None, None),
    ]

    response = world["client"].get(f"/api/v1/google/connections/{world['connection'].id}/locations")

    assert response.status_code == 200
    assert response.json()["results"] == [
        {
            "google_location_id": GOOGLE_A,
            "title": "Title A",
            "has_place_id": True,
            "has_review_link": True,
            "mapped_location_id": str(world["location"].id),
        },
        {
            "google_location_id": GOOGLE_B,
            "title": "Title B",
            "has_place_id": False,
            "has_review_link": False,
            "mapped_location_id": None,
        },
    ]
    assert ACCESS_TOKEN not in response.content.decode()
