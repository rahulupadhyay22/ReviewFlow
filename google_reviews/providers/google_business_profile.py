"""The only module that talks to Google (OAuth, Account Management, Business
Information and the v4 reviews API).

No business logic, no database access; called only from
google_reviews/services.py. Stdlib urllib with an explicit timeout, the same
seam style as whatsapp/providers/meta_cloud.py and billing/razorpay.py.
Exception text never contains a response body, token, code, ID token or email;
only the operation and the HTTP status are logged.

Every endpoint, parameter and field below comes from
docs/05-integrations/Google-Business-Profile.md (G-1 .. G-15, read 2026-10-23).
RS256 verification uses the already-installed cryptography package; no JWT
library is added.
"""
import base64
import binascii
import http.client
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.conf import settings

from google_reviews.providers.base import (
    ExchangeResult,
    GoogleAuthRevoked,
    GooglePermanentError,
    GoogleRateLimited,
    GoogleSyncProvider,
    GoogleTransientError,
    IdTokenClaims,
    IdTokenInvalid,
    IdTokenUnverifiable,
    ProviderLocation,
    ProviderReview,
    ReviewPage,
    TokenSet,
)

logger = logging.getLogger(__name__)

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"  # G-2
_TOKEN_URL = "https://oauth2.googleapis.com/token"  # G-2
_ACCOUNTS_URL = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"  # G-8
_LOCATIONS_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"  # G-8
_REVIEWS_BASE = "https://mybusiness.googleapis.com/v4"  # G-11
# G-5, G-6: business.manage for the Business Profile APIs; openid + email for
# the ID token sub and email.
_SCOPES = "openid email https://www.googleapis.com/auth/business.manage"
_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})  # G-15
_ACCOUNT_PAGE_SIZE = 20  # G-8: default and maximum
_LOCATION_PAGE_SIZE = 100  # G-8: maximum
_REVIEW_PAGE_SIZE = 50  # G-11: maximum
# Loop guards. Hitting one is an error, never a silent truncation.
_MAX_ACCOUNT_PAGES = 50
_MAX_LOCATION_PAGES = 100
_MAX_BODY = 8 * 1024 * 1024
_REVOKED_ERRORS = frozenset({"invalid_grant", "admin_policy_enforced"})  # G-10
_STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}  # G-11
_ACCOUNT_NAME = re.compile(r"^accounts/([A-Za-z0-9_.\-]+)$")
_LOCATION_NAME = re.compile(r"^locations/([A-Za-z0-9_.\-]+)$")
_FULL_LOCATION_NAME = re.compile(r"^accounts/([A-Za-z0-9_.\-]+)/locations/([A-Za-z0-9_.\-]+)$")
_B64URL = re.compile(r"^[A-Za-z0-9_\-]*$")
_MAX_JWKS_TTL = 86400

# In-process only: Redis holds no business data and the database is not a key
# cache. Keys are public, so a stale copy is harmless; it is refreshed on an
# unknown kid or on expiry.
_jwks_lock = threading.Lock()
_jwks_cache = {"keys": {}, "expires": 0.0}


def reset_jwks_cache() -> None:
    with _jwks_lock:
        _jwks_cache.update(keys={}, expires=0.0)


def _quote(value) -> str:
    """A provider id as one URL path segment."""
    return urllib.parse.quote(str(value), safe="")


