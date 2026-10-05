"""Razorpay billing webhook receiver: gap tests added by the /test-feature gate
(W6). They extend billing/tests/test_webhook.py (developer tests, unchanged):
receive-order precedence, pre-tenant posture, enqueue arguments, log hygiene on
the success path, and fail-closed entitlement for non-qualifying invoices.

Razorpay is the FakeProvider from conftest (never the network). The D1 invoice
fetch is deliberately not exercised here."""
import json
import logging

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from billing import tasks
from billing.models import BillingEvent, Subscription
from billing.services import event_id_failure
from billing.tests.webhook_helpers import (
    END,
    REF,
    SECRET,
    START,
    URL,
    deliver,
    event,
    no_billing_rows,
    rows,
    sign,
)
from core.tenancy import tenant_atomic, tenant_context

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("webhook_secret")]

INVALID_SIGNATURE = {"error": {"code": "invalid_signature", "message": "Invalid webhook signature."}}


# === receive-order precedence: 401 always beats 400 ===============================


def test_a_bad_signature_on_a_non_json_body_is_401_not_400(webhook_setup):
    resp = deliver(b"not json", raw=True, signature="0" * 64)
    assert resp.status_code == 401 and resp.json() == INVALID_SIGNATURE
    no_billing_rows(webhook_setup.a.merchant)


@pytest.mark.parametrize("event_id", [None, "", "e" * 65, "evt 1", "evt_é"])
def test_an_invalid_event_id_on_a_non_json_body_is_401_not_400(webhook_setup, event_id):
    with CaptureQueriesContext(connection) as ctx:
        resp = deliver(b"not json", raw=True, event_id=event_id)
    assert resp.status_code == 401 and resp.json() == INVALID_SIGNATURE
    assert ctx.captured_queries == []


def test_an_invalid_event_id_beats_an_over_long_event_type(webhook_setup):
    resp = deliver(event("e" * 65), event_id="evt 1")
    assert resp.status_code == 401 and resp.json() == INVALID_SIGNATURE


def test_the_400_error_shape_is_the_documented_one(webhook_setup):
    resp = deliver(b"not json", raw=True)
    assert resp.status_code == 400
    assert set(resp.json()) == {"error"} and resp.json()["error"]["code"] == "invalid_payload"
    assert isinstance(resp.json()["error"]["message"], str)


# === the event-id validator, directly ==============================================


@pytest.mark.parametrize("value", [True, 1.5, [], {}, ("evt",), bytearray(b"evt")])
def test_the_validator_rejects_every_non_string_as_not_string(value):
    assert event_id_failure(value) == "not_string"


@pytest.mark.parametrize(
    "value, category",
    [
        ("\n", "invalid_characters"),
        (" ", "invalid_characters"),
        ("\x7f", "invalid_characters"),
        ("\x80", "invalid_characters"),
        ("a\x00b", "invalid_characters"),
        ("日本語", "invalid_characters"),
        ("!", None),  # 0x21, lowest allowed
        ("~", None),  # 0x7E, highest allowed
        ("A" * 64, None),
        ("A" * 65, "too_long"),
        ("é" * 65, "too_long"),  # length is checked before characters
    ],
)
def test_the_validator_boundaries_of_the_printable_ascii_range(value, category):
    assert event_id_failure(value) == category


# === pre-tenant posture: signature is the only authentication ======================


def test_the_url_needs_no_session_csrf_token_or_api_key(webhook_setup, django_capture_on_commit_callbacks):
    client = APIClient(enforce_csrf_checks=True)  # no cookie, no CSRF token
    with django_capture_on_commit_callbacks(execute=False):
        resp = deliver(event(), client=client)
    assert resp.status_code == 200
    assert len(rows(webhook_setup.a.merchant)["events"]) == 1


def test_a_garbage_bearer_header_neither_helps_nor_hurts(webhook_setup, django_capture_on_commit_callbacks):
    body = json.dumps(event()).encode()
    with django_capture_on_commit_callbacks(execute=False):
        ok = APIClient().post(
            URL,
            data=body,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE=sign(body),
            HTTP_X_RAZORPAY_EVENT_ID="evt_bearer",
            HTTP_AUTHORIZATION="Bearer rf_live_garbage",
        )
    assert ok.status_code == 200
    bad = APIClient().post(
        URL,
        data=body,
        content_type="application/json",
        HTTP_X_RAZORPAY_SIGNATURE="0" * 64,
        HTTP_X_RAZORPAY_EVENT_ID="evt_bearer2",
        HTTP_AUTHORIZATION="Bearer rf_live_garbage",
    )
    assert bad.status_code == 401 and bad.json() == INVALID_SIGNATURE


# === tenancy ==========================================================================


def test_a_late_event_for_a_replaced_ref_is_200_and_stores_nothing(webhook_setup):
    with tenant_context(webhook_setup.a.merchant.id), tenant_atomic():
        Subscription.objects.filter(pk=webhook_setup.sub_a.pk).update(payment_provider_ref="sub_Replacement1")
    resp = deliver(event(ref=REF))  # the old ref no longer resolves to any row
    assert resp.status_code == 200 and resp.json() == {}
    no_billing_rows(webhook_setup.a.merchant, webhook_setup.b.merchant)


def test_a_64_character_ref_is_well_formed_so_the_lookup_runs(webhook_setup):
    with CaptureQueriesContext(connection) as ctx:
        resp = deliver(event(ref="s" * 64))
    assert resp.status_code == 200 and resp.json() == {}
    assert ctx.captured_queries  # boundary: 64 is valid, unlike the 65 that skips the lookup
    no_billing_rows(webhook_setup.a.merchant, webhook_setup.b.merchant)


