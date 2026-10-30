"""Provider failures on the two request-path endpoints (spec PE-1): a fixed
status, code and message each, never provider text. Real PostgreSQL commits, so
`connection.in_atomic_block` is meaningful at the Google call."""
import pytest

from core.tenancy import tenant_atomic, tenant_context
from google_reviews.models import GoogleConnection, GoogleLocation
from google_reviews.providers.base import GooglePermanentError, GoogleRateLimited, GoogleTransientError

pytestmark = pytest.mark.django_db(transaction=True)

SECRET = "SECRET-PROVIDER-TEXT-do-not-leak"

CASES = [
    (GoogleRateLimited, 429, "google_rate_limited", "Google is limiting requests right now. Try again shortly."),
    (GoogleTransientError, 503, "google_unavailable", "Google is temporarily unavailable. Try again later."),
    (GooglePermanentError, 502, "google_request_failed", "Google could not complete the request."),
]


@pytest.fixture
def setup(make_merchant, make_location, make_connection, session_client):
    owner = make_merchant("A")
    return {
        "owner": owner,
        "client": session_client(owner.user.email),
        "location": make_location(owner.merchant),
        "connection": make_connection(owner.merchant),
    }


def put(setup, google_location_id="accounts/1/locations/A"):
    client = setup["client"]
    return client.put(
        f"/api/v1/locations/{setup['location'].id}/google-location",
        {"google_connection_id": str(setup["connection"].id), "google_location_id": google_location_id},
        format="json",
        HTTP_X_CSRFTOKEN=client.csrf,
    )


def get_locations(setup):
    return setup["client"].get(f"/api/v1/google/connections/{setup['connection'].id}/locations")


@pytest.mark.parametrize("error_class,status,code,message", CASES)
def test_the_locations_list_maps_each_provider_failure(setup, fake_provider, caplog, error_class, status, code, message):
    fake_provider.list_error = error_class(SECRET)

    response = get_locations(setup)

    assert response.status_code == status
    assert response.json() == {"error": {"code": code, "message": message}}
    assert SECRET not in response.content.decode()
    assert SECRET not in caplog.text and "ACCESS-TOKEN-SENTINEL" not in caplog.text
    assert fake_provider.list_in_atomic == [False]
    assert "Retry-After" not in response


@pytest.mark.parametrize("error_class,status,code,message", CASES)
def test_the_mapping_put_maps_each_provider_failure_and_writes_nothing(
    setup, fake_provider, sync_enqueues, caplog, error_class, status, code, message
):
    fake_provider.list_error = error_class(SECRET)

    response = put(setup)

    assert response.status_code == status
    assert response.json() == {"error": {"code": code, "message": message}}
    assert SECRET not in response.content.decode()
    assert SECRET not in caplog.text and "ACCESS-TOKEN-SENTINEL" not in caplog.text
    assert fake_provider.list_in_atomic == [False]
    assert sync_enqueues == []
    with tenant_context(setup["owner"].merchant_id), tenant_atomic():
        assert GoogleLocation.objects.count() == 0


@pytest.mark.parametrize("error_class,status,code,message", CASES)
def test_a_provider_failure_never_changes_the_connection_status(setup, fake_provider, error_class, status, code, message):
    fake_provider.list_error = error_class(SECRET)

    get_locations(setup)
    put(setup)

    with tenant_context(setup["owner"].merchant_id), tenant_atomic():
        assert GoogleConnection.objects.get(pk=setup["connection"].pk).status == GoogleConnection.Status.ACTIVE
