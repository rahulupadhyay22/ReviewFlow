"""google_reviews business logic (spec 09-google-business-profile).

Views, serializers and tasks call these functions; none of them holds a rule
(Coding-Standards.md section 1). Rules that cut across functions:

- Every OAuth attempt transition is ONE conditional UPDATE in its own short
  tenant_atomic(); the affected-row count decides the winner. There is no
  read-then-write on a status.
- No Google call is ever made inside a database transaction or while a row is
  locked. The code exchange, token refresh and JWKS fetch run only in Celery
  tasks (spec GA-1 was rejected); a request makes only read-only list calls.
- A request that would need a token refresh answers 409
  google_token_refresh_pending and registers a refresh task with
  transaction.on_commit INSIDE a tenant_atomic() block, which then exits
  normally, so the task starts only after the block has committed.
- Tokens, the authorization code, ID tokens, sub and email are never logged,
  audited or passed as task arguments.
"""
import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

import psycopg.errors
from cryptography.fernet import InvalidToken
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import Merchant, TeamMember, User
from auditlog.services import ActorNotMember, record
from core.crypto import decrypt, encrypt
from core.exceptions import TenantContextError
from core.tenancy import get_current_merchant_id, tenant_atomic
from google_reviews.exceptions import (
    GoogleLocationAlreadyMapped,
    GoogleLocationNotAvailable,
    GoogleNeedsReauth,
    GoogleNotConfigured,
    GoogleNotFound,
    GooglePlaceIdMissing,
    GoogleRateLimitedError,
    GoogleRequestFailed,
    GoogleTokenRefreshPending,
    GoogleUnavailable,
    InvalidOAuthState,
    ReviewLinkMissing,
)
from google_reviews.models import GoogleConnection, GoogleLocation, GoogleOAuthState, GoogleReview
from google_reviews.providers import get_provider
from google_reviews.providers.base import (
    GoogleAuthRevoked,
    GooglePermanentError,
    GoogleRateLimited,
    GoogleTransientError,
    IdTokenInvalid,
    IdTokenUnverifiable,
)
from locations.services import accessible_locations, get_accessible_location

logger = logging.getLogger(__name__)

BUSINESS_SCOPE = "https://www.googleapis.com/auth/business.manage"
_CONNECTION_UNIQUE = "google_connection_merchant_sub_uniq"
_LOCATION_UNIQUE = "google_location_unique_per_connection"
# The OneToOneField's unique index on location_id (Django's generated name,
# read back from pg_constraint): the loser of two concurrent first mappings.
_LOCATION_ONE_TO_ONE = "google_reviews_googlelocation_location_id_key"
_MAX_REVIEW_LINK = 2048
_Failure = GoogleOAuthState.FailureCode


