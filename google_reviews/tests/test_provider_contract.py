"""Provider contract tests: one group per GoogleBusinessProfileProvider method,
using anonymized Google payloads from tests/fixtures (G-14) and a generated JWKS
(G-15). The only mock is the Google HTTP boundary (`http`). No database."""
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from django.utils import timezone

from google_reviews.providers.base import (
    GoogleAuthRevoked,
    GooglePermanentError,
    GoogleRateLimited,
    GoogleTransientError,
)
from google_reviews.providers.google_business_profile import GoogleBusinessProfileProvider
from google_reviews.tests.helpers import (
    ACCOUNTS_URL,
    CLIENT_ID,
    JWKS_URL,
    LOCATIONS_BASE,
    REVIEWS_BASE,
    TOKEN_URL,
    Resp,
    fixture_json,
    google_settings,  # noqa: F401
    http,  # noqa: F401
    jwks,
    make_claims,
    sign,
)

pytestmark = pytest.mark.usefixtures("google_settings", "http")

provider = GoogleBusinessProfileProvider()
PARENT = "accounts/100000000001/locations/200000000001"


# --- authorization_url -------------------------------------------------------------------------


def test_authorization_url_carries_the_documented_parameters():
    url = provider.authorization_url("STATE-VALUE", "NONCE-VALUE")

    parsed = urlparse(url)
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert query == {
        "client_id": CLIENT_ID,
        "redirect_uri": "https://app.example.com/google/callback",
        "response_type": "code",
        "scope": "openid email https://www.googleapis.com/auth/business.manage",
        "state": "STATE-VALUE",
        "nonce": "NONCE-VALUE",
        "access_type": "offline",
        "prompt": "consent",
    }


# --- exchange_code ---------------------------------------------------------------------------------


def test_exchange_code_maps_the_token_response(http, settings):
    http.route(TOKEN_URL, fixture_json("token_response.json"))

    result = provider.exchange_code("AUTH-CODE")

    assert result.access_token == "ya29.TEST-ACCESS-TOKEN"
    assert result.refresh_token == "1//TEST-REFRESH-TOKEN"
    assert "https://www.googleapis.com/auth/business.manage" in result.granted_scopes
    assert result.id_token == "PLACEHOLDER-REPLACED-BY-THE-TEST"
    assert timedelta(seconds=3500) < result.expires_at - datetime.now(dt_timezone.utc) <= timedelta(seconds=3599)
    (call,) = http.calls
    assert (call.method, call.url) == ("POST", TOKEN_URL)  # secrets are never in the URL
    form = {k: v[0] for k, v in parse_qs(call.data.decode()).items()}
    assert form["grant_type"] == "authorization_code" and form["code"] == "AUTH-CODE"
    assert form["client_id"] == CLIENT_ID and form["redirect_uri"] == "https://app.example.com/google/callback"
    assert call.timeout == settings.GOOGLE_HTTP_TIMEOUT_SECONDS


def test_exchange_code_without_a_refresh_token_returns_none_for_it(http):
    body = fixture_json("token_response.json")
    del body["refresh_token"]
    http.route(TOKEN_URL, body)

    assert provider.exchange_code("c").refresh_token is None


@pytest.mark.parametrize(
    "answer,error",
    [
        (Resp(500, {"error": "backend_error"}), GoogleTransientError),
        (Resp(429, fixture_json("rate_limit.json")), GoogleRateLimited),
        (Resp(400, fixture_json("refresh_invalid_grant.json")), GooglePermanentError),
        (Resp(200, {"expires_in": 3599}), GooglePermanentError),  # no access_token
        (Resp(200, b"not json"), GoogleTransientError),
    ],
    ids=["5xx", "429", "400", "unusable-body", "unreadable"],
)
def test_exchange_code_errors_are_classified(http, answer, error):
    http.route(TOKEN_URL, answer)
    with pytest.raises(error):
        provider.exchange_code("c")


# --- verify_id_token (JWKS fixture) ----------------------------------------------------------------------


def test_verify_id_token_returns_the_claims_of_a_token_signed_by_a_jwks_key(http):
    http.route(JWKS_URL, jwks())

    claims = provider.verify_id_token(sign(make_claims("N-1")))

    assert (claims.sub, claims.email, claims.email_verified, claims.nonce) == ("sub-123", "owner@example.com", True, "N-1")
    assert [c.url for c in http.calls] == [JWKS_URL]


