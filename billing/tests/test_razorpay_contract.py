"""Contract tests for billing/razorpay.py. No network: urlopen is patched.

The JSON fixtures are built from the fields Razorpay documents for these
entities (Billing-Specification.md §M, F4/F5 and the invoice fields listed
there); they are not captured traffic. Real-mode evidence is the T1/T4/T7
merge gates, which these tests do not replace."""
import hashlib
import hmac
import io
import json
import logging
import urllib.error
from pathlib import Path

import pytest

from billing import razorpay
from billing.exceptions import (
    BillingNotConfigured,
    BillingProviderRejected,
    BillingProviderUnavailable,
)

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "whsec_test_value"


@pytest.fixture(autouse=True)
def creds(settings):
    settings.RAZORPAY_KEY_ID = "rzp_test_keyid"
    settings.RAZORPAY_KEY_SECRET = "key_secret_value"
    settings.RAZORPAY_WEBHOOK_SECRET = SECRET
    settings.RAZORPAY_SUBSCRIPTION_TOTAL_COUNT = 1200


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def urlopen(monkeypatch):
    """Records the Request and returns whatever the test queues."""
    state = {"request": None, "timeout": None, "result": _Resp(b"{}")}

    def fake(req, timeout=None):
        state["request"], state["timeout"] = req, timeout
        result = state["result"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(razorpay.urllib.request, "urlopen", fake)
    return state


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- signature ----------------------------------------------------------


def test_signature_accepts_the_hex_hmac_of_the_raw_body():
    body = (FIXTURES / "razorpay_subscription_charged.json").read_bytes()
    assert razorpay.verify_webhook_signature(body, _sign(body)) is True


def test_signature_rejects_missing_wrong_and_altered_bodies():
    body = b'{"event": "subscription.charged"}'
    assert razorpay.verify_webhook_signature(body, None) is False
    assert razorpay.verify_webhook_signature(body, "") is False
    assert razorpay.verify_webhook_signature(body, "0" * 64) is False
    assert razorpay.verify_webhook_signature(body + b" ", _sign(body)) is False
    assert razorpay.verify_webhook_signature(body, _sign(body, "another-secret")) is False
    assert razorpay.verify_webhook_signature(body, "é" * 64) is False  # non-ASCII, no TypeError


def test_signature_never_verifies_with_an_empty_secret(settings):
    settings.RAZORPAY_WEBHOOK_SECRET = ""
    body = b"{}"
    empty_key_signature = hmac.new(b"", body, hashlib.sha256).hexdigest()
    assert razorpay.verify_webhook_signature(body, empty_key_signature) is False


# --- request shape ------------------------------------------------------


def test_create_subscription_sends_basic_auth_timeout_and_total_count(urlopen):
    urlopen["result"] = _Resp(json.dumps({"id": "sub_New1", "status": "created"}).encode())
    out = razorpay.create_subscription("plan_X")
    req = urlopen["request"]
    assert out["id"] == "sub_New1"
    assert req.get_method() == "POST"
    assert req.full_url == "https://api.razorpay.com/v1/subscriptions"
    assert req.get_header("Authorization").startswith("Basic ")
    assert urlopen["timeout"] == razorpay._HTTP_TIMEOUT
    body = json.loads(req.data)
    assert body == {"plan_id": "plan_X", "total_count": 1200, "customer_notify": True}
    assert "notes" not in body


def test_fetch_subscription_reads_the_entity_fields_the_code_relies_on(urlopen):
    urlopen["result"] = _Resp((FIXTURES / "razorpay_subscription_entity.json").read_bytes())
    entity = razorpay.fetch_subscription("sub_TestSub000001")
    assert urlopen["request"].get_method() == "GET"
    assert urlopen["request"].full_url.endswith("/v1/subscriptions/sub_TestSub000001")
    for field in ("id", "plan_id", "status", "current_start", "current_end"):
        assert field in entity


def test_update_and_cancel_request_shapes(urlopen):
    razorpay.update_subscription("sub_1", "plan_Y", "cycle_end")
    req = urlopen["request"]
    assert req.get_method() == "PATCH"
    assert json.loads(req.data) == {"plan_id": "plan_Y", "schedule_change_at": "cycle_end"}

    razorpay.cancel_subscription("sub_1", True)
    req = urlopen["request"]
    assert req.get_method() == "POST"
    assert req.full_url.endswith("/v1/subscriptions/sub_1/cancel")
    assert json.loads(req.data) == {"cancel_at_cycle_end": True}


def test_update_rejects_an_unknown_schedule_value():
    with pytest.raises(ValueError):
        razorpay.update_subscription("sub_1", "plan_Y", "later")


@pytest.mark.parametrize("ref", ["", "sub/../x", "sub 1", "sub_1?x=1", "sub_1" + chr(10), "s" * 65, None])
def test_a_ref_that_is_not_an_id_never_reaches_the_url(urlopen, ref):
    with pytest.raises(ValueError):
        razorpay.fetch_subscription(ref)
    assert urlopen["request"] is None


# --- errors -------------------------------------------------------------


def test_unset_credentials_raise_billing_not_configured(settings, urlopen):
    settings.RAZORPAY_KEY_SECRET = ""
    with pytest.raises(BillingNotConfigured):
        razorpay.fetch_subscription("sub_1")
    assert urlopen["request"] is None


def _http_error(status, body=b""):
    return urllib.error.HTTPError("https://api.razorpay.com/x", status, "msg", {}, io.BytesIO(body))


def test_4xx_is_rejected_with_the_provider_code_only(urlopen):
    secret_text = "SENSITIVE-DESCRIPTION"
    body = json.dumps({"error": {"code": "BAD_REQUEST_ERROR", "description": secret_text}}).encode()
    urlopen["result"] = _http_error(400, body)
    with pytest.raises(BillingProviderRejected) as exc_info:
        razorpay.update_subscription("sub_1", "plan_Y", "now")
    assert exc_info.value.provider_code == "BAD_REQUEST_ERROR"
    assert secret_text not in str(exc_info.value)


def test_4xx_with_an_unparseable_body_is_still_rejected(urlopen):
    urlopen["result"] = _http_error(400, b"<html>")
    with pytest.raises(BillingProviderRejected) as exc_info:
        razorpay.fetch_subscription("sub_1")
    assert exc_info.value.provider_code is None


@pytest.mark.parametrize(
    "failure",
    [
        _http_error(500),
        _http_error(503),
        urllib.error.URLError("dns"),
        TimeoutError(),
        ConnectionResetError(),
    ],
)
def test_5xx_and_transport_failures_are_unavailable(urlopen, failure):
    urlopen["result"] = failure
    with pytest.raises(BillingProviderUnavailable):
        razorpay.fetch_subscription("sub_1")


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b""])
def test_an_unparseable_or_non_object_success_is_unavailable(urlopen, raw):
    urlopen["result"] = _Resp(raw)
    with pytest.raises(BillingProviderUnavailable):
        razorpay.fetch_subscription("sub_1")