class AttemptNoLongerPending(Exception):
    """The successful finish found the attempt already terminal (for example
    expired to EXCHANGE_INTERRUPTED). Raised inside the finish transaction so
    that the connection write and its audit row roll back with it."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def is_enabled() -> bool:
    """The feature flag and every platform setting a connect needs. An unset
    value fails closed (spec: 503 google_not_configured)."""
    return bool(
        settings.GOOGLE_INTEGRATION_ENABLED
        and settings.GOOGLE_OAUTH_CLIENT_ID
        and settings.GOOGLE_OAUTH_CLIENT_SECRET
        and settings.GOOGLE_OAUTH_REDIRECT_URI
        and settings.FERNET_KEY
    )


def _current_merchant_id():
    merchant_id = get_current_merchant_id()
    if merchant_id is None:
        raise TenantContextError("google_reviews services require a tenant context.")
    return merchant_id


def _enqueue_on_commit(task_name: str, *args) -> None:
    """Register a task enqueue with transaction.on_commit. Call it only INSIDE
    a tenant_atomic() block: outside any transaction Django runs the callback
    immediately, before the state it depends on is committed. A broker error is
    logged by class name only and never fails the caller."""

    def _run():
        from google_reviews import tasks  # lazy: tasks import this module

        try:
            getattr(tasks, task_name).delay(*args)
        except Exception as exc:
            logger.warning("Enqueue of google_reviews %s failed with %s", task_name, type(exc).__name__)

    transaction.on_commit(_run)


def validate_review_link(uri) -> str:
    """The newReviewUri that Google returns, accepted only if it is a plain
    https URL (spec RL-1). Returned unchanged: ReviewFlow never builds or
    rewrites a review URL. No host allowlist is applied, because Google
    documents no host (G-9)."""
    if not isinstance(uri, str) or not uri or len(uri) > _MAX_REVIEW_LINK:
        raise ReviewLinkMissing()
    if uri != uri.strip() or any(ord(ch) <= 32 or ord(ch) == 127 for ch in uri):
        raise ReviewLinkMissing()
    try:
        parts = urlsplit(uri)
        hostname = parts.hostname
    except ValueError:
        raise ReviewLinkMissing() from None
    if parts.scheme != "https" or not hostname or "@" in parts.netloc or parts.fragment:
        raise ReviewLinkMissing()
    return uri


# --- OAuth attempts (spec "OAuth attempt lifecycle") ----------------------------------


def expire_attempts(*, attempt_id=None, now=None) -> None:
    """Both expire rules. Needs a tenant context; runs in the caller's
    transaction. An unclaimed attempt (ISSUED, or PENDING with its code still
    stored) expires at expires_at. An exchange in flight (PENDING, code already
    taken) is not expired until the grace period past expires_at, so an
    exchange that starts at the last second can always finish."""
    now = now or timezone.now()
    grace = timedelta(seconds=settings.GOOGLE_OAUTH_EXCHANGE_GRACE_SECONDS)
    states = GoogleOAuthState.objects.all()
    if attempt_id is not None:
        states = states.filter(pk=attempt_id)
    states.filter(
        Q(status=GoogleOAuthState.Status.ISSUED)
        | Q(status=GoogleOAuthState.Status.PENDING, code_enc__isnull=False),
        expires_at__lte=now,
    ).update(
        status=GoogleOAuthState.Status.FAILED,
        failure_code=_Failure.OAUTH_EXPIRED,
        code_enc=None,
        completed_at=now,
        updated_at=now,
    )
    states.filter(
        status=GoogleOAuthState.Status.PENDING, code_enc__isnull=True, expires_at__lte=now - grace
    ).update(
        status=GoogleOAuthState.Status.FAILED,
        failure_code=_Failure.EXCHANGE_INTERRUPTED,
        completed_at=now,
        updated_at=now,
    )


def purge_attempts(*, now=None) -> None:
    """Delete terminal attempts older than the retention period past
    expires_at. Needs a tenant context."""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=settings.GOOGLE_OAUTH_ATTEMPT_RETENTION_SECONDS)
    GoogleOAuthState.objects.filter(
        status__in=[GoogleOAuthState.Status.COMPLETED, GoogleOAuthState.Status.FAILED],
        expires_at__lte=cutoff,
    ).delete()


def issue_oauth_state(*, actor) -> tuple[str, str]:
    """Issue one single-use attempt for the current merchant and the actor (a
    User). Only sha256(state) and sha256(nonce) are stored; the plaintexts are
    returned once, for the authorization URL only."""
    merchant_id = _current_merchant_id()
    now = timezone.now()
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with tenant_atomic():
        expire_attempts(now=now)
        purge_attempts(now=now)
        GoogleOAuthState.objects.create(
            merchant_id=merchant_id,
            user=actor,
            state_hash=_hash(state),
            nonce_hash=_hash(nonce),
            expires_at=now + timedelta(seconds=settings.GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS),
        )
    return state, nonce


def begin_google_oauth(*, actor) -> dict:
    """POST /google/oauth/start. Raises GoogleNotConfigured (503) before
    anything is stored. No Google call."""
    if not is_enabled():
        raise GoogleNotConfigured()
    state, nonce = issue_oauth_state(actor=actor)
    return {"authorization_url": get_provider().authorization_url(state, nonce)}


def complete_google_oauth(*, actor, code: str, state: str) -> GoogleOAuthState:
    """POST /google/oauth/complete. In one conditional UPDATE, claim the attempt
    (ISSUED -> PENDING) and store the encrypted code; then enqueue the exchange
    after commit. Makes no Google call. A missing, expired or already claimed
    state, or the state of another user, matches no row and gets the one
    generic error."""
    if not is_enabled():
        raise GoogleNotConfigured()
    now = timezone.now()
    state_hash = _hash(state)
    code_enc = encrypt(code)
    with tenant_atomic():
        claimed = GoogleOAuthState.objects.filter(
            state_hash=state_hash,
            user=actor,
            status=GoogleOAuthState.Status.ISSUED,
            expires_at__gt=now,
        ).update(status=GoogleOAuthState.Status.PENDING, code_enc=code_enc, updated_at=now)
        if claimed != 1:
            raise InvalidOAuthState()
        attempt = GoogleOAuthState.objects.get(state_hash=state_hash)
        _enqueue_on_commit("exchange_oauth_code", str(attempt.merchant_id), str(attempt.pk))
    return attempt


def get_oauth_attempt(*, actor, attempt_id) -> GoogleOAuthState:
    """GET /google/oauth/attempts/{id}: only the user who started the attempt.
    Anyone else, and an unknown or foreign id, gets the same 404. The expire
    rules run for that row first."""
    with tenant_atomic():
        expire_attempts(attempt_id=attempt_id)
        attempt = GoogleOAuthState.objects.filter(pk=attempt_id, user=actor).first()
    if attempt is None:
        raise GoogleNotFound()
    return attempt


# --- the code exchange (task path, spec "OAuth attempt lifecycle") --------------------


def _violated_constraint(exc: IntegrityError, name: str) -> bool:
    """True only for a unique violation of the named constraint. Django re-raises
    the driver error with `from`, so the cause carries the constraint name."""
    cause = exc.__cause__
    return isinstance(cause, psycopg.errors.UniqueViolation) and getattr(cause.diag, "constraint_name", None) == name


def _mark_failed(attempt_id, code, now=None) -> bool:
    """The only way an attempt is failed after a claim: a conditional UPDATE
    that cannot overwrite a terminal state. Returns whether it changed a row."""
    now = now or timezone.now()
    return (
        GoogleOAuthState.objects.filter(pk=attempt_id, status=GoogleOAuthState.Status.PENDING).update(
            status=GoogleOAuthState.Status.FAILED,
            failure_code=code,
            completed_at=now,
            code_enc=None,
            updated_at=now,
        )
        == 1
    )


def _fail(attempt_id, code) -> None:
    with tenant_atomic():
        changed = _mark_failed(attempt_id, code)
    logger.info("Google OAuth attempt %s: %s", attempt_id, f"failed {code}" if changed else "left unchanged")


def _take_code(attempt_id):
    """Take the stored code out of the database BEFORE Google sees it. A
    compare-and-swap: read the ciphertext, then clear it only if it is still
    there; the affected-row count decides the single winner, so a duplicate
    task finds nothing. Returns (code_enc, nonce_hash, user_id) or None.

    Why this is a single-winner claim: under PostgreSQL READ COMMITTED two
    workers may both read the ciphertext, but their UPDATEs lock the same row
    one after the other. The second UPDATE waits for the first to commit, then
    re-evaluates its WHERE clause against the new row version, where code_enc
    is NULL; `NULL = <ciphertext>` is never true, so it matches 0 rows and that
    worker returns None without calling Google. The compare cannot be fooled by
    the value coming back (ABA): code_enc is set once, by the conditional
    ISSUED -> PENDING update in complete_google_oauth, the CHECK
    google_oauth_code_only_pending allows a non-null value only while PENDING,
    and Fernet ciphertext is randomised. The take commits before exchange_code
    runs; a crash after it leaves a code-less PENDING attempt that
    expire_attempts ends as EXCHANGE_INTERRUPTED, so a code is never reused."""
    now = timezone.now()
    with tenant_atomic():
        row = (
            GoogleOAuthState.objects.filter(
                pk=attempt_id,
                status=GoogleOAuthState.Status.PENDING,
                code_enc__isnull=False,
                expires_at__gt=now,
            )
            .values_list("code_enc", "nonce_hash", "user_id")
            .first()
        )
        if row is None:
            return None
        taken = GoogleOAuthState.objects.filter(
            pk=attempt_id,
            status=GoogleOAuthState.Status.PENDING,
            code_enc=row[0],
            expires_at__gt=now,
        ).update(code_enc=None, updated_at=now)
    return row if taken == 1 else None


def _validate_exchange(provider, result, nonce_hash):
    """Spec checks in order: the ID token (signature, iss, aud, exp, nonce),
    then identity, then scope. Returns (claims, None) or (None, failure_code)."""
    if not result.id_token:
        return None, _Failure.IDENTITY_MISSING
    try:
        claims = provider.verify_id_token(result.id_token)
    except IdTokenUnverifiable:
        return None, _Failure.ID_TOKEN_UNVERIFIABLE
    except IdTokenInvalid:
        return None, _Failure.ID_TOKEN_INVALID
    if claims.nonce is None or not hmac.compare_digest(_hash(claims.nonce), nonce_hash):
        return None, _Failure.ID_TOKEN_INVALID
    if not claims.sub or len(claims.sub) > 255 or not claims.email or len(claims.email) > 254:
        return None, _Failure.IDENTITY_MISSING
    if BUSINESS_SCOPE not in result.granted_scopes:
        return None, _Failure.SCOPE_MISSING
    return claims, None


def exchange_attempt(attempt_id) -> None:
    """The body of exchange_oauth_code. Every Google call runs outside any
    transaction. A failed or uncertain exchange is terminal and never retried
    (the code is spent). Needs a tenant context."""
    taken = _take_code(attempt_id)
    if taken is None:
        return  # a duplicate task, or the attempt expired: no Google call
    code_enc, nonce_hash, user_id = taken
    provider = get_provider()
    try:
        result = provider.exchange_code(decrypt(code_enc))
    except (InvalidToken, GoogleTransientError, GooglePermanentError, GoogleRateLimited):
        return _fail(attempt_id, _Failure.EXCHANGE_FAILED)
    claims, failure = _validate_exchange(provider, result, nonce_hash)
    if failure is not None:
        return _fail(attempt_id, failure)
    try:
        with tenant_atomic(durable=True):
            _finish_ok(attempt_id, user_id, claims, result)
    except AttemptNoLongerPending:
        logger.info("Google OAuth attempt %s was no longer pending; nothing written", attempt_id)
    except ActorNotMember:
        _fail(attempt_id, _Failure.ACTOR_NOT_MEMBER)
    except IntegrityError as exc:
        # Caught OUTSIDE the failed transaction, after it rolled back: nothing
        # is read or written inside a broken transaction. The losing attempt's
        # tokens existed only in this function and are discarded.
        if not _violated_constraint(exc, _CONNECTION_UNIQUE):
            raise
        _fail(attempt_id, _Failure.GOOGLE_ACCOUNT_ALREADY_CONNECTED)


def _finish_ok(attempt_id, user_id, claims, result) -> None:
    """Runs inside ONE durable tenant_atomic. Order: membership re-check, the
    connection write and its audit row, then the conditional finish UPDATE. If
    that UPDATE matches no row the attempt is no longer PENDING, and raising
    rolls the connection and audit row back with it: a connection is never left
    behind an attempt the user sees as FAILED."""
    now = timezone.now()
    # A user removed from the team since clicking Connect must not create a
    # connection (spec decision P-2).
    if not TeamMember.objects.filter(user_id=user_id, accepted_at__isnull=False).exists():
        _mark_failed(attempt_id, _Failure.ACTOR_NOT_MEMBER, now)
        return
    connection, created, failure = _upsert_connection(claims, result, now)
    if failure is not None:
        _mark_failed(attempt_id, failure, now)
        return
    record(
        "google.connection_added" if created else "google.connection_reauthorized",
        actor=User.objects.get(pk=user_id),
        target=connection,
    )
    updated = GoogleOAuthState.objects.filter(pk=attempt_id, status=GoogleOAuthState.Status.PENDING).update(
        status=GoogleOAuthState.Status.COMPLETED,
        google_connection=connection,
        completed_at=now,
        code_enc=None,
        updated_at=now,
    )
    if updated != 1:
        raise AttemptNoLongerPending()


def _upsert_connection(claims, result, now):
    """Spec "Token handling". Returns (connection, created, failure_code).

    The existing (merchant, sub) row is read with select_for_update(), so
    attempts for an existing account serialize and the second takes the normal
    reauthorization path. With NO existing row both attempts INSERT, and
    UNIQUE(merchant, google_account_sub) makes the second raise; the caller
    handles that after the rollback. No refresh token is ever invented."""
    existing = GoogleConnection.objects.select_for_update().filter(google_account_sub=claims.sub).first()
    if existing is None:
        if result.refresh_token is None:
            return None, False, _Failure.REFRESH_TOKEN_MISSING
        connection = GoogleConnection.objects.create(
            merchant_id=_current_merchant_id(),
            google_account_sub=claims.sub,
            google_account_email=claims.email,
            access_token_enc=encrypt(result.access_token),
            refresh_token_enc=encrypt(result.refresh_token),
            expires_at=result.expires_at,
            status=GoogleConnection.Status.ACTIVE,
            connected_at=now,
        )
        return connection, True, None
    if result.refresh_token is None and existing.status != GoogleConnection.Status.ACTIVE:
        # The stored refresh token is presumed dead: keeping it would make a
        # falsely ACTIVE connection.
        return None, False, _Failure.REFRESH_TOKEN_MISSING
    existing.access_token_enc = encrypt(result.access_token)
    if result.refresh_token is not None:
        existing.refresh_token_enc = encrypt(result.refresh_token)
    existing.google_account_email = claims.email
    existing.expires_at = result.expires_at
    existing.status = GoogleConnection.Status.ACTIVE
    existing.connected_at = now
    existing.save(
        update_fields=[
            "access_token_enc",
            "refresh_token_enc",
            "google_account_email",
            "expires_at",
            "status",
            "connected_at",
            "updated_at",
        ]
    )
    return existing, False, None


# --- token refresh (task path only; spec "Token handling") ----------------------------


def mark_needs_reauth(connection_id, *, refresh_token_enc_seen: str, reason: str) -> bool:
    """ACTIVE -> NEEDS_REAUTH, only while the refresh token is still the one
    that was read, so a reauthorization that committed meanwhile is never
    overwritten. Called only for GoogleAuthRevoked, never for a timeout, 5xx or
    rate limit. No audit row (spec OD-9): one structured warning with only the
    connection UUID and a fixed reason."""
    now = timezone.now()
    with tenant_atomic():
        changed = GoogleConnection.objects.filter(
            pk=connection_id,
            status=GoogleConnection.Status.ACTIVE,
            refresh_token_enc=refresh_token_enc_seen,
        ).update(status=GoogleConnection.Status.NEEDS_REAUTH, updated_at=now)
    if changed:
        logger.warning("google.connection_needs_reauth connection=%s reason=%s", connection_id, reason)
    return bool(changed)


def refresh_connection(connection_id) -> None:
    """Refresh the access token when it is inside the margin. Task path only:
    never called from a request. The persisting UPDATE is conditional on the
    connection still being ACTIVE with the refresh token that was read, so it
    never overwrites a reauthorization; a zero-row result discards the refreshed
    token. A transient or rate-limit error propagates and changes nothing."""
    with tenant_atomic():
        connection = GoogleConnection.objects.filter(pk=connection_id).first()
    if connection is None or connection.status != GoogleConnection.Status.ACTIVE:
        return
    margin = timedelta(seconds=settings.GOOGLE_TOKEN_REFRESH_MARGIN_SECONDS)
    if connection.expires_at > timezone.now() + margin:
        return
    seen = connection.refresh_token_enc
    try:
        refresh_token = decrypt(seen)
    except InvalidToken:
        logger.error("Google connection %s holds an undecryptable refresh token", connection_id)
        return
    try:
        tokens = get_provider().refresh(refresh_token)
    except GoogleAuthRevoked as exc:
        mark_needs_reauth(connection_id, refresh_token_enc_seen=seen, reason=exc.reason)
        return
    now = timezone.now()
    updates = {
        "access_token_enc": encrypt(tokens.access_token),
        "expires_at": tokens.expires_at,
        "updated_at": now,
    }
    if tokens.refresh_token:
        updates["refresh_token_enc"] = encrypt(tokens.refresh_token)
    with tenant_atomic():
        GoogleConnection.objects.filter(
            pk=connection_id, status=GoogleConnection.Status.ACTIVE, refresh_token_enc=seen
        ).update(**updates)


def get_access_token(connection_id) -> str | None:
    """Task path: refresh if needed, then the decrypted access token of an ACTIVE
    connection, or None when it is gone or needs reauthorization."""
    refresh_connection(connection_id)
    with tenant_atomic():
        connection = GoogleConnection.objects.filter(
            pk=connection_id, status=GoogleConnection.Status.ACTIVE
        ).first()
    return decrypt(connection.access_token_enc) if connection is not None else None


def sweep_oauth_attempts() -> None:
    """The daily expire rules and purge for the current merchant."""
    now = timezone.now()
    with tenant_atomic():
        expire_attempts(now=now)
        purge_attempts(now=now)


def refresh_merchant_tokens() -> None:
    """For each ACTIVE connection of the current merchant: refresh_connection,
    which acts only on those inside the margin. One connection failing is
    logged by class name and the loop goes on. Also sweeps OAuth attempts."""
    sweep_oauth_attempts()
    with tenant_atomic():
        ids = list(
            GoogleConnection.objects.filter(status=GoogleConnection.Status.ACTIVE).values_list("pk", flat=True)
        )
    for connection_id in ids:
        try:
            refresh_connection(connection_id)
        except Exception as exc:
            logger.warning("Google token refresh of connection %s failed with %s", connection_id, type(exc).__name__)


def merchant_ids_with_google() -> list[uuid.UUID]:
    """The ids of every non-DELETED merchant. Merchant is a global, non-RLS
    table, so reading its ids is not an RLS bypass. The Google tables are RLS
    protected and cannot be listed across merchants, so each merchant is
    entered under its own tenant context and finds nothing to do if it has no
    connection."""
    return list(Merchant.objects.exclude(status=Merchant.Status.DELETED).values_list("id", flat=True))


# --- request path: location list and mapping (non-atomic views, spec P-1 / P-5) -------
#
# Each function below runs in a view that has the tenant context but NO
# request-wide transaction. Every DB access is inside a short tenant_atomic()
# block, and every Google call runs between blocks with no transaction open.


def list_connections():
    """The merchant connections, for GET /google/connections (the NEEDS_REAUTH
    banner). The caller paginates."""
    return GoogleConnection.objects.all()


def _read_token_in_block(connection) -> str | None:
    """Block A. The decrypted access token, or None when a refresh is needed.
    Then a refresh task is registered with on_commit INSIDE this block, and the
    caller must let the block exit normally: raising inside it would roll the
    block back and discard the callback. A request never refreshes a token."""
    if connection.status != GoogleConnection.Status.ACTIVE:
        raise GoogleNeedsReauth()
    margin = timedelta(seconds=settings.GOOGLE_TOKEN_REFRESH_MARGIN_SECONDS)
    if connection.expires_at <= timezone.now() + margin:
        _enqueue_on_commit("refresh_connection_token", str(connection.merchant_id), str(connection.pk))
        return None
    return decrypt(connection.access_token_enc)


def _assert_same_merchant(location, connection) -> None:
    """location.merchant == connection.merchant == the current merchant. FK
    checks bypass RLS, so this is enforced here, before any write. The error is
    the same one an unknown google_location_id gets (spec P-4)."""
    if not (location.merchant_id == connection.merchant_id == _current_merchant_id()):
        raise GoogleLocationNotAvailable()


def _list_google_locations(token):
    """The one place a request-path Google failure becomes an API error. The
    message is fixed text: no provider content ever reaches a response (the
    provider itself logs the exception class only). Provider classes and the
    task paths are unchanged."""
    try:
        return get_provider().list_locations(token)  # no transaction is open
    except GoogleRateLimited:
        raise GoogleRateLimitedError() from None
    except GoogleTransientError:
        raise GoogleUnavailable() from None
    except GooglePermanentError:
        raise GoogleRequestFailed() from None


def list_available_locations(connection_id) -> list[dict]:
    """GET /google/connections/{id}/locations. Only read-only Google calls."""
    with tenant_atomic():  # block A
        connection = GoogleConnection.objects.filter(pk=connection_id).first()
        if connection is None:
            raise GoogleNotFound()
        token = _read_token_in_block(connection)
    if token is None:
        raise GoogleTokenRefreshPending()
    found = _list_google_locations(token)
    with tenant_atomic():
        mapped = dict(
            GoogleLocation.objects.filter(google_connection_id=connection_id).values_list(
                "google_location_id", "location_id"
            )
        )
    return [
        {
            "google_location_id": item.google_location_id,
            "title": item.title,
            "has_place_id": item.google_place_id is not None,
            "has_review_link": item.review_uri is not None,
            "mapped_location_id": mapped.get(item.google_location_id),
        }
        for item in found
    ]


def map_location(*, team_member, location_id, connection_id, google_location_id) -> GoogleLocation:
    """PUT /locations/{id}/google-location. The Place ID and the review link
    come only from the provider list, never from the request (spec RL-1, OD-3).
    Errors: 404 for an unknown or foreign location or connection (a MANAGER:
    an unassigned location); 422 google_location_not_available for an unknown
    google_location_id or a cross-merchant pair; 422 google_place_id_missing;
    422 google_review_link_missing; 409 google_location_already_mapped."""
    with tenant_atomic():  # block A
        location = get_accessible_location(team_member, location_id)
        connection = GoogleConnection.objects.filter(pk=connection_id).first()
        if connection is None:
            raise GoogleNotFound()
        _assert_same_merchant(location, connection)
        token = _read_token_in_block(connection)
    if token is None:
        raise GoogleTokenRefreshPending()
    found = next(
        (item for item in _list_google_locations(token) if item.google_location_id == google_location_id),
        None,
    )
    if found is None:
        raise GoogleLocationNotAvailable()
    if not found.google_place_id:
        raise GooglePlaceIdMissing()
    review_link = validate_review_link(found.review_uri)
    try:
        with tenant_atomic():  # block B
            return _write_mapping(location, connection_id, found, review_link)
    except IntegrityError as exc:
        # Caught outside block B, after it rolled back (spec OD-7). The loser of
        # two concurrent first mappings violates the location_id unique index
        # instead; its rollback also discards its on_commit sync.
        if _violated_constraint(exc, _LOCATION_UNIQUE) or _violated_constraint(exc, _LOCATION_ONE_TO_ONE):
            raise GoogleLocationAlreadyMapped() from None
        raise


def _write_mapping(location, connection_id, found, review_link) -> GoogleLocation:
    connection = GoogleConnection.objects.filter(
        pk=connection_id, status=GoogleConnection.Status.ACTIVE
    ).first()
    if connection is None:
        raise GoogleNeedsReauth()
    _assert_same_merchant(location, connection)
    mapping = GoogleLocation.objects.select_for_update().filter(location=location).first()
    if mapping is None:
        mapping = GoogleLocation(location=location)
    elif mapping.google_location_id != found.google_location_id:
        # A deliberate remap to a different Google location: the old Google
        # location's reviews must not appear under the new mapping. This is not
        # OD-8 (reviews deleted upstream are kept). The row lock above
        # serializes this with _upsert_reviews / the counters update.
        deleted, _ = GoogleReview.objects.filter(google_location=mapping).delete()
        mapping.average_rating = None
        mapping.review_count = None
        mapping.last_synced_at = None
        logger.info("Google location %s remapped: %s old reviews deleted", mapping.pk, deleted)
    # A repoint replaces all three Google values together.
    mapping.google_connection = connection
    mapping.google_location_id = found.google_location_id
    mapping.google_place_id = found.google_place_id
    mapping.review_link = review_link
    mapping.save()
    _enqueue_on_commit("sync_location", str(connection.merchant_id), str(mapping.pk))
    return mapping


def get_location_mapping(location):
    """GET /locations/{id}/google-location: the mapping or None."""
    return GoogleLocation.objects.select_related("google_connection").filter(location=location).first()


# --- review sync (task path; spec "Review upsert") ------------------------------------


def _upsert_reviews(google_location_pk, reviews, expected_google_location_id) -> bool:
    """Returns False when the mapping no longer points at the Google location
    this sync started with (a remap committed meanwhile): nothing is written.
    The mapping row is locked for the write, and a remap locks the same row, so
    the two serialize.

    One page of reviews in one INSERT ... ON CONFLICT DO UPDATE. The field
    names were checked against the installed Django: unique_fields takes the
    model field name google_location (column google_location_id). A review
    without a usable rating or timestamp is skipped (rating and
    review_created_at are not null), and duplicate ids within the page are
    collapsed, because PostgreSQL rejects one statement touching a row twice."""
    now = timezone.now()
    rows = {}
    for review in reviews:
        if review.rating is None or review.review_created_at is None or len(review.google_review_id) > 255:
            continue
        rows[review.google_review_id] = GoogleReview(
            google_location_id=google_location_pk,
            google_review_id=review.google_review_id,
            reviewer_name=review.reviewer_name[:255] if review.reviewer_name else None,
            rating=review.rating,
            text=review.text,
            review_created_at=review.review_created_at,
            fetched_at=now,
        )
    if not rows:
        return True
    with tenant_atomic():
        if not (
            GoogleLocation.objects.select_for_update()
            .filter(pk=google_location_pk, google_location_id=expected_google_location_id)
            .exists()
        ):
            return False
        GoogleReview.objects.bulk_create(
            list(rows.values()),
            update_conflicts=True,
            unique_fields=["google_location", "google_review_id"],
            update_fields=["reviewer_name", "rating", "text", "review_created_at", "fetched_at", "updated_at"],
        )
    return True


def sync_location_reviews(google_location_pk) -> bool:
    """Sync one mapped location. Returns True when Google rate-limited the run
    (it stops quietly and last_synced_at is left unchanged, so the next Beat
    tick resumes it: no retry, no countdown). A location Google rejects (for
    example one that is not verified) is skipped and logged by UUID; it never
    causes NEEDS_REAUTH. A transient error propagates."""
    with tenant_atomic():
        location = GoogleLocation.objects.select_related("google_connection").filter(pk=google_location_pk).first()
    if location is None or location.google_connection.status != GoogleConnection.Status.ACTIVE:
        return False
    token = get_access_token(location.google_connection_id)
    if token is None:
        return False
    provider = get_provider()
    page_token, seen, average, count = None, set(), None, None
    try:
        while True:
            page = provider.list_reviews(token, location.google_location_id, page_token)  # no transaction open
            if not _upsert_reviews(location.pk, page.reviews, location.google_location_id):
                logger.info("Google review sync of location %s stopped: remapped meanwhile", google_location_pk)
                return False
            if page_token is None:
                average, count = page.average_rating, page.review_count
            page_token = page.next_page_token
            if not page_token:
                break
            if page_token in seen:
                raise GooglePermanentError("Google repeated a page token.")
            seen.add(page_token)
    except GoogleRateLimited:
        logger.info("Google review sync of location %s stopped: rate limited", google_location_pk)
        return True
    except GooglePermanentError:
        logger.warning("Google review sync skipped location %s: refused by Google", google_location_pk)
        return False
    now = timezone.now()
    with tenant_atomic():
        # Conditional on the identity this sync started with: a remap that
        # committed meanwhile leaves the reset counters alone (0 rows).
        GoogleLocation.objects.filter(pk=location.pk, google_location_id=location.google_location_id).update(
            average_rating=average, review_count=count, last_synced_at=now, updated_at=now
        )
    return False


def sync_merchant_reviews() -> None:
    """Sync every due mapped location of the current merchant whose connection
    is ACTIVE (last_synced_at older than GOOGLE_SYNC_INTERVAL_SECONDS, which is
    the stagger). A rate limit ends the whole run. One location failing is
    logged by class name and the loop goes on."""
    cutoff = timezone.now() - timedelta(seconds=settings.GOOGLE_SYNC_INTERVAL_SECONDS)
    with tenant_atomic():
        ids = list(
            GoogleLocation.objects.filter(google_connection__status=GoogleConnection.Status.ACTIVE)
            .filter(Q(last_synced_at__isnull=True) | Q(last_synced_at__lte=cutoff))
            .values_list("pk", flat=True)
        )
    for pk in ids:
        try:
            if sync_location_reviews(pk):
                return
        except Exception as exc:
            logger.warning("Google review sync of location %s failed with %s", pk, type(exc).__name__)


def list_reviews(*, team_member=None, location_id=None, date_from=None, date_to=None, rating_min=None):
    """GET /reviews. A session request passes its team_member, so a MANAGER sees
    only assigned locations; an API key has none and is merchant-wide. A
    location_id outside that scope simply matches nothing (an empty page)."""
    reviews = GoogleReview.objects.select_related("google_location")
    if team_member is not None:
        reviews = reviews.filter(google_location__location__in=accessible_locations(team_member))
    if location_id is not None:
        reviews = reviews.filter(google_location__location_id=location_id)
    if date_from is not None:
        reviews = reviews.filter(review_created_at__gte=date_from)
    if date_to is not None:
        reviews = reviews.filter(review_created_at__lte=date_to)
    if rating_min is not None:
        reviews = reviews.filter(rating__gte=rating_min)
    return reviews
