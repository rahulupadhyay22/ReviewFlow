"""Shared payload validation for provider adapters
(Integration-Architecture.md §"Idempotency & Normalization"; Security-Controls.md
§"Rate Limiting & Abuse Prevention" — input validation at the adapter boundary).

Per-provider raw-payload schemas are added here by each adapter phase
(06/17). Phase 04 only has the shared primitives every adapter's
normalize() output must satisfy.
"""
import re

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