def _call(method: str, url: str, *, headers: dict | None = None, data: bytes | None = None, operation: str):
    """One HTTP call. Returns (status, response headers, raw body). A non-2xx
    answer is returned, not raised, so each caller can map it; only a network
    failure raises (always transient)."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=settings.GOOGLE_HTTP_TIMEOUT_SECONDS) as resp:
            return 200, getattr(resp, "headers", None), resp.read(_MAX_BODY)
    except urllib.error.HTTPError as exc:
        # HTTPError is a URLError: it must be caught first.
        try:
            raw = exc.read(_MAX_BODY)
        except (OSError, http.client.HTTPException):
            raw = b""
        logger.warning("Google %s answered %s", operation, exc.code)
        return exc.code, exc.headers, raw
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        logger.warning("Google %s failed with %s", operation, type(exc).__name__)
        raise GoogleTransientError("Google is unavailable.") from None


def _raise_for_status(status: int) -> None:
    if status == 429:
        raise GoogleRateLimited("Google rate limit reached.")
    if status >= 500:
        raise GoogleTransientError("Google is unavailable.")
    raise GooglePermanentError("Google refused the request.")


def _json_dict(raw: bytes) -> dict:
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise GoogleTransientError("Google returned an unreadable response.") from None
    if not isinstance(parsed, dict):
        raise GoogleTransientError("Google returned an unreadable response.")
    return parsed


def _error_code(raw: bytes) -> str | None:
    """The OAuth error code of a token-endpoint failure, or None."""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    error = parsed.get("error") if isinstance(parsed, dict) else None
    return error if isinstance(error, str) else None


def _bearer(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _b64url_decode(segment: str) -> bytes:
    if not _B64URL.match(segment):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _fetch_jwks() -> tuple[dict, int]:
    """The signing keys {kid: RSA public key} and the cache lifetime from the
    response Cache-Control (G-15). No max-age means no caching."""
    try:
        status, headers, raw = _call(
            "GET", settings.GOOGLE_OIDC_JWKS_URI, headers={"Accept": "application/json"}, operation="jwks"
        )
    except GoogleTransientError:
        raise IdTokenUnverifiable("The Google signing keys could not be fetched.") from None
    if status != 200:
        raise IdTokenUnverifiable("The Google signing keys could not be fetched.")
    try:
        body = _json_dict(raw)
    except GoogleTransientError:
        raise IdTokenUnverifiable("The Google signing keys could not be read.") from None
    items = body.get("keys")
    keys = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        kid, n, e = item.get("kid"), item.get("n"), item.get("e")
        if (
            item.get("kty") != "RSA"
            or item.get("use") != "sig"
            or item.get("alg") != "RS256"
            or not isinstance(kid, str)
            or not kid
            or not isinstance(n, str)
            or not isinstance(e, str)
        ):
            continue
        try:
            numbers = rsa.RSAPublicNumbers(
                int.from_bytes(_b64url_decode(e), "big"), int.from_bytes(_b64url_decode(n), "big")
            )
            keys[kid] = numbers.public_key()
        except (ValueError, binascii.Error):
            continue
    ttl = 0
    cache_control = headers.get("Cache-Control") if headers is not None else None
    match = re.search(r"max-age=(\d+)", cache_control or "")
    if match:
        ttl = min(int(match.group(1)), _MAX_JWKS_TTL)
    return keys, ttl


def _jwks_key(kid: str):
    """The RSA public key for kid, or None. A cached key is used while it is
    fresh; an unknown kid, or an expired cache, forces exactly one refetch."""
    with _jwks_lock:
        fresh = time.monotonic() < _jwks_cache["expires"]
        cached = _jwks_cache["keys"].get(kid) if fresh else None
    if cached is not None:
        return cached
    keys, ttl = _fetch_jwks()
    with _jwks_lock:
        _jwks_cache.update(keys=keys, expires=time.monotonic() + ttl)
    return keys.get(kid)


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_review(item) -> ProviderReview | None:
    if not isinstance(item, dict):
        return None
    review_id = _text(item.get("reviewId"))
    if review_id is None:
        return None
    reviewer = item.get("reviewer")
    name = None
    # proto3 JSON omits a false isAnonymous, so only an explicit true hides the
    # name (G-11: displayName is real only when isAnonymous is false).
    if isinstance(reviewer, dict) and reviewer.get("isAnonymous") is not True:
        name = _text(reviewer.get("displayName"))
    return ProviderReview(
        google_review_id=review_id,
        reviewer_name=name,
        rating=_STARS.get(item.get("starRating")),
        text=_text(item.get("comment")),
        review_created_at=_parse_time(item.get("createTime")),
    )


def _average_rating(value) -> Decimal | None:
    if not _is_number(value):
        return None
    try:
        rating = Decimal(str(value)).quantize(Decimal("0.1"))
    except InvalidOperation:
        return None
    return rating if Decimal("0") <= rating <= Decimal("5") else None


class GoogleBusinessProfileProvider(GoogleSyncProvider):
    def authorization_url(self, state: str, nonce: str) -> str:
        query = urllib.parse.urlencode(
            {
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
                "response_type": "code",
                "scope": _SCOPES,
                "state": state,
                "nonce": nonce,
                # G-7: a refresh token is returned on the first authorization
                # only, so consent is requested every time.
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        return f"{_AUTH_URL}?{query}"

    def _token_request(self, form: dict, operation: str) -> tuple[int, bytes]:
        status, _headers, raw = _call(
            "POST",
            _TOKEN_URL,
            data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            operation=operation,
        )
        return status, raw

    def exchange_code(self, code: str) -> ExchangeResult:
        status, raw = self._token_request(
            {
                "code": code,
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            "token exchange",
        )
        if status != 200:
            _raise_for_status(status)
        body = _json_dict(raw)
        access_token, expires_in = _text(body.get("access_token")), body.get("expires_in")
        if access_token is None or not _is_number(expires_in) or expires_in <= 0:
            raise GooglePermanentError("Google returned an unusable token response.")
        scope = body.get("scope")
        return ExchangeResult(
            access_token=access_token,
            refresh_token=_text(body.get("refresh_token")),
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            granted_scopes=frozenset(scope.split()) if isinstance(scope, str) else frozenset(),
            id_token=_text(body.get("id_token")),
        )

    def verify_id_token(self, id_token: str) -> IdTokenClaims:
        """Spec "ID-token validation" checks 1-5: RS256 only, signature against
        the JWKS, iss, aud (plus azp for a list), exp. The nonce and identity
        checks are made by the service. HTTPS delivery alone is never treated
        as proof of validity."""
        parts = id_token.split(".") if isinstance(id_token, str) else []
        if len(parts) != 3:
            raise IdTokenInvalid("The ID token is invalid.")
        try:
            header = json.loads(_b64url_decode(parts[0]))
            payload = json.loads(_b64url_decode(parts[1]))
            signature = _b64url_decode(parts[2])
        except (ValueError, binascii.Error):
            raise IdTokenInvalid("The ID token is invalid.") from None
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise IdTokenInvalid("The ID token is invalid.")
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
            raise IdTokenInvalid("The ID token is invalid.")
        key = _jwks_key(kid)
        if key is None:
            raise IdTokenInvalid("The ID token is invalid.")
        try:
            key.verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            raise IdTokenInvalid("The ID token is invalid.") from None
        if payload.get("iss") not in _ISSUERS:
            raise IdTokenInvalid("The ID token is invalid.")
        client_id = settings.GOOGLE_OAUTH_CLIENT_ID
        audience = payload.get("aud")
        if isinstance(audience, list):
            audience_ok = client_id in audience and payload.get("azp") == client_id
        else:
            audience_ok = audience == client_id
        if not client_id or not audience_ok:
            raise IdTokenInvalid("The ID token is invalid.")
        expiry = payload.get("exp")
        # Google: the token must not be accepted on or after exp.
        if isinstance(expiry, bool) or not isinstance(expiry, int) or time.time() >= expiry:
            raise IdTokenInvalid("The ID token is invalid.")
        verified = payload.get("email_verified")
        return IdTokenClaims(
            sub=_text(payload.get("sub")),
            email=_text(payload.get("email")),
            email_verified=verified if isinstance(verified, bool) else None,
            nonce=_text(payload.get("nonce")),
        )

    def refresh(self, refresh_token: str) -> TokenSet:
        status, raw = self._token_request(
            {
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            "token refresh",
        )
        if status != 200:
            # G-10 (partial): only a 400 carrying one of the two documented
            # error codes is revocation. Anything else never changes status.
            if status == 400:
                error = _error_code(raw)
                if error in _REVOKED_ERRORS:
                    raise GoogleAuthRevoked(error)
            _raise_for_status(status)
        body = _json_dict(raw)
        access_token, expires_in = _text(body.get("access_token")), body.get("expires_in")
        if access_token is None or not _is_number(expires_in) or expires_in <= 0:
            raise GooglePermanentError("Google returned an unusable token response.")
        return TokenSet(
            access_token=access_token,
            # G-7: the documented refresh response carries none, but a new one
            # is not excluded.
            refresh_token=_text(body.get("refresh_token")),
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        )

    def _get_json(self, url: str, access_token: str, operation: str) -> dict:
        status, _headers, raw = _call("GET", url, headers=_bearer(access_token), operation=operation)
        if status != 200:
            _raise_for_status(status)
        return _json_dict(raw)

    def list_locations(self, access_token: str) -> list[ProviderLocation]:
        locations = []
        for account_name in self._account_names(access_token):
            token = None
            for _ in range(_MAX_LOCATION_PAGES):
                query = {"readMask": "name,title,metadata", "pageSize": _LOCATION_PAGE_SIZE}
                if token:
                    query["pageToken"] = token
                body = self._get_json(
                    f"{_LOCATIONS_BASE}/{account_name}/locations?{urllib.parse.urlencode(query)}",
                    access_token,
                    "locations list",
                )
                items = body.get("locations")
                for item in items if isinstance(items, list) else []:
                    location = self._parse_location(account_name, item)
                    if location is not None:
                        locations.append(location)
                token = _text(body.get("nextPageToken"))
                if not token:
                    break
            else:
                raise GooglePermanentError("Google returned too many location pages.")
        return locations

    def _account_names(self, access_token: str) -> list[str]:
        names, token = [], None
        for _ in range(_MAX_ACCOUNT_PAGES):
            query = {"pageSize": _ACCOUNT_PAGE_SIZE}
            if token:
                query["pageToken"] = token
            body = self._get_json(f"{_ACCOUNTS_URL}?{urllib.parse.urlencode(query)}", access_token, "accounts list")
            items = body.get("accounts")
            for item in items if isinstance(items, list) else []:
                name = item.get("name") if isinstance(item, dict) else None
                if isinstance(name, str) and _ACCOUNT_NAME.match(name):
                    names.append(name)
            token = _text(body.get("nextPageToken"))
            if not token:
                return names
        raise GooglePermanentError("Google returned too many account pages.")

    @staticmethod
    def _parse_location(account_name: str, item) -> ProviderLocation | None:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        if not isinstance(name, str) or _LOCATION_NAME.match(name) is None:
            return None
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        return ProviderLocation(
            google_location_id=f"{account_name}/{name}",
            title=_text(item.get("title")) or "",
            google_place_id=_text(metadata.get("placeId")),
            review_uri=_text(metadata.get("newReviewUri")),
        )

    def list_reviews(self, access_token: str, google_location_id: str, page_token: str | None = None) -> ReviewPage:
        match = _FULL_LOCATION_NAME.match(google_location_id or "")
        if match is None:
            raise GooglePermanentError("The Google location id is malformed.")
        # The v4 parent is the stored full resource name (spec OD-12).
        parent = f"accounts/{_quote(match.group(1))}/locations/{_quote(match.group(2))}"
        query = {"pageSize": _REVIEW_PAGE_SIZE}
        if page_token:
            query["pageToken"] = page_token
        body = self._get_json(
            f"{_REVIEWS_BASE}/{parent}/reviews?{urllib.parse.urlencode(query)}", access_token, "reviews list"
        )
        items = body.get("reviews")
        reviews = [r for r in (_parse_review(i) for i in (items if isinstance(items, list) else [])) if r]
        count = body.get("totalReviewCount")
        return ReviewPage(
            reviews=reviews,
            average_rating=_average_rating(body.get("averageRating")),
            review_count=count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None,
            next_page_token=_text(body.get("nextPageToken")),
        )
