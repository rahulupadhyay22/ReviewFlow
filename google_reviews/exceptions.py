"""google_reviews domain errors (spec 09-google-business-profile). Each
declares its HTTP mapping for core.api.exception_handler. Messages are fixed
text: never a token, code, ID token, Google account id or email, reviewer name
or provider response. Provider-side errors live in providers/base.py."""
from core.exceptions import ReviewFlowError


class GoogleNotConfigured(ReviewFlowError):
    """The feature flag is off, or a required platform setting is unset."""

    http_status = 503
    code = "google_not_configured"

    def __init__(self):
        super().__init__("Google integration is not configured.")


class InvalidOAuthState(ReviewFlowError):
    """Missing, expired, already claimed, or another user's state: one generic
    response for all of them, so a response reveals nothing about the cause."""

    http_status = 400
    code = "invalid_oauth_state"

    def __init__(self):
        super().__init__("Invalid or expired authorization state.")


class GoogleNeedsReauth(ReviewFlowError):
    http_status = 409
    code = "google_needs_reauth"

    def __init__(self):
        super().__init__("The Google connection needs to be reauthorized.")


class GoogleTokenRefreshPending(ReviewFlowError):
    """The stored access token needs a refresh, which a request may not perform
    (spec GA-1 rejected). A refresh task has been enqueued; retry afterwards."""

    http_status = 409
    code = "google_token_refresh_pending"

    def __init__(self):
        super().__init__("The Google access token is being refreshed. Try again shortly.")


class GooglePlaceIdMissing(ReviewFlowError):
    http_status = 422
    code = "google_place_id_missing"

    def __init__(self):
        super().__init__("This Google location has no Place ID.")


class ReviewLinkMissing(ReviewFlowError):
    """No usable newReviewUri for the location (spec RL-1)."""

    http_status = 422
    code = "google_review_link_missing"

    def __init__(self):
        super().__init__("This Google location has no usable review link.")


class GoogleLocationAlreadyMapped(ReviewFlowError):
    http_status = 409
    code = "google_location_already_mapped"

    def __init__(self):
        super().__init__("This Google location is already mapped to a location.")


class GoogleLocationNotAvailable(ReviewFlowError):
    """An unknown google_location_id, or a cross-merchant pair. One class and
    one message for both, so a response never reveals whether the location
    exists in another merchant's account (spec P-4)."""

    http_status = 422
    code = "google_location_not_available"

    def __init__(self):
        super().__init__("This Google location is not available.")


class GoogleNotFound(ReviewFlowError):
    http_status = 404
    code = "not_found"

    def __init__(self):
        super().__init__("Not found.")


class GoogleRateLimitedError(ReviewFlowError):
    """Google answered 429 to a read-only list call made by a request (spec
    "Provider errors on the request path"). Fixed message: no provider text."""

    http_status = 429
    code = "google_rate_limited"

    def __init__(self):
        super().__init__("Google is limiting requests right now. Try again shortly.")


class GoogleUnavailable(ReviewFlowError):
    """A network failure, timeout, 5xx or unreadable Google response."""

    http_status = 503
    code = "google_unavailable"

    def __init__(self):
        super().__init__("Google is temporarily unavailable. Try again later.")


class GoogleRequestFailed(ReviewFlowError):
    """Google refused the request (a non-429 4xx or an unusable body)."""

    http_status = 502
    code = "google_request_failed"

    def __init__(self):
        super().__init__("Google could not complete the request.")
