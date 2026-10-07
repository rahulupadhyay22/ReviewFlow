"""W2 of 07-plan-change-replacement: the provider seam and the exceptions.

No network: urlopen is patched. These verify ReviewFlow's handling only (the
request body it builds, the fields it keeps from a 4xx), never Razorpay's
contract: what Razorpay does with `start_at`/`expire_by` or which refusal means
"cannot be updated" is unverified (gates G-0 to G-3).

Not in the spec's "Files to create" list (it has no provider-seam file); the
merged seam tests live in test_razorpay_contract.py, which stays unchanged."""
import io
import json
import logging
import urllib.error

import pytest

from billing import razorpay
from billing.exceptions import (
    BillingProviderRejected,
    NoCreditAcknowledgementRequired,
    ReplacementActivating,
    ReplacementCommitted,
    ReplacementInProgress,
)
from core.api import exception_handler
from core.exceptions import ReviewFlowError


@pytest.fixture(autouse=True)
def creds(settings):
    settings.RAZORPAY_KEY_ID = "rzp_test_keyid"
    settings.RAZORPAY_KEY_SECRET = "key_secret_value"
    settings.RAZORPAY_SUBSCRIPTION_TOTAL_COUNT = 1200


class _Resp:
    def __init__(self, body=b"{}"):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def urlopen(monkeypatch):
    state = {"request": None, "result": _Resp(b'{"id": "sub_New1", "status": "created"}')}

    def fake(req, timeout=None):
        state["request"] = req
        result = state["result"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(razorpay.urllib.request, "urlopen", fake)
    return state


def _http_error(status, body=b""):
    return urllib.error.HTTPError("https://api.razorpay.com/x", status, "msg", {}, io.BytesIO(body))


def _error_body(**error):
    return json.dumps({"error": error}).encode()


def _sent(urlopen):
    return json.loads(urlopen["request"].data)


# --- create_subscription: start_at / expire_by --------------------------------------


def test_a_plain_create_sends_exactly_the_body_it_always_did(urlopen):
    razorpay.create_subscription("plan_X")
    assert _sent(urlopen) == {"plan_id": "plan_X", "total_count": 1200, "customer_notify": True}


def test_start_at_and_expire_by_are_sent_only_when_given(urlopen):
    razorpay.create_subscription("plan_X", start_at=1_800_000_000)
    assert _sent(urlopen) == {
        "plan_id": "plan_X",
        "total_count": 1200,
        "customer_notify": True,
        "start_at": 1_800_000_000,
    }
    razorpay.create_subscription("plan_X", expire_by=1_799_000_000)
    body = _sent(urlopen)
    assert body["expire_by"] == 1_799_000_000 and "start_at" not in body
    razorpay.create_subscription("plan_X", start_at=1_800_000_000, expire_by=1_799_000_000)
    body = _sent(urlopen)
    assert body["start_at"] == 1_800_000_000 and body["expire_by"] == 1_799_000_000
    assert "notes" not in body  # still nothing about the merchant


@pytest.mark.parametrize("bad", [0, -1, 1.5, "1800000000", True, False])
@pytest.mark.parametrize("field", ["start_at", "expire_by"])
def test_a_non_timestamp_is_refused_before_any_request(urlopen, field, bad):
    with pytest.raises(ValueError):
        razorpay.create_subscription("plan_X", **{field: bad})
    assert urlopen["request"] is None


def test_the_new_arguments_are_keyword_only(urlopen):
    with pytest.raises(TypeError):
        razorpay.create_subscription("plan_X", 1_800_000_000)
    assert urlopen["request"] is None


# --- the 4xx path records a bounded status and reason --------------------------------


def test_a_4xx_records_the_status_code_and_a_short_reason(urlopen):
    urlopen["result"] = _http_error(
        400, _error_body(code="BAD_REQUEST_ERROR", reason="input_validation_failed", description="DESC")
    )
    with pytest.raises(BillingProviderRejected) as info:
        razorpay.update_subscription("sub_1", "plan_Y", "now")
    err = info.value
    assert (err.status, err.provider_code, err.reason) == (400, "BAD_REQUEST_ERROR", "input_validation_failed")


@pytest.mark.parametrize("status", [400, 404, 422, 429])
def test_every_4xx_status_is_recorded(urlopen, status):
    urlopen["result"] = _http_error(status, _error_body(code="X"))
    with pytest.raises(BillingProviderRejected) as info:
        razorpay.fetch_subscription("sub_1")
    assert info.value.status == status


@pytest.mark.parametrize(
    "reason, kept",
    [
        ("input_validation_failed", True),
        ("a.b:c-d_E9", True),
        ("x" * 64, True),
        ("x" * 65, False),  # too long
        ("Subscription cannot be updated for UPI", False),  # free text (a description)
        ("with space", False),
        ("semi;colon", False),
        ("", False),
        (123, False),  # not a string
        (None, False),
        (["list"], False),
    ],
)
def test_only_a_short_identifier_is_kept_as_the_reason(urlopen, reason, kept):
    urlopen["result"] = _http_error(400, _error_body(code="BAD_REQUEST_ERROR", reason=reason))
    with pytest.raises(BillingProviderRejected) as info:
        razorpay.fetch_subscription("sub_1")
    assert info.value.reason == (reason if kept else None)


@pytest.mark.parametrize("body", [b"", b"<html>", b"[]", b'{"error": "text"}', b'{"error": {}}'])
def test_an_unusable_error_body_leaves_the_reason_empty_but_keeps_the_status(urlopen, body):
    urlopen["result"] = _http_error(400, body)
    with pytest.raises(BillingProviderRejected) as info:
        razorpay.fetch_subscription("sub_1")
    assert info.value.reason is None and info.value.status == 400


def test_the_status_and_reason_never_appear_in_the_message_or_the_logs(urlopen, caplog):
    marker = "reason_marker_9"
    urlopen["result"] = _http_error(
        400, _error_body(code="BAD_REQUEST_ERROR", reason=marker, description="LEAKY-DESCRIPTION")
    )
    with caplog.at_level(logging.DEBUG, logger="billing"):
        with pytest.raises(BillingProviderRejected) as info:
            razorpay.update_subscription("sub_Leak0001", "plan_Y", "now")
    assert info.value.reason == marker
    for text in (str(info.value), repr(info.value), caplog.text):
        assert marker not in text
        assert "LEAKY-DESCRIPTION" not in text
        assert "sub_Leak0001" not in text


@pytest.mark.parametrize("failure", [_http_error(500), _http_error(503), urllib.error.URLError("dns")])
def test_5xx_and_transport_failures_are_still_unavailable_not_rejected(urlopen, failure):
    from billing.exceptions import BillingProviderUnavailable

    urlopen["result"] = failure
    with pytest.raises(BillingProviderUnavailable):
        razorpay.fetch_subscription("sub_1")


def test_the_known_refusal_set_ships_empty_so_nothing_classifies():
    """G-0: Razorpay's refusal for an unsupported update is not evidenced."""
    assert isinstance(razorpay.UPDATE_UNSUPPORTED_REFUSALS, frozenset)
    assert razorpay.UPDATE_UNSUPPORTED_REFUSALS == frozenset()


def test_the_rejected_error_defaults_and_keyword_only_fields():
    bare = BillingProviderRejected()
    assert (bare.provider_code, bare.status, bare.reason) == (None, None, None)
    with pytest.raises(TypeError):
        BillingProviderRejected("CODE", 400)  # status is keyword-only


# --- the four new exceptions ---------------------------------------------------------


@pytest.mark.parametrize(
    "exc_class, status, code",
    [
        (ReplacementInProgress, 409, "replacement_in_progress"),
        (NoCreditAcknowledgementRequired, 422, "no_credit_acknowledgement_required"),
        (ReplacementActivating, 409, "replacement_activating"),
        (ReplacementCommitted, 409, "replacement_committed"),
    ],
)
def test_the_new_exceptions_map_to_the_documented_status_and_code(exc_class, status, code):
    exc = exc_class()
    assert isinstance(exc, ReviewFlowError)
    assert (exc.http_status, exc.code) == (status, code)
    response = exception_handler(exc, {})
    assert response.status_code == status
    assert response.data["error"]["code"] == code
    assert str(exc)  # a fixed message, no placeholders