# --- refresh ---------------------------------------------------------------------------------------------------


def test_refresh_maps_the_response_and_returns_no_refresh_token(http):
    http.route(TOKEN_URL, fixture_json("refresh_response.json"))

    tokens = provider.refresh("1//STORED")

    assert tokens.access_token == "ya29.TEST-REFRESHED-ACCESS-TOKEN" and tokens.refresh_token is None
    assert tokens.expires_at > timezone.now() + timedelta(seconds=3000)
    form = {k: v[0] for k, v in parse_qs(http.calls[0].data.decode()).items()}
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "1//STORED"


@pytest.mark.parametrize(
    "fixture,reason", [("refresh_invalid_grant.json", "invalid_grant"), ("refresh_admin_policy_enforced.json", "admin_policy_enforced")]
)
def test_refresh_revocation_errors_raise_auth_revoked_and_nothing_else_does(http, caplog, fixture, reason):
    http.route(TOKEN_URL, Resp(400, fixture_json(fixture)))
    caplog.set_level(logging.DEBUG)

    with pytest.raises(GoogleAuthRevoked) as raised:
        provider.refresh("1//SECRET-REFRESH")

    assert raised.value.reason == reason
    assert "Token has been expired" not in str(raised.value)  # no provider text
    assert "1//SECRET-REFRESH" not in caplog.text


@pytest.mark.parametrize(
    "answer,error",
    [
        (Resp(400, {"error": "invalid_request"}), GooglePermanentError),
        (Resp(401, {"error": "invalid_client"}), GooglePermanentError),
        (Resp(500, {"error": "x"}), GoogleTransientError),
        (Resp(429, fixture_json("rate_limit.json")), GoogleRateLimited),
    ],
    ids=["400-other", "401", "5xx", "429"],
)
def test_refresh_other_errors_are_never_a_revocation(http, answer, error):
    http.route(TOKEN_URL, answer)
    with pytest.raises(error) as raised:
        provider.refresh("1//x")
    assert not isinstance(raised.value, GoogleAuthRevoked)


def test_a_rate_limit_is_raised_as_such_even_with_a_retry_after_header(http):
    # The Retry-After header is not read (G-12); the sync behavior is asserted
    # in test_sync.py::test_a_retry_after_header_changes_nothing.
    http.route(TOKEN_URL, Resp(429, fixture_json("rate_limit.json"), {"Retry-After": "30"}))
    with pytest.raises(GoogleRateLimited):
        provider.refresh("1//x")


# --- list_locations ------------------------------------------------------------------------------------------------


def test_list_locations_maps_the_documented_shape_with_and_without_place_id_and_review_uri(http):
    http.route(ACCOUNTS_URL, fixture_json("accounts.json"))
    http.route(LOCATIONS_BASE, fixture_json("locations.json"))

    found = provider.list_locations("ya29.X")

    assert [(f.google_location_id, f.title, f.google_place_id, f.review_uri) for f in found] == [
        (PARENT, "Test Cafe Indiranagar", "ChIJ-TEST-PLACE-1", "https://g.page/r/TEST-REVIEW-1/review"),
        ("accounts/100000000001/locations/200000000002", "Test Cafe Koramangala", None, "https://g.page/r/TEST-REVIEW-2/review"),
        ("accounts/100000000001/locations/200000000003", "Test Cafe Whitefield", "ChIJ-TEST-PLACE-3", None),
    ]
    assert found[0].google_place_id != found[0].google_location_id
    assert all(c.method == "GET" for c in http.calls)
    assert {c.url.split("?")[0] for c in http.calls} == {ACCOUNTS_URL, f"{LOCATIONS_BASE}accounts/100000000001/locations"}


