"""Two concurrent FIRST mappings of the same Location (code review, 2026-10-30).
With no row to lock, both PUTs see `mapping is None` and both INSERT; the loser
violates the OneToOne unique index on location_id and must answer 409
google_location_already_mapped, not 500. Real PostgreSQL: two threads, each with
its own connection; only the Google boundary is mocked."""
import threading
import time

import pytest
from django.db import IntegrityError, connection

from core.tenancy import tenant_atomic, tenant_context
from google_reviews import services
from google_reviews.exceptions import GoogleLocationAlreadyMapped
from google_reviews.models import GoogleLocation
from google_reviews.tests.conftest import GOOGLE_A, GOOGLE_B

pytestmark = pytest.mark.django_db(transaction=True)

ONE_TO_ONE = "google_reviews_googlelocation_location_id_key"


@pytest.fixture
def world(make_merchant, make_location, make_connection, fake_provider, sync_enqueues):
    owner = make_merchant("A")
    return {
        "owner": owner,
        "location": make_location(owner.merchant),
        "connection": make_connection(owner.merchant),
    }


def first_mapping(world, google_location_id):
    return services.map_location(
        team_member=world["owner"],
        location_id=world["location"].id,
        connection_id=world["connection"].id,
        google_location_id=google_location_id,
    )


def rows(world):
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        return list(GoogleLocation.objects.values("pk", "google_location_id", "google_place_id", "review_link"))


def test_the_constraint_name_is_the_one_the_database_generated():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'google_reviews_googlelocation'::regclass "
            "AND contype = 'u' AND conkey = ARRAY[(SELECT attnum FROM pg_attribute "
            "WHERE attrelid = 'google_reviews_googlelocation'::regclass AND attname = 'location_id')]"
        )
        assert [name for (name,) in cursor.fetchall()] == [ONE_TO_ONE]
    assert services._LOCATION_ONE_TO_ONE == ONE_TO_ONE


def test_the_loser_of_two_concurrent_first_mappings_gets_409_and_the_winner_is_untouched(
    world, spawn, sync_enqueues, monkeypatch
):
    held, release, b_pid_known = threading.Event(), threading.Event(), threading.Event()
    b_pids = []
    real_enqueue = services._enqueue_on_commit
    merchant_id = world["owner"].merchant_id

    def hold_after_the_insert(*args):
        held.set()  # A's INSERT is done and uncommitted; B has not inserted yet
        assert release.wait(30)
        return real_enqueue(*args)

    monkeypatch.setattr(services, "_enqueue_on_commit", hold_after_the_insert)
    winner = spawn(lambda: first_mapping(world, GOOGLE_A), merchant_id)
    assert held.wait(30), "the first request never inserted"

    def loser_request():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            b_pids.append(cursor.fetchone()[0])
        b_pid_known.set()
        return first_mapping(world, GOOGLE_B)

    loser = spawn(loser_request, merchant_id)
    try:
        assert b_pid_known.wait(30)
        deadline = time.monotonic() + 30
        while True:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid = %s AND wait_event_type = 'Lock'", [b_pids[0]]
                )
                if cursor.fetchone()[0] == 1:
                    break
            assert time.monotonic() < deadline, "the second INSERT was never observed waiting on the unique index"
            time.sleep(0.01)  # a poll interval, not a synchronization delay
    finally:
        release.set()

    assert winner.join().google_location_id == GOOGLE_A
    loser.thread.join(30)
    assert isinstance(loser.error, GoogleLocationAlreadyMapped)  # 409 google_location_already_mapped

    stored = rows(world)
    assert len(stored) == 1  # exactly one mapping, and it is the winner's, unchanged
    assert (stored[0]["google_location_id"], stored[0]["google_place_id"]) == (GOOGLE_A, "place-A")
    assert stored[0]["review_link"] == "https://g.page/r/A/review"
    # one sync, from the winner only: the loser's rollback discarded its callback
    assert sync_enqueues == [((str(merchant_id), str(stored[0]["pk"])), False)]


def test_an_unrelated_integrity_error_still_propagates(world, monkeypatch):
    def unrelated(*args, **kwargs):
        raise IntegrityError("some other constraint")

    monkeypatch.setattr(services, "_write_mapping", unrelated)

    with tenant_context(world["owner"].merchant_id):
        with pytest.raises(IntegrityError):
            first_mapping(world, GOOGLE_A)

    assert rows(world) == []
