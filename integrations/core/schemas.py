"""Shared payload validation for provider adapters
(Integration-Architecture.md §"Idempotency & Normalization"; Security-Controls.md
§"Rate Limiting & Abuse Prevention" — input validation at the adapter boundary).

Per-provider raw-payload schemas are added here by each adapter phase
(06/17). Phase 04 has the shared primitives every adapter's normalize()
output must satisfy; Phase 06 (spec Decisions 7, 16) adds the receipt-time
idempotency key and the field-mapping/HMAC helpers shared by the Generic
Webhook, CSV and REST adapters, plus (Phase 06 Shopify workstream) the
Shopify receiver's HMAC check.
"""
import hashlib
import hmac
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from core.exceptions import ReviewFlowError

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class PayloadValidationError(ReviewFlowError):
    """Raised by SaleCreated.__post_init__ or an adapter's parse()/normalize().

    Constructed with a stable CODE only (spec Decision 19/6) -- never a
    free-text message, and never the offending value, which may be a phone
    number or other PII. events.services._safe_error() maps the code to the
    SAFE_ERRORS table; an unrecognized code is stored as PAYLOAD_INVALID.
    """

    def __init__(self, code: str = "PAYLOAD_INVALID"):
        self.code = code
        super().__init__(code)


def validate_e164(phone: str) -> str:
    """Raises PayloadValidationError("INVALID_PHONE") for a non-blank value
    that isn't a valid E.164 number. Callers normalize blank/whitespace-only
    to None before calling this (spec Decision 17) -- a missing phone is not
    a validation failure."""
    if not E164_RE.match(phone):
        raise PayloadValidationError("INVALID_PHONE")
    return phone


def validate_currency(code: str) -> str:
    if not CURRENCY_RE.match(code or ""):
        raise PayloadValidationError("INVALID_CURRENCY")
    return code


def parse_amount(value) -> Decimal:
    """Shared amount parser for adapters without a native decimal type
    (Generic Webhook, CSV, REST) -- spec Decision 7/16."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise PayloadValidationError("INVALID_AMOUNT")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise PayloadValidationError("INVALID_AMOUNT") from None


def parse_occurred_at(value) -> datetime:
    """Shared ISO-8601 occurred_at parser (spec Decision 7/16). Must carry a
    timezone -- a naive result is caught by SaleCreated.__post_init__ too,
    but adapters call this directly to get the same safe error code for an
    unparsable string."""
    if value is None:
        raise PayloadValidationError("INVALID_OCCURRED_AT")
    if isinstance(value, datetime):
        return value
    try:
        text = str(value)
        # datetime.fromisoformat doesn't accept a trailing "Z" before
        # Python 3.11's relaxed parser landed everywhere we run; normalize
        # it defensively so a common ISO-8601 form always works.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        raise PayloadValidationError("INVALID_OCCURRED_AT") from None


def sale_event_key(external_location_id: str | None, external_transaction_id: str) -> str:
    """Deterministic receipt-time external_event_id for sources with no
    native provider event id (Generic Webhook, CSV, REST API -- spec
    Decision 7). Distinct per (external_location_id, external_transaction_id)
    so two locations sharing a transaction id never collide at the event
    level, and always fits CharField(255) regardless of the transaction id's
    length. \x1f (unit separator) can never appear in either input's normal
    use, and even if it did the hash still can't collide two distinct pairs
    with different location/id boundaries onto the same digest in practice.
    """
    raw = f"{external_location_id or ''}\x1f{external_transaction_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def resolve_path(obj, dotted_path: str):
    """Resolves a dot-separated path against nested dicts/lists (spec
    Decision 16, Generic Webhook field mapping). A numeric segment indexes a
    list. Returns None when any segment is missing/out of range -- callers
    decide whether that's fatal for a required field."""
    current = obj
    for segment in dotted_path.split("."):
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list):
            try:
                idx = int(segment)
            except ValueError:
                return None
            current = current[idx] if 0 <= idx < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


def verify_hmac_sha256(secret: str, body: bytes, signature: str, *, encoding: str = "hex") -> bool:
    """Constant-time HMAC-SHA256 verification shared by every webhook
    receiver (spec Decision 1, 3, 5). Fails closed: a missing/malformed
    signature, or an unexpected encoding, returns False rather than
    raising -- callers never need a try/except around this."""
    if not signature or not secret:
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256)
    try:
        if encoding == "hex":
            expected = digest.hexdigest()
        elif encoding == "base64":
            import base64

            expected = base64.b64encode(digest.digest()).decode()
        else:
            return False
        # compare_digest raises TypeError for a non-ASCII str (e.g. a
        # crafted header) -- fold that into the same fail-closed False so
        # every malformed signature gets the identical 401, never a 500.
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False
