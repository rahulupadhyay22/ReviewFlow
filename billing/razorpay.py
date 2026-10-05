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
            raise BillingProviderRejected(_provider_error_code(error_body)) from None
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


def create_subscription(provider_plan_id: str) -> dict:
    """No open-ended subscription exists: total_count is required, and 1200
    monthly cycles is 100 years, Razorpay's maximum (V1; T2 unconfirmed, so it
    is a setting). Sends no notes: nothing about the merchant reaches Razorpay."""
    return _request(
        "POST",
        "/v1/subscriptions",
        {
            "plan_id": provider_plan_id,
            "total_count": settings.RAZORPAY_SUBSCRIPTION_TOTAL_COUNT,
            "customer_notify": True,
        },
        operation="create_subscription",
    )


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
