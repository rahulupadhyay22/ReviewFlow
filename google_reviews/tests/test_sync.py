"""Review sync (spec 09 "Sync", "Review upsert", OD-8): idempotent upsert,
pagination, skips, rate limits and the Beat fan-out. Real PostgreSQL commits.
Upsert/guard logic runs against `fake_provider`; the Google -> row mapping and
pagination run the real provider against the fake HTTP boundary (`http`)."""
import inspect
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from django.utils import timezone

from core.tenancy import tenant_atomic, tenant_context
from google_reviews import services, tasks
from google_reviews.models import GoogleConnection, GoogleLocation, GoogleReview
from google_reviews.providers.base import (
    GooglePermanentError,
    GoogleRateLimited,
    GoogleTransientError,
    ProviderReview,
    ReviewPage,
)
from google_reviews.tests.conftest import GOOGLE_A, GOOGLE_B
from google_reviews.tests.helpers import (
    REVIEWS_BASE,
    Resp,
    fixture_json,
    google_settings,  # noqa: F401
    http,  # noqa: F401
)

pytestmark = pytest.mark.django_db(transaction=True)

WHEN = datetime(2026, 9, 1, 10, 0, tzinfo=dt_timezone.utc)


@pytest.fixture
def world(make_merchant, make_location, make_connection, make_mapping):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    connection = make_connection(owner.merchant)
    return {
        "owner": owner,
        "location": location,
        "connection": connection,
        "mapping": make_mapping(owner.merchant, location, connection, GOOGLE_A, reviews=0),
        "make_location": make_location,
        "make_mapping": make_mapping,
    }


def rev(review_id, rating=5, text="text", name="Reviewer"):
    return ProviderReview(review_id, name, rating, text, WHEN)


def page(*reviews, next_token=None, average="4.0", count=7):
    return ReviewPage(list(reviews), Decimal(average), count, next_token)


def sync(world, pk=None):
    with tenant_context(world["owner"].merchant_id):
        return services.sync_location_reviews(pk or world["mapping"].pk)


def sync_merchant(world):
    with tenant_context(world["owner"].merchant_id):
        services.sync_merchant_reviews()


def stored(world, mapping=None):
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        return list(
            GoogleReview.objects.filter(google_location_id=(mapping or world["mapping"]).pk)
            .order_by("google_review_id")
            .values("id", "google_review_id", "reviewer_name", "rating", "text", "created_at", "updated_at", "fetched_at")
        )


def mapping_row(world, mapping=None):
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        return GoogleLocation.objects.get(pk=(mapping or world["mapping"]).pk)


# --- upsert (fake_provider) -------------------------------------------------------------------


def test_syncing_the_same_page_twice_does_not_duplicate_reviews(world, fake_provider):
    fake_provider.pages[(GOOGLE_A, None)] = page(rev("r1"), rev("r2"))

    sync(world)
    sync(world)

    assert [r["google_review_id"] for r in stored(world)] == ["r1", "r2"]


def test_an_edited_review_updates_in_place_keeping_id_and_created_at(world, fake_provider):
    fake_provider.pages[(GOOGLE_A, None)] = page(rev("r1", rating=5, text="old"))
    sync(world)
    (before,) = stored(world)

    fake_provider.pages[(GOOGLE_A, None)] = page(rev("r1", rating=2, text="edited", name="New Name"))
    sync(world)

    (after,) = stored(world)
    assert (after["id"], after["created_at"]) == (before["id"], before["created_at"])
    assert (after["rating"], after["text"], after["reviewer_name"]) == (2, "edited", "New Name")
    assert after["fetched_at"] > before["fetched_at"] and after["updated_at"] > before["updated_at"]


def test_a_page_with_the_same_review_id_twice_upserts_once_without_error(world, fake_provider):
    fake_provider.pages[(GOOGLE_A, None)] = page(rev("dup", text="first"), rev("dup", text="second"), rev("other"))

    sync(world)

    rows = stored(world)
    assert [r["google_review_id"] for r in rows] == ["dup", "other"]


