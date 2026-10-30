"""Remapping a location (spec RM-1) and the sync identity guard. Real PostgreSQL
with real commits and row locks. The only mock is the Google boundary."""
import threading
import time
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

import pytest
from django.db import connection

from core.tenancy import tenant_atomic, tenant_context
from google_reviews import services
from google_reviews.models import GoogleLocation, GoogleReview
from google_reviews.providers.base import ProviderReview, ReviewPage
from google_reviews.tests.conftest import GOOGLE_A, GOOGLE_B

pytestmark = pytest.mark.django_db(transaction=True)

GOOGLE_C = "accounts/1/locations/C"
WHEN = datetime(2026, 10, 1, tzinfo=dt_timezone.utc)


@pytest.fixture
def world(make_merchant, make_location, make_connection, make_mapping, session_client):
    owner = make_merchant("A")
    connection_ = make_connection(owner.merchant)
    first, second = make_location(owner.merchant, "First"), make_location(owner.merchant, "Second")
    return {
        "owner": owner,
        "client": session_client(owner.user.email),
        "connection": connection_,
        "location": first,
        "mapping": make_mapping(owner.merchant, first, connection_, GOOGLE_A, reviews=2),
        "other_location": second,
        "other_mapping": make_mapping(owner.merchant, second, connection_, GOOGLE_C, reviews=2),
    }


def put(world, google_location_id, client=None, location=None, connection_id=None):
    client = client or world["client"]
    location = location or world["location"]
    return client.put(
        f"/api/v1/locations/{location.id}/google-location",
        {"google_connection_id": str(connection_id or world["connection"].id), "google_location_id": google_location_id},
        format="json",
        HTTP_X_CSRFTOKEN=client.csrf,
    )


def remap(world, google_location_id):
    with tenant_context(world["owner"].merchant_id):
        return services.map_location(
            team_member=world["owner"],
            location_id=world["location"].id,
            connection_id=world["connection"].id,
            google_location_id=google_location_id,
        )


def review_ids(world, mapping):
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        return sorted(GoogleReview.objects.filter(google_location_id=mapping.pk).values_list("google_review_id", flat=True))


def mapping_row(world, mapping):
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        return GoogleLocation.objects.get(pk=mapping.pk)


def sync(world):
    with tenant_context(world["owner"].merchant_id):
        return services.sync_location_reviews(world["mapping"].pk)


def page(*ids, next_token=None, average="3.0", count=9):
    reviews = [ProviderReview(i, "Reviewer", 4, "text", WHEN) for i in ids]
    return ReviewPage(reviews, Decimal(average), count, next_token)


def test_a_remap_deletes_only_the_old_reviews_and_resets_the_counters(world, fake_provider, sync_enqueues):
    response = put(world, GOOGLE_B)

    assert response.status_code == 200, response.content
    row = mapping_row(world, world["mapping"])
    assert (row.google_location_id, row.google_place_id) == (GOOGLE_B, "place-B")
    assert row.review_link == "https://g.page/r/B/review"
    assert (row.average_rating, row.review_count, row.last_synced_at) == (None, None, None)
    assert review_ids(world, world["mapping"]) == []
    assert len(review_ids(world, world["other_mapping"])) == 2
    other = mapping_row(world, world["other_mapping"])
    assert (other.average_rating, other.review_count) == (Decimal("4.5"), 2)
    # one sync, registered after the commit
    assert sync_enqueues == [((str(world["owner"].merchant_id), str(world["mapping"].pk)), False)]


def test_the_next_sync_leaves_only_the_new_locations_reviews(world, fake_provider, sync_enqueues):
    put(world, GOOGLE_B)
    fake_provider.pages[(GOOGLE_B, None)] = page("B-new-1")

    sync(world)

    assert review_ids(world, world["mapping"]) == ["B-new-1"]
    listed = world["client"].get("/api/v1/reviews", {"location_id": str(world["location"].id)}).json()["results"]
    assert [r["google_review_id"] for r in listed] == ["B-new-1"]


def test_a_put_with_the_same_id_keeps_reviews_and_counters(world, fake_provider, sync_enqueues):
    before = mapping_row(world, world["mapping"])

    response = put(world, GOOGLE_A)

    assert response.status_code == 200, response.content
    after = mapping_row(world, world["mapping"])
    assert (after.average_rating, after.review_count, after.last_synced_at) == (
        before.average_rating,
        before.review_count,
        before.last_synced_at,
    )
    assert len(review_ids(world, world["mapping"])) == 2


