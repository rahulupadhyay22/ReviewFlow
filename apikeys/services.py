"""
apikeys business logic (spec .claude/specs/05-public-api-keys.md
§"Services & background tasks"). Views, the middleware, and the DRF
authenticator all call into here -- no business logic there
(Coding-Standards.md §1).
"""
import hashlib
import re
import secrets
import uuid
from datetime import timedelta

from django.db.models import Q, QuerySet
from django.utils import timezone

from accounts.models import Merchant, TeamMember
from apikeys.exceptions import ApiKeyNotFound, InvalidApiKey, InvalidScopes
from apikeys.models import ApiKey
from auditlog.services import record
from core.tenancy import api_key_lookup_atomic, get_current_merchant_id, tenant_atomic, tenant_context

_KEY_RE = re.compile(r"^rf_live_[A-Za-z0-9_-]{32}$")
_TOUCH_INTERVAL = timedelta(seconds=60)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _validate_scopes(scopes) -> list[str]:
    if not isinstance(scopes, list) or not scopes:
        raise InvalidScopes("scopes must be a non-empty list.")
    allowed = set(ApiKey.Scope.values)
    if not all(isinstance(s, str) and s in allowed for s in scopes):
        raise InvalidScopes("Unknown API key scope.")
    if len(set(scopes)) != len(scopes):
        raise InvalidScopes("Duplicate API key scopes.")
    return scopes


def create_api_key(*, actor: TeamMember, scopes: list[str]) -> tuple[ApiKey, str]:
    """Generates rf_live_<32 url-safe chars>, stores only its sha256 hash,
    and returns (key, raw). raw is never stored, logged, or audited -- the
    caller (the view) is the only place it is returned to the client, once."""
    scopes = _validate_scopes(scopes)
    raw = "rf_live_" + secrets.token_urlsafe(24)
    key_hash = _hash(raw)
    # A key_hash collision (192 random bits, then sha256) is an accepted,
    # practically-impossible risk under the approved Phase 05 design:
    # UNIQUE(key_hash) is the guard, and a collision surfaces as an
    # IntegrityError (500) rather than being retried or remapped.
    with tenant_atomic():
        key = ApiKey.objects.create(
            merchant_id=get_current_merchant_id(), key_hash=key_hash, scopes_json=scopes
        )
        record("api_key.created", actor=actor.user, target=key, metadata={"scopes": scopes})
    return key, raw


def list_api_keys() -> QuerySet[ApiKey]:
    return ApiKey.objects.all()


def get_api_key(key_id: uuid.UUID | str) -> ApiKey:
    key = ApiKey.objects.filter(id=key_id).first()
    if key is None:
        raise ApiKeyNotFound()
    return key


def revoke_api_key(*, actor: TeamMember, api_key: ApiKey) -> None:
    """Idempotent via a conditional UPDATE, not check-then-set. A second
    revoke changes nothing and writes no second audit row."""
    with tenant_atomic():
        updated = ApiKey.objects.filter(pk=api_key.pk, is_active=True).update(is_active=False)
        if updated:
            record("api_key.revoked", actor=actor.user, target=api_key)


def authenticate_api_key(raw: str) -> ApiKey:
    """The pre-tenant lookup (Multi-Tenancy.md §"API key lookup"). Raises
    InvalidApiKey -- one generic reason -- for a malformed token, an unknown
    key, a revoked key, or a key whose merchant is not ACTIVE. This is a
    point-in-time check: nothing is locked, so a key revoked or a merchant
    suspended after this call does not retroactively cancel an in-flight
    request (spec Decisions 11-12)."""
    if not _KEY_RE.match(raw or ""):
        raise InvalidApiKey("Malformed API key.")
    key_hash = _hash(raw)
    with api_key_lookup_atomic(key_hash):
        key = ApiKey.objects.for_lookup_hash(key_hash).select_related("merchant").first()
    if key is None or not key.is_active or key.merchant.status != Merchant.Status.ACTIVE:
        raise InvalidApiKey("Invalid or revoked API key.")
    return key


def touch_last_used(api_key: ApiKey) -> None:
    """Best-effort operational metadata, never an authentication dependency
    (spec Decision 6). Its own short transaction, separate from both the
    pre-tenant lookup (T1) and the request-wide tenant transaction (T3), so
    its UPDATE never holds a row lock for the whole request. At most once
    per 60s per key: a zero-row match (no update needed) takes no lock."""
    with tenant_context(api_key.merchant_id), tenant_atomic():
        ApiKey.objects.filter(pk=api_key.pk).filter(
            Q(last_used_at__isnull=True) | Q(last_used_at__lt=timezone.now() - _TOUCH_INTERVAL)
        ).update(last_used_at=timezone.now())