def test_a_review_missing_from_a_later_sync_is_kept(world, fake_provider):
    fake_provider.pages[(GOOGLE_A, None)] = page(rev("r1"), rev("r2"))
    sync(world)

    fake_provider.pages[(GOOGLE_A, None)] = page(rev("r2"))
    sync(world)

    assert [r["google_review_id"] for r in stored(world)] == ["r1", "r2"]


def test_counters_and_watermark_are_updated_after_a_successful_sync(world, fake_provider):
    fake_provider.pages[(GOOGLE_A, None)] = page(rev("r1"), average="4.3", count=11)
    before = timezone.now()

    sync(world)

    row = mapping_row(world)
    assert (row.average_rating, row.review_count) == (Decimal("4.3"), 11)
    assert row.last_synced_at >= before


def test_every_page_is_fetched_and_the_counters_come_from_the_first_page(world, fake_provider):
    fake_provider.pages[(GOOGLE_A, None)] = page(rev("r1"), next_token="t2", average="4.3", count=11)
    fake_provider.pages[(GOOGLE_A, "t2")] = page(rev("r2"), next_token="t3", average="1.0", count=1)
    fake_provider.pages[(GOOGLE_A, "t3")] = page(rev("r3"), average="1.0", count=1)

    sync(world)

    assert [r["google_review_id"] for r in stored(world)] == ["r1", "r2", "r3"]
    assert (mapping_row(world).average_rating, mapping_row(world).review_count) == (Decimal("4.3"), 11)


# --- when a location is (not) synced -----------------------------------------------------------------


def test_a_needs_reauth_connection_is_not_synced_and_no_provider_call_is_made(world, fake_provider):
    calls = []
    fake_provider.on_list_reviews = lambda parent, token: calls.append(parent)
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        GoogleConnection.objects.update(status="NEEDS_REAUTH")
        GoogleLocation.objects.update(last_synced_at=None)

    assert sync(world) is False
    sync_merchant(world)

    assert calls == [] and stored(world) == []


def test_an_unmapped_location_is_never_synced(world, fake_provider):
    calls = []
    fake_provider.on_list_reviews = lambda parent, token: calls.append(parent)
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        GoogleLocation.objects.all().delete()

    sync_merchant(world)

    assert calls == []


def test_only_due_locations_are_synced_by_the_merchant_run(world, fake_provider, settings):
    second = world["make_location"](world["owner"].merchant, "Second")
    other = world["make_mapping"](world["owner"].merchant, second, world["connection"], GOOGLE_B, reviews=0)
    calls = []
    fake_provider.on_list_reviews = lambda parent, token: calls.append(parent)
    long_ago = timezone.now() - timedelta(seconds=settings.GOOGLE_SYNC_INTERVAL_SECONDS + 60)
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        GoogleLocation.objects.filter(pk=world["mapping"].pk).update(last_synced_at=long_ago)
        GoogleLocation.objects.filter(pk=other.pk).update(last_synced_at=timezone.now())

    sync_merchant(world)

    assert calls == [GOOGLE_A]


# --- failures ---------------------------------------------------------------------------------------------


def test_a_rate_limit_stops_the_run_without_retry_and_keeps_the_watermark(world, fake_provider, settings):
    second = world["make_location"](world["owner"].merchant, "Second")
    other = world["make_mapping"](world["owner"].merchant, second, world["connection"], GOOGLE_B, reviews=0)
    long_ago = timezone.now() - timedelta(seconds=settings.GOOGLE_SYNC_INTERVAL_SECONDS + 60)
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        GoogleLocation.objects.update(last_synced_at=long_ago)
    calls = []

    def hook(parent, token):
        calls.append((parent, token))
        if token == "p2":
            raise GoogleRateLimited("limited")

    fake_provider.on_list_reviews = hook
    for parent in (GOOGLE_A, GOOGLE_B):
        fake_provider.pages[(parent, None)] = page(rev(f"{parent[-1]}-1"), next_token="p2")

    result = tasks.sync_merchant_google_reviews.apply(args=(str(world["owner"].merchant_id),))

    assert result.successful()
    assert len({parent for parent, _ in calls}) == 1 and len(calls) == 2  # the other location never ran
    watermarks = [mapping_row(world, m).last_synced_at for m in (world["mapping"], other)]
    assert watermarks == [long_ago, long_ago]
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        assert GoogleConnection.objects.get().status == "ACTIVE"