def test_a_failure_after_the_delete_rolls_everything_back(world, fake_provider, monkeypatch):
    def boom(*args):
        raise RuntimeError("enqueue failed inside the block")

    monkeypatch.setattr(services, "_enqueue_on_commit", boom)

    with pytest.raises(RuntimeError):
        remap(world, GOOGLE_B)

    row = mapping_row(world, world["mapping"])
    assert (row.google_location_id, row.average_rating, row.review_count) == (GOOGLE_A, Decimal("4.5"), 2)
    assert len(review_ids(world, world["mapping"])) == 2


def test_a_sync_that_is_mid_page_when_the_remap_commits_writes_nothing_after(world, fake_provider, sync_enqueues):
    fake_provider.pages[(GOOGLE_A, None)] = page("A-new-1", next_token="t2")
    fake_provider.pages[(GOOGLE_A, "t2")] = page("A-new-2")

    def remap_during_page_two(google_location_id, page_token):
        if page_token == "t2":
            remap(world, GOOGLE_B)

    fake_provider.on_list_reviews = remap_during_page_two

    assert sync(world) is False

    row = mapping_row(world, world["mapping"])
    assert row.google_location_id == GOOGLE_B
    assert review_ids(world, world["mapping"]) == []
    assert (row.average_rating, row.review_count, row.last_synced_at) == (None, None, None)


def test_the_counters_update_is_skipped_when_the_mapping_changed(world, fake_provider, sync_enqueues, monkeypatch):
    fake_provider.pages[(GOOGLE_A, None)] = page("A-new-1")
    real_upsert = services._upsert_reviews

    def upsert_then_remap(*args):
        result = real_upsert(*args)
        remap(world, GOOGLE_B)  # commits between the last page write and the counters
        return result

    monkeypatch.setattr(services, "_upsert_reviews", upsert_then_remap)

    sync(world)

    row = mapping_row(world, world["mapping"])
    assert row.google_location_id == GOOGLE_B
    assert (row.average_rating, row.review_count, row.last_synced_at) == (None, None, None)


def test_remap_and_a_stale_page_write_serialize_on_the_mapping_row_lock(world, fake_provider, sync_enqueues, spawn, monkeypatch):
    held, release, upsert_pid_known = threading.Event(), threading.Event(), threading.Event()
    pids = []
    merchant_id = world["owner"].merchant_id

    def hold_the_lock_inside_block_b(*args):
        held.set()  # the mapping row is locked and written, not yet committed
        assert release.wait(30)

    monkeypatch.setattr(services, "_enqueue_on_commit", hold_the_lock_inside_block_b)
    remapping = spawn(lambda: remap(world, GOOGLE_B), merchant_id)
    assert held.wait(30), "the remap never reached the lock"

    def stale_upsert():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            pids.append(cursor.fetchone()[0])
        upsert_pid_known.set()
        return services._upsert_reviews(
            world["mapping"].pk, [ProviderReview("A-late", None, 5, "late", WHEN)], GOOGLE_A
        )

    upserting = spawn(stale_upsert, merchant_id)
    try:
        assert upsert_pid_known.wait(30)
        deadline = time.monotonic() + 30
        while True:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid = %s AND wait_event_type = 'Lock'", [pids[0]]
                )
                if cursor.fetchone()[0] == 1:
                    break
            assert time.monotonic() < deadline, "the stale write was never observed waiting on the row lock"
            time.sleep(0.01)  # a poll interval, not a synchronization delay
    finally:
        release.set()
    remapping.join()

    assert upserting.join() is False  # re-checked the identity after the lock was released
    assert review_ids(world, world["mapping"]) == []
    assert mapping_row(world, world["mapping"]).google_location_id == GOOGLE_B


def test_another_merchants_remap_attempt_deletes_nothing(world, make_merchant, make_connection, session_client, fake_provider, sync_enqueues):
    intruder = make_merchant("B")
    client = session_client(intruder.user.email)
    foreign_connection = make_connection(intruder.merchant, sub="sub-2")

    response = put(world, GOOGLE_B, client=client, connection_id=foreign_connection.id)

    assert response.status_code == 404
    assert mapping_row(world, world["mapping"]).google_location_id == GOOGLE_A
    assert len(review_ids(world, world["mapping"])) == 2
    assert sync_enqueues == []
