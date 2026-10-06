"""The only module that talks to Razorpay (spec 07 Decision 15: the V1
payment-provider seam; no generic provider interface). No business logic, no
database access; called only from billing/services.py.

Stdlib urllib with HTTP Basic auth and an explicit timeout, the same helper
pattern as integrations/shopify/services.py. Exception text never contains a
response body or a credential, and nothing here logs a body, a payment id or a
secret: only the exception class name.
"""
import base64
import hashlib
import hmac
import http.client
import json
import logging
import re
import urllib.error
import urllib.request

from django.conf import settings

from billing.exceptions import (
    BillingNotConfigured,
    BillingProviderRejected,
    BillingProviderUnavailable,
)
from core.tenancy import is_valid_billing_ref

logger = logging.getLogger(__name__)

BASE_URL = "https://api.razorpay.com"
_HTTP_TIMEOUT = 10  # seconds; the two OWNER endpoints call this under a row lock

# G-0 (spec 07-plan-change-replacement): the refusals that mean "this subscription
# cannot be updated" (UPI, eMandate, domestic card), as (HTTP status,
# provider error code, reason). It ships EMPTY on purpose: Razorpay's refusal for
# an unsupported update is not evidenced, and the generic BAD_REQUEST_ERROR is
# shared by many refusals. Entries are added only from recorded evidence, in a
# reviewed change; until then nothing classifies, even with a flag on.
UPDATE_UNSUPPORTED_REFUSALS: frozenset[tuple[int, str, str]] = frozenset()

# G-2: the entity field a downgrade replacement's start is verified from. It is
# None until Razorpay's future-start behavior is evidenced, so no downgrade
# replacement is created (the merged 409 plan_change_unsupported stands).
DOWNGRADE_START_FIELD: str | None = None

# A `reason` is kept only if it is a short identifier. Free text (a description)
# never matches, so it cannot be stored or leaked through this field.
_REASON = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _check_ref(ref: str) -> str:
    # The ref goes into a URL path: refuse anything that is not an id.
    if not is_valid_billing_ref(ref):
        raise ValueError("ref must match ^[A-Za-z0-9_]{1,64}$.")
    return ref


def _provider_error_code(body: bytes) -> str | None:
    """The provider's error code only, never its description."""
    try:
        code = json.loads(body)["error"]["code"]
    except (ValueError, TypeError, KeyError):
        return None
    return code if isinstance(code, str) and len(code) <= 64 else None


def _provider_error_reason(body: bytes) -> str | None:
    """The error's short `reason` identifier, or None. Never the description."""
    try:
        reason = json.loads(body)["error"]["reason"]
    except (ValueError, TypeError, KeyError):
        return None
    return reason if isinstance(reason, str) and _REASON.fullmatch(reason) else None


def _request(method: str, path: str, body: dict | None = None, *, operation: str) -> dict:
    """`operation` is a fixed label, the only thing logged about the call: a
    path can carry a subscription ref."""
    key_id, key_secret = settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET
    if not key_id or not key_secret:
        raise BillingNotConfigured()
    token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # HTTPError is a URLError: it must be caught first.
        status = exc.code
        try:
            error_body = exc.read()
        except (OSError, http.client.HTTPException):
            error_body = b""
        logger.warning("Razorpay %s answered %s", operation, status)
        if 400 <= status < 500:
            raise BillingProviderRejected(
                _provider_error_code(error_body),
                status=status,
                reason=_provider_error_reason(error_body),
            ) from None
        raise BillingProviderUnavailable() from None
    # http.client.HTTPException (e.g. IncompleteRead) is not an OSError.
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        logger.warning("Razorpay request failed with %s", type(exc).__name__)
        raise BillingProviderUnavailable() from None
    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        raise BillingProviderUnavailable() from None
    if not isinstance(result, dict):
        raise BillingProviderUnavailable()
    return result


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    """X-Razorpay-Signature: hex HMAC-SHA256 of the raw body keyed with the
    webhook secret (T3). An unset/empty secret rejects everything: an HMAC is
    never computed with an empty key."""
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret or not signature or not isinstance(raw_body, (bytes, bytearray)):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode("utf-8", "replace"))


def _unix_time(name: str, value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive unix timestamp.")
    return value


def create_subscription(
    provider_plan_id: str, *, start_at: int | None = None, expire_by: int | None = None
) -> dict:
    """No open-ended subscription exists: total_count is required, and 1200
    monthly cycles is 100 years, Razorpay's maximum (V1; T2 unconfirmed, so it
    is a setting). Sends no notes: nothing about the merchant reaches Razorpay.

    `start_at` and `expire_by` (the documented create fields, V1) are sent only
    when given, so a plain create's body is exactly what it always was. What
    Razorpay does with them is not verified (G-1, G-2, G-3)."""
    body = {
        "plan_id": provider_plan_id,
        "total_count": settings.RAZORPAY_SUBSCRIPTION_TOTAL_COUNT,
        "customer_notify": True,
    }
    if start_at is not None:
        body["start_at"] = _unix_time("start_at", start_at)
    if expire_by is not None:
        body["expire_by"] = _unix_time("expire_by", expire_by)
    return _request("POST", "/v1/subscriptions", body, operation="create_subscription")


def fetch_subscription(ref: str) -> dict:
    return _request("GET", f"/v1/subscriptions/{_check_ref(ref)}", operation="fetch_subscription")


def fetch_invoices(ref: str) -> list[dict]:
    """NOT IMPLEMENTED -- stopped on purpose (approved plan, W2).

    The paid-entitlement rule needs the subscription's invoices, and a first
    page must never be assumed to be all of them. Razorpay's documentation
    (read 2026-09-30, the fetch-invoices page) lists only `subscription_id`
    as a query parameter and documents no page size, no `count`/`skip`, and
    no rule for obtaining the complete list. How to obtain the complete set
    is an open question for the user. Until it is answered this raises
    loudly instead of returning a list that may be partial.
    """
    _check_ref(ref)
    raise NotImplementedError("fetch_invoices: invoice-list pagination is not established")


def update_subscription(ref: str, provider_plan_id: str, schedule_change_at: str) -> dict:
    """schedule_change_at is "now" (immediate upgrade) or "cycle_end"."""
    if schedule_change_at not in ("now", "cycle_end"):
        raise ValueError("schedule_change_at must be 'now' or 'cycle_end'.")
    return _request(
        "PATCH",
        f"/v1/subscriptions/{_check_ref(ref)}",
        {"plan_id": provider_plan_id, "schedule_change_at": schedule_change_at},
        operation="update_subscription",
    )


def cancel_subscription(ref: str, at_cycle_end: bool) -> dict:
    return _request(
        "POST",
        f"/v1/subscriptions/{_check_ref(ref)}/cancel",
        {"cancel_at_cycle_end": bool(at_cycle_end)},
        operation="cancel_subscription",
    )