def test_a_retry_after_header_changes_nothing(world, http, monkeypatch):
    """G-12: Google documents no Retry-After, so a 429 carrying one still just
    stops the run: no sleep, no immediate retry, no scheduled retry, and the
    watermark is untouched."""
    import time

    from celery import Task

    long_ago = timezone.now() - timedelta(hours=3)
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        GoogleLocation.objects.update(last_synced_at=long_ago)
    first = dict(fixture_json("reviews.json"))
    first["nextPageToken"] = "tok2"

    def route(req):
        if parse_qs(urlparse(req.full_url).query).get("pageToken") == ["tok2"]:
            return Resp(429, fixture_json("rate_limit.json"), {"Retry-After": "30"})
        return first

    http.route(REVIEWS_BASE, route)

    def forbidden(*args, **kwargs):
        raise AssertionError("a rate limit must not sleep or schedule a retry")

    monkeypatch.setattr(time, "sleep", forbidden)
    monkeypatch.setattr(Task, "apply_async", forbidden)
    monkeypatch.setattr(Task, "retry", forbidden)

    assert sync(world) is True  # stopped quietly on the 429

    assert len(http.to(REVIEWS_BASE)) == 2  # page 1, then the one rate-limited call; no retry
    assert mapping_row(world).last_synced_at == long_ago


def test_the_next_run_resumes_after_a_rate_limit(world, fake_provider):
    state = {"limited": True}

    def hook(parent, token):
        if state["limited"]:
            raise GoogleRateLimited("limited")

    fake_provider.on_list_reviews = hook
    fake_provider.pages[(GOOGLE_A, None)] = page(rev("r1"))
    assert sync(world) is True
    assert stored(world) == []

    state["limited"] = False
    assert sync(world) is False

    assert [r["google_review_id"] for r in stored(world)] == ["r1"]


@pytest.mark.parametrize("error", [GoogleTransientError("boom"), GooglePermanentError("refused")], ids=["transient", "permanent"])
def test_a_failing_location_does_not_stop_the_others_and_never_causes_needs_reauth(world, fake_provider, settings, error):
    second = world["make_location"](world["owner"].merchant, "Second")
    other = world["make_mapping"](world["owner"].merchant, second, world["connection"], GOOGLE_B, reviews=0)
    long_ago = timezone.now() - timedelta(seconds=settings.GOOGLE_SYNC_INTERVAL_SECONDS + 60)
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        GoogleLocation.objects.update(last_synced_at=long_ago)

    def hook(parent, token):
        if parent == GOOGLE_A:
            raise error

    fake_provider.on_list_reviews = hook
    fake_provider.pages[(GOOGLE_B, None)] = page(rev("b-1"))

    sync_merchant(world)

    assert [r["google_review_id"] for r in stored(world, other)] == ["b-1"]
    assert mapping_row(world).last_synced_at == long_ago
    with tenant_context(world["owner"].merchant_id), tenant_atomic():
        assert GoogleConnection.objects.get().status == "ACTIVE"


# --- real provider: mapping and pagination -----------------------------------------------------------------