def test_errors_and_logs_never_contain_the_body_or_credentials(urlopen, caplog):
    body = json.dumps({"error": {"code": "X", "description": "LEAKY-BODY"}}).encode()
    urlopen["result"] = _http_error(400, body)
    with caplog.at_level(logging.DEBUG, logger="billing"):
        with pytest.raises(BillingProviderRejected):
            razorpay.fetch_subscription("sub_1")
    text = caplog.text
    for forbidden in ("LEAKY-BODY", "key_secret_value", "rzp_test_keyid", SECRET):
        assert forbidden not in text


@pytest.mark.parametrize("status", [400, 503])
@pytest.mark.parametrize(
    "operation, call",
    [
        ("fetch_subscription", lambda ref: razorpay.fetch_subscription(ref)),
        ("update_subscription", lambda ref: razorpay.update_subscription(ref, "plan_x", "now")),
        ("cancel_subscription", lambda ref: razorpay.cancel_subscription(ref, True)),
    ],
)
def test_a_provider_error_logs_a_fixed_operation_label_never_the_ref(urlopen, caplog, status, operation, call):
    """The cancel path is /v1/subscriptions/<ref>/cancel: deriving the logged
    label from the path used to leave the ref in the log line."""
    ref = "sub_LogMarker0001"
    urlopen["result"] = _http_error(status)
    with caplog.at_level(logging.DEBUG, logger="billing"):
        with pytest.raises((BillingProviderRejected, BillingProviderUnavailable)):
            call(ref)
    assert f"Razorpay {operation} answered {status}" in caplog.text
    assert ref not in caplog.text and "/v1/" not in caplog.text


def test_a_create_error_logs_its_fixed_operation_label(urlopen, caplog):
    urlopen["result"] = _http_error(400)
    with caplog.at_level(logging.DEBUG, logger="billing"):
        with pytest.raises(BillingProviderRejected):
            razorpay.create_subscription("plan_x")
    assert "Razorpay create_subscription answered 400" in caplog.text


# --- the stop marker ----------------------------------------------------


def test_fetch_invoices_is_an_explicit_stop_marker_not_a_partial_list(urlopen):
    """Pagination of the invoice list is undocumented (see the function's
    docstring), so the function refuses to return a list that may be partial.
    This test documents the open question; it is replaced, not skipped, once
    the user answers it."""
    with pytest.raises(NotImplementedError):
        razorpay.fetch_invoices("sub_1")
    assert urlopen["request"] is None