def test_list_locations_follows_every_next_page_token_for_accounts_and_locations(http):
    def token_of(req):
        return parse_qs(urlparse(req.full_url).query).get("pageToken", [None])[0]

    def accounts(req):
        if token_of(req) is None:
            return {"accounts": [{"name": "accounts/1"}], "nextPageToken": "a2"}
        return {"accounts": [{"name": "accounts/2"}]}

    def locations(req):
        account = req.full_url.split("/accounts/")[1].split("/")[0]
        if account == "1" and token_of(req) is None:
            return {"locations": [{"name": "locations/11", "title": "One"}], "nextPageToken": "l2"}
        if account == "1":
            return {"locations": [{"name": "locations/12", "title": "Two"}]}
        return {"locations": [{"name": "locations/21", "title": "Three"}]}

    http.route(ACCOUNTS_URL, accounts)
    http.route(LOCATIONS_BASE, locations)

    found = provider.list_locations("ya29.X")

    assert [f.google_location_id for f in found] == [
        "accounts/1/locations/11",
        "accounts/1/locations/12",
        "accounts/2/locations/21",
    ]
    first_locations_call = parse_qs(urlparse(http.to(LOCATIONS_BASE)[0].url).query)
    assert first_locations_call["readMask"] == ["name,title,metadata"]


@pytest.mark.parametrize(
    "answer,error",
    [(Resp(429, fixture_json("rate_limit.json")), GoogleRateLimited), (Resp(503, {}), GoogleTransientError), (Resp(403, {}), GooglePermanentError)],
)
def test_list_locations_errors_are_classified(http, answer, error):
    http.route(ACCOUNTS_URL, answer)
    with pytest.raises(error):
        provider.list_locations("ya29.X")


# --- list_reviews ---------------------------------------------------------------------------------------------------------


def test_list_reviews_maps_a_review_page(http):
    http.route(REVIEWS_BASE, {**fixture_json("reviews.json"), "nextPageToken": "NEXT"})

    result = provider.list_reviews("ya29.X", PARENT, None)

    by_id = {r.google_review_id: r for r in result.reviews}
    assert list(by_id) == ["TEST-REVIEW-ID-1", "TEST-REVIEW-ID-2", "TEST-REVIEW-ID-3", "TEST-REVIEW-ID-4"]
    assert [by_id[k].rating for k in by_id] == [5, 1, None, 3]  # STAR_RATING_UNSPECIFIED -> None
    assert by_id["TEST-REVIEW-ID-1"].reviewer_name == "Test Reviewer One"
    assert by_id["TEST-REVIEW-ID-2"].reviewer_name is None  # isAnonymous
    assert by_id["TEST-REVIEW-ID-2"].text is None
    assert by_id["TEST-REVIEW-ID-1"].review_created_at == datetime(2026, 9, 1, 10, 0, 0, 123456, tzinfo=dt_timezone.utc)
    assert (result.average_rating, result.review_count, result.next_page_token) == (Decimal("3.5"), 4, "NEXT")
    (call,) = http.calls
    assert call.method == "GET"
    parsed = urlparse(call.url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{REVIEWS_BASE}{PARENT}/reviews"
    assert parse_qs(parsed.query) == {"pageSize": ["50"]}


def test_list_reviews_sends_the_page_token(http):
    http.route(REVIEWS_BASE, {"reviews": []})

    provider.list_reviews("ya29.X", PARENT, "TOKEN-2")

    assert parse_qs(urlparse(http.calls[0].url).query)["pageToken"] == ["TOKEN-2"]


@pytest.mark.parametrize("bad", ["locations/200000000001", "200000000001", "accounts/1/locations/", "accounts/1/locations/2/../x", ""])
def test_list_reviews_refuses_a_malformed_parent_without_calling_google(http, bad):
    with pytest.raises(GooglePermanentError):
        provider.list_reviews("ya29.X", bad, None)
    assert http.calls == []


@pytest.mark.parametrize(
    "answer,error",
    [
        (Resp(429, fixture_json("rate_limit.json")), GoogleRateLimited),
        (Resp(403, {"error": {"code": 403}}), GooglePermanentError),
        (Resp(404, {"error": {"code": 404}}), GooglePermanentError),
        (Resp(500, {}), GoogleTransientError),
    ],
    ids=["429", "403", "404", "5xx"],
)
def test_list_reviews_errors_are_classified_and_leak_nothing(http, caplog, answer, error):
    http.route(REVIEWS_BASE, answer)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(error) as raised:
        provider.list_reviews("ya29.SECRET-ACCESS", PARENT, None)

    assert "ya29.SECRET-ACCESS" not in caplog.text and "ya29.SECRET-ACCESS" not in str(raised.value)
    assert "Quota exceeded" not in str(raised.value)