def test_google_reviews_are_mapped_and_every_page_is_followed_with_the_stored_parent_verbatim(
    world, http, google_settings
):
    first = fixture_json("reviews.json")
    first["nextPageToken"] = "tok2"
    last = {
        "reviews": [
            {"reviewId": "TEST-REVIEW-ID-5", "reviewer": {"displayName": "Five", "isAnonymous": False},
             "starRating": "FIVE", "comment": "Last page.", "createTime": "2026-09-05T10:00:00Z"}
        ],
        "averageRating": 1.0,
        "totalReviewCount": 1,
    }  # fmt: skip

    def route(req):
        token = parse_qs(urlparse(req.full_url).query).get("pageToken")
        return last if token == ["tok2"] else first

    http.route(REVIEWS_BASE, route)

    sync(world)

    rows = {r["google_review_id"]: r for r in stored(world)}
    assert sorted(rows) == ["TEST-REVIEW-ID-1", "TEST-REVIEW-ID-2", "TEST-REVIEW-ID-4", "TEST-REVIEW-ID-5"]
    assert "TEST-REVIEW-ID-3" not in rows  # STAR_RATING_UNSPECIFIED is skipped, not stored
    assert [rows[k]["rating"] for k in sorted(rows)] == [5, 1, 3, 5]
    assert rows["TEST-REVIEW-ID-2"]["reviewer_name"] is None  # anonymous
    assert rows["TEST-REVIEW-ID-1"]["reviewer_name"] == "Test Reviewer One"
    assert rows["TEST-REVIEW-ID-2"]["text"] is None  # absent comment
    row = mapping_row(world)
    assert (row.average_rating, row.review_count) == (Decimal("3.5"), 4)  # from the first page
    urls = [c.url for c in http.to(REVIEWS_BASE)]
    assert len(urls) == 2
    assert all(u.startswith(f"{REVIEWS_BASE}{GOOGLE_A}/reviews?") for u in urls)  # the parent, verbatim
    assert "pageToken=tok2" in urls[1] and "pageToken" not in urls[0]
    assert all(c.in_atomic is False and c.method == "GET" for c in http.to(REVIEWS_BASE))


def test_no_reply_field_is_stored_or_exposed():
    names = {f.name for f in GoogleReview._meta.get_fields()}
    assert not ({"reply_text", "reply_status", "reviewReply", "reply"} & names)


# --- tasks, queues, fan-out -----------------------------------------------------------------------------------


def test_every_google_task_runs_on_the_google_sync_queue_and_tenant_tasks_take_merchant_id_first():
    all_tasks = [
        tasks.sync_google_reviews,
        tasks.sync_merchant_google_reviews,
        tasks.sync_location,
        tasks.refresh_google_tokens,
        tasks.refresh_merchant_google_tokens,
        tasks.exchange_oauth_code,
        tasks.refresh_connection_token,
    ]
    assert {t.queue for t in all_tasks} == {"google_sync"}
    for task in (t for t in all_tasks if t not in (tasks.sync_google_reviews, tasks.refresh_google_tokens)):
        assert list(inspect.signature(task.run).parameters)[0] == "merchant_id"
        assert task.max_retries == 0
    assert tasks.exchange_oauth_code.max_retries == 0


def test_beat_entries_exist_on_the_google_sync_queue(settings):
    schedule = settings.CELERY_BEAT_SCHEDULE
    assert schedule["google-sync-reviews"]["task"] == "google_reviews.tasks.sync_google_reviews"
    assert schedule["google-refresh-tokens"]["task"] == "google_reviews.tasks.refresh_google_tokens"
    assert schedule["google-refresh-tokens"]["schedule"] == 86400
    assert schedule["google-sync-reviews"]["schedule"] == settings.GOOGLE_SYNC_BEAT_SECONDS
    for name in ("google-sync-reviews", "google-refresh-tokens"):
        assert schedule[name]["options"]["queue"] == "google_sync"


@pytest.mark.parametrize(
    "beat,tenant",
    [("sync_google_reviews", "sync_merchant_google_reviews"), ("refresh_google_tokens", "refresh_merchant_google_tokens")],
)
def test_the_beat_fan_out_enqueues_one_task_per_merchant_with_an_explicit_merchant_id(make_merchant, monkeypatch, beat, tenant):
    a, b = make_merchant("A"), make_merchant("B")
    sent = []
    monkeypatch.setattr(getattr(tasks, tenant), "delay", lambda *args: sent.append(args))

    getattr(tasks, beat)()

    assert sent.count((str(a.merchant_id),)) == 1 and sent.count((str(b.merchant_id),)) == 1
    assert all(len(args) == 1 for args in sent)
