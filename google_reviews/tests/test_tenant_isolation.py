"""Cross-merchant access: API (IDOR on UUIDs) and Celery tasks (Multi-Tenancy.md
test 3: a task carrying merchant A cannot touch B's rows). Google is mocked only
at the HTTP boundary (`http`) or the provider (`fake_provider`)."""
from datetime import timedelta

import pytest
from django.utils import timezone

from core.tenancy import tenant_atomic, tenant_context
from google_reviews import tasks
from google_reviews.models import GoogleConnection, GoogleLocation, GoogleReview
from google_reviews.providers.base import ProviderReview, ReviewPage
from google_reviews.tests.conftest import GOOGLE_A, GOOGLE_B
from google_reviews.tests.helpers import enqueues, google_settings, http, make_state  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def worlds(make_merchant, make_location, make_connection, make_mapping, session_client):
    out = {}
    for name in ("A", "B"):
        owner = make_merchant(name)
        location = make_location(owner.merchant)
        conn = make_connection(owner.merchant)
        out[name] = {
            "owner": owner,
            "client": session_client(owner.user.email),
            "location": location,
            "connection": conn,
            "mapping": make_mapping(owner.merchant, location, conn, GOOGLE_A, reviews=2),
            "state": make_state(owner),
        }
    return out


def put(world, location, connection_id, google_location_id=GOOGLE_A):
    client = world["client"]
    return client.put(
        f"/api/v1/locations/{location.id}/google-location",
        {"google_connection_id": str(connection_id), "google_location_id": google_location_id},
        format="json",
        HTTP_X_CSRFTOKEN=client.csrf,
    )


def test_merchant_a_cannot_read_merchant_bs_oauth_attempt(worlds):
    response = worlds["A"]["client"].get(f"/api/v1/google/oauth/attempts/{worlds['B']['state'].pk}")
    assert response.status_code == 404


def test_merchant_a_cannot_read_merchant_bs_mapping(worlds):
    response = worlds["A"]["client"].get(f"/api/v1/locations/{worlds['B']['location'].id}/google-location")
    assert response.status_code == 404


def test_merchant_a_cannot_map_bs_location_or_use_bs_connection(worlds, fake_provider, sync_enqueues):
    a, b = worlds["A"], worlds["B"]
    other = b["location"]

    assert put(a, other, a["connection"].id).status_code == 404
    assert put(a, a["location"], b["connection"].id, GOOGLE_B).status_code == 404

    with tenant_context(b["owner"].merchant_id), tenant_atomic():
        assert GoogleLocation.objects.get(location=other).google_connection_id == b["connection"].id
    assert sync_enqueues == []


def test_merchant_a_cannot_list_locations_through_bs_connection(worlds, fake_provider):
    response = worlds["A"]["client"].get(f"/api/v1/google/connections/{worlds['B']['connection'].id}/locations")
    assert response.status_code == 404
    assert fake_provider.list_in_atomic == []


def test_connections_list_contains_only_the_callers_connections(worlds):
    body = worlds["A"]["client"].get("/api/v1/google/connections").json()
    assert [row["id"] for row in body["results"]] == [str(worlds["A"]["connection"].id)]


def test_a_task_for_merchant_a_given_bs_location_reads_and_writes_nothing(worlds, fake_provider):
    a, b = worlds["A"], worlds["B"]
    seen = []
    fake_provider.on_list_reviews = lambda parent, token: seen.append(parent)
    fake_provider.pages[(GOOGLE_A, None)] = ReviewPage(
        [ProviderReview("INTRUDER", "x", 5, "t", timezone.now())], None, 9, None
    )
    with tenant_context(b["owner"].merchant_id), tenant_atomic():
        before = GoogleLocation.objects.values("average_rating", "review_count", "last_synced_at").get(pk=b["mapping"].pk)

    tasks.sync_location.delay(str(a["owner"].merchant_id), str(b["mapping"].pk))

    assert seen == []
    with tenant_context(b["owner"].merchant_id), tenant_atomic():
        after = GoogleLocation.objects.values("average_rating", "review_count", "last_synced_at").get(pk=b["mapping"].pk)
        assert after == before
        assert not GoogleReview.objects.filter(google_review_id="INTRUDER").exists()


def test_a_merchant_wide_sync_for_a_leaves_bs_due_location_alone(worlds, fake_provider):
    a, b = worlds["A"], worlds["B"]
    old = timezone.now() - timedelta(days=1)
    for world in (a, b):
        with tenant_context(world["owner"].merchant_id), tenant_atomic():
            GoogleLocation.objects.filter(pk=world["mapping"].pk).update(last_synced_at=old)
    fake_provider.pages[(GOOGLE_A, None)] = ReviewPage([ProviderReview("NEW-1", "x", 5, "t", timezone.now())], None, 3, None)

    tasks.sync_merchant_google_reviews.delay(str(a["owner"].merchant_id))

    with tenant_context(a["owner"].merchant_id), tenant_atomic():
        assert GoogleReview.objects.filter(google_review_id="NEW-1").exists()
        assert GoogleLocation.objects.get(pk=a["mapping"].pk).last_synced_at > old
    with tenant_context(b["owner"].merchant_id), tenant_atomic():
        assert not GoogleReview.objects.filter(google_review_id="NEW-1").exists()
        assert GoogleLocation.objects.get(pk=b["mapping"].pk).last_synced_at == old


def test_refresh_task_for_a_given_bs_connection_makes_no_google_call_and_changes_nothing(worlds, http):
    a, b = worlds["A"], worlds["B"]
    with tenant_context(b["owner"].merchant_id), tenant_atomic():
        GoogleConnection.objects.filter(pk=b["connection"].pk).update(expires_at=timezone.now() + timedelta(seconds=30))
        before = GoogleConnection.objects.values("access_token_enc", "expires_at", "status").get(pk=b["connection"].pk)

    tasks.refresh_connection_token.delay(str(a["owner"].merchant_id), str(b["connection"].pk))

    assert http.calls == []
    with tenant_context(b["owner"].merchant_id), tenant_atomic():
        assert GoogleConnection.objects.values("access_token_enc", "expires_at", "status").get(pk=b["connection"].pk) == before