def test_the_stored_event_carries_identifiers_only(webhook_setup, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=False):
        deliver(event(notes={"marker": "PAYLOAD-MARKER"}), event_id="evt_ident")
    with tenant_context(webhook_setup.a.merchant.id), tenant_atomic():
        stored = BillingEvent.objects.get()
        assert (stored.provider, stored.provider_event_id, stored.provider_ref) == ("razorpay", "evt_ident", REF)
        assert stored.event_type == "subscription.charged"
        assert stored.merchant_id == webhook_setup.a.merchant.id
        assert "PAYLOAD-MARKER" not in repr(list(BillingEvent.objects.values()))


# === enqueue ===============================================================================


def test_the_sync_is_enqueued_once_for_the_resolved_merchant_only_after_commit(webhook_setup, monkeypatch, django_capture_on_commit_callbacks):
    calls = []
    monkeypatch.setattr(tasks.sync_subscription, "delay", lambda *a, **k: calls.append((a, k)))
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        assert deliver(event(notes={"merchant_id": str(webhook_setup.b.merchant.id)})).status_code == 200
    assert calls == []  # nothing enqueued before the commit
    for callback in callbacks:
        callback()
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert (list(args) + list(kwargs.values())) == [webhook_setup.a.merchant.id]


def test_a_rejected_request_enqueues_nothing(webhook_setup, monkeypatch, django_capture_on_commit_callbacks):
    calls = []
    monkeypatch.setattr(tasks.sync_subscription, "delay", lambda *a, **k: calls.append(a))
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        deliver(event(), signature="0" * 64)
        deliver(event(), event_id="evt 1")
        deliver(b"nope", raw=True)
        deliver(event(ref="bad ref"))
    assert callbacks == [] and calls == []


# === log hygiene on the accepted path ========================================================


def test_an_accepted_delivery_logs_no_body_signature_secret_or_notes(webhook_setup, caplog, django_capture_on_commit_callbacks):
    payload = event(notes={"marker": "BODY-MARKER"})
    body = json.dumps(payload).encode()
    with caplog.at_level(logging.DEBUG), django_capture_on_commit_callbacks(execute=True):
        assert deliver(body, raw=True).status_code == 200
        assert deliver(body, raw=True).status_code == 200  # the duplicate path too
    text = caplog.text
    for forbidden in ("BODY-MARKER", sign(body), SECRET, "pay_TestPay0000001"):
        assert forbidden not in text


# === fail closed: only a qualifying paid invoice grants entitlement ==============================


def _activate(webhook_setup, callbacks_ctx):
    with callbacks_ctx(execute=True):
        assert deliver(event()).status_code == 200
    return rows(webhook_setup.a.merchant)


@pytest.mark.parametrize(
    "override",
    [
        {"billing_start": START + 10_000_000, "billing_end": END + 10_000_000},  # a different period
        {"billing_end": START},  # window ends at current_start (end is exclusive)
        {"subscription_id": "sub_SomeoneElse"},
        {"status": "partially_paid"},
        {"amount_due": 1},
        {"payment_id": ""},
    ],
)
def test_provider_active_with_a_non_qualifying_invoice_grants_nothing(
    webhook_setup, override, django_capture_on_commit_callbacks
):
    webhook_setup.provider.invoices = [{**webhook_setup.provider.invoices[0], **override}]
    r = _activate(webhook_setup, django_capture_on_commit_callbacks)
    assert r["status"] == "INCOMPLETE" and r["usage"] == 0 and r["payments"] == []
    assert r["audits"] == [] and r["events"][0][1] is None


def test_a_charged_payload_with_active_status_but_no_invoices_grants_nothing(webhook_setup, django_capture_on_commit_callbacks):
    webhook_setup.provider.invoices = []
    r = _activate(webhook_setup, django_capture_on_commit_callbacks)
    assert r["status"] == "INCOMPLETE" and r["usage"] == 0 and r["payments"] == []


def test_an_invoice_fetch_failure_records_no_payment_and_leaves_the_event_unprocessed(
    webhook_setup, django_capture_on_commit_callbacks
):
    from billing.exceptions import BillingProviderUnavailable

    webhook_setup.provider.invoices_error = BillingProviderUnavailable()
    r = _activate(webhook_setup, django_capture_on_commit_callbacks)
    assert r["status"] == "INCOMPLETE" and r["payments"] == [] and r["usage"] == 0
    assert r["events"][0][1] is None


def test_a_qualifying_paid_invoice_marks_the_event_processed_and_grants_entitlement(
    webhook_setup, django_capture_on_commit_callbacks
):
    r = _activate(webhook_setup, django_capture_on_commit_callbacks)
    assert r["status"] == "ACTIVE" and r["events"][0][1] is not None
    assert r["payments"] == ["pay_TestPay0000001"] and r["usage"] == 1


# === throttle ===========================================================================================


def test_the_throttle_applies_to_valid_requests_and_leaves_the_429_shape_retryable(webhook_setup, monkeypatch):
    from integrations.throttling import WebhookIpRateThrottle

    monkeypatch.setattr(WebhookIpRateThrottle, "THROTTLE_RATES", {"webhook_ip": "1/min"})
    assert deliver(event(), event_id="evt_t1").status_code == 200
    with CaptureQueriesContext(connection) as ctx:
        resp = deliver(event(), event_id="evt_t2")
    assert resp.status_code == 429 and int(resp["Retry-After"]) > 0
    assert ctx.captured_queries == []  # throttled before anything is read
    assert [e[0] for e in rows(webhook_setup.a.merchant)["events"]] == ["evt_t1"]
