"""GoogleSyncProvider interface (SAD.md §6, Coding-Standards.md §3).

Providers do no database access and call no services. Error messages never
carry a token, code, ID token, email, reviewer name or provider response.
Facts about Google's API are recorded in
docs/05-integrations/Google-Business-Profile.md (G-1 .. G-15).
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from core.exceptions import ReviewFlowError


class GoogleTransientError(ReviewFlowError):
    """Network failure, timeout or 5xx. Never changes a connection's status."""


class GooglePermanentError(ReviewFlowError):
    """A 4xx or unreadable response that retrying will not fix."""


class GoogleAuthRevoked(ReviewFlowError):
    """The refresh token is dead: invalid_grant or admin_policy_enforced on a
    refresh (G-10). The only error that may set NEEDS_REAUTH."""

    def __init__(self, reason: str = "invalid_grant"):
        super().__init__("The Google authorization was revoked.")
        self.reason = reason


class GoogleRateLimited(ReviewFlowError):
    """HTTP 429 (G-12). Google documents no Retry-After, so none is carried:
    the caller stops until the next Beat tick."""


class IdTokenInvalid(ReviewFlowError):
    """An ID-token validation check failed (spec "ID-token validation")."""


class IdTokenUnverifiable(ReviewFlowError):
    """Google's signing keys could not be fetched, so the token could not be
    verified."""


@dataclass(frozen=True)
class TokenSet:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class ExchangeResult:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: datetime
    granted_scopes: frozenset
    # The raw ID token: validated by verify_id_token before any claim is read,
    # never stored or logged.
    id_token: str | None = field(repr=False)


@dataclass(frozen=True)
class IdTokenClaims:
    sub: str | None
    email: str | None
    email_verified: bool | None
    nonce: str | None


@dataclass(frozen=True)
class ProviderLocation:
    # accounts/{accountId}/locations/{locationId} (spec OD-12)
    google_location_id: str
    title: str
    google_place_id: str | None
    review_uri: str | None


@dataclass(frozen=True)
class ProviderReview:
    google_review_id: str
    # None when the reviewer is anonymous (G-11: displayName is real only if
    # isAnonymous is false).
    reviewer_name: str | None
    # 1..5, or None for STAR_RATING_UNSPECIFIED / an unknown value.
    rating: int | None
    text: str | None
    # None when createTime is missing or unreadable.
    review_created_at: datetime | None


@dataclass(frozen=True)
class ReviewPage:
    reviews: list
    average_rating: Decimal | None
    review_count: int | None
    next_page_token: str | None


class GoogleSyncProvider(ABC):
    @abstractmethod
    def authorization_url(self, state: str, nonce: str) -> str: ...

    @abstractmethod
    def exchange_code(self, code: str) -> ExchangeResult: ...

    @abstractmethod
    def verify_id_token(self, id_token: str) -> IdTokenClaims: ...

    @abstractmethod
    def refresh(self, refresh_token: str) -> TokenSet: ...

    @abstractmethod
    def list_locations(self, access_token: str) -> list[ProviderLocation]: ...

    @abstractmethod
    def list_reviews(
        self, access_token: str, google_location_id: str, page_token: str | None = None
    ) -> ReviewPage: ...
