"""POST/GET /api/v1/webhooks/whatsapp (spec 08 Definition of done "Inbound
webhook & opt-out"; Webhook-Specification.md fail-closed rule; Testing-Strategy
Duplicate Webhook Event). Meta's HTTP is not used here: the view only verifies,
parses and enqueues."""
import hashlib
import hmac
import json
import logging

import pytest
from django.apps import apps
from rest_framework.test import APIClient

from core.tenancy import tenant_atomic, tenant_context
from customers.models import Customer
from whatsapp import tasks
from whatsapp.tests.conftest import fixture_bytes, load_fixture
from whatsapp.tests.helpers import APP_SECRET, CUSTOMER_PHONE, SHARED_PHONE_NUMBER_ID, VERIFY_TOKEN

pytestmark = pytest.mark.django_db

URL = "/api/v1/webhooks/whatsapp"


def sign(raw: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def deliver(raw: bytes, signature="auto", client=None):
    client = client or APIClient()
    extra = {}
    sig = sign(raw) if signature == "auto" else signature
    if sig is not None:
        extra["HTTP_X_HUB_SIGNATURE_256"] = sig
    return client.post(URL, data=raw, content_type="application/json", **extra)


def stop_payload(text):
    payload = load_fixture("inbound_text_stop.json")
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = text
    return json.dumps(payload).encode()


@pytest.fixture
def enqueue(monkeypatch):
    """Replaces only the broker call of the fan-out task, so the view can be
    asserted in isolation (zero queries, exactly one enqueue)."""
    from unittest.mock import Mock

    mock = Mock()
    monkeypatch.setattr(tasks.fan_out_inbound_opt_out, "delay", mock)
    return mock


@pytest.fixture(autouse=True)
def _settings(meta_settings):
    return meta_settings


# --- fail closed ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "signature",
    [None, "", "sha256=", "sha256=zz", "deadbeef", sign(b"another body"), sign(b"x", "another-secret")],
    ids=["missing", "empty", "no-digest", "not-hex", "no-prefix", "wrong-body", "wrong-secret"],
)
def test_missing_or_invalid_signature_answers_401_with_zero_queries_and_nothing_enqueued(
    django_assert_num_queries, enqueue, signature
):
    raw = fixture_bytes("inbound_text_stop.json")

    with django_assert_num_queries(0):
        resp = deliver(raw, signature=signature)

    assert resp.status_code == 401
    assert "error" in resp.json()
    enqueue.assert_not_called()


def test_a_tampered_body_answers_401(django_assert_num_queries, enqueue):
    raw = fixture_bytes("inbound_text_stop.json")
    signature = sign(raw)

    with django_assert_num_queries(0):
        resp = deliver(raw.replace(b"STOP", b"GO!!"), signature=signature)

    assert resp.status_code == 401
    enqueue.assert_not_called()


def test_an_unset_app_secret_answers_401_even_with_a_signature_made_with_an_empty_key(
    django_assert_num_queries, enqueue, settings
):
    settings.META_APP_SECRET = ""
    raw = fixture_bytes("inbound_text_stop.json")

    with django_assert_num_queries(0):
        resp = deliver(raw, signature=sign(raw, ""))

    assert resp.status_code == 401
    enqueue.assert_not_called()


# --- what a valid delivery does -----------------------------------------------------------------


def test_a_signed_stop_answers_200_with_zero_queries_and_enqueues_exactly_one_fan_out(
    django_assert_num_queries, enqueue
):
    with django_assert_num_queries(0):
        resp = deliver(fixture_bytes("inbound_text_stop.json"))

    assert resp.status_code == 200
    enqueue.assert_called_once()
    assert enqueue.call_args.args == (SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)


@pytest.mark.parametrize("text", ["stop", "  Stop  ", "UNSUBSCRIBE", "unsubscribe\n"])
def test_keyword_matching_ignores_case_and_surrounding_whitespace(enqueue, text):
    assert deliver(stop_payload(text)).status_code == 200
    enqueue.assert_called_once()


@pytest.mark.parametrize("text", ["STOP please", "don't stop", "Thanks", "", "STOPPED"])
def test_only_a_whole_message_keyword_triggers_an_opt_out(enqueue, text):
    assert deliver(stop_payload(text)).status_code == 200
    enqueue.assert_not_called()


def test_a_non_keyword_text_answers_200_and_enqueues_nothing(django_assert_num_queries, enqueue):
    with django_assert_num_queries(0):
        resp = deliver(fixture_bytes("inbound_text_other.json"))

    assert resp.status_code == 200
    enqueue.assert_not_called()


@pytest.mark.parametrize("fixture", ["status_sent.json", "status_delivered.json", "status_read.json", "status_failed.json"])
def test_status_only_deliveries_are_acknowledged_without_queries_or_enqueues(
    django_assert_num_queries, enqueue, fixture
):
    with django_assert_num_queries(0):
        resp = deliver(fixture_bytes(fixture))

    assert resp.status_code == 200
    enqueue.assert_not_called()


def test_there_is_no_whatsapp_message_model_in_this_phase():
    with pytest.raises(LookupError):
        apps.get_model("whatsapp", "WhatsAppMessage")


def test_a_mixed_stop_and_status_delivery_processes_the_stop(django_assert_num_queries, enqueue):
    with django_assert_num_queries(0):
        resp = deliver(fixture_bytes("inbound_stop_with_status.json"))

    assert resp.status_code == 200
    enqueue.assert_called_once()
    assert enqueue.call_args.args == (SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)


def test_an_unparseable_but_correctly_signed_body_answers_200_and_enqueues_nothing(enqueue):
    assert deliver(b"not json").status_code == 200
    enqueue.assert_not_called()


def test_the_old_split_paths_do_not_exist():
    client = APIClient()
    assert client.post("/api/v1/webhooks/whatsapp/status", data=b"{}", content_type="application/json").status_code == 404
    assert client.post("/api/v1/webhooks/whatsapp/inbound", data=b"{}", content_type="application/json").status_code == 404


def test_the_webhook_does_not_accept_a_session_or_csrf(make_merchant, session_client, enqueue):
    # A logged-in browser session is irrelevant: only the signature authenticates.
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    raw = fixture_bytes("inbound_text_stop.json")

    assert deliver(raw, signature=None, client=client).status_code == 401
    assert deliver(raw, client=APIClient(enforce_csrf_checks=True)).status_code == 200


# --- end to end: duplicate delivery, unknown number, logs -----------------------------------------------


def opted_out(merchant, phone=CUSTOMER_PHONE):
    with tenant_context(merchant.id), tenant_atomic():
        return Customer.objects.values_list("opted_out", "opted_out_at").get(phone=phone)


def test_the_same_signed_stop_posted_five_times_opts_the_customer_out_once(
    make_merchant, make_customer, map_to_shared, django_capture_on_commit_callbacks
):
    owner = make_merchant("A")
    map_to_shared(owner.merchant)
    make_customer(owner.merchant, CUSTOMER_PHONE)
    raw = fixture_bytes("inbound_text_stop.json")

    with django_capture_on_commit_callbacks(execute=True):
        first = deliver(raw)
    flag, first_at = opted_out(owner.merchant)
    statuses = [first.status_code]
    for _ in range(4):
        with django_capture_on_commit_callbacks(execute=True):
            statuses.append(deliver(raw).status_code)

    flag_after, at_after = opted_out(owner.merchant)
    assert statuses == [200] * 5
    assert flag is True and flag_after is True
    assert first_at is not None and at_after == first_at


def test_a_stop_to_an_unknown_phone_number_id_answers_200_and_opts_nobody_out(
    make_merchant, make_customer, map_to_shared, django_capture_on_commit_callbacks
):
    owner = make_merchant("A")
    map_to_shared(owner.merchant)
    make_customer(owner.merchant, CUSTOMER_PHONE)
    payload = load_fixture("inbound_text_stop.json")
    payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "UNKNOWN-NUMBER"

    with django_capture_on_commit_callbacks(execute=True):
        resp = deliver(json.dumps(payload).encode())

    assert resp.status_code == 200
    assert opted_out(owner.merchant) == (False, None)


def test_no_log_record_contains_the_phone_number_or_the_message_text(
    make_merchant, make_customer, map_to_shared, django_capture_on_commit_callbacks, caplog
):
    owner = make_merchant("A")
    map_to_shared(owner.merchant)
    make_customer(owner.merchant, CUSTOMER_PHONE)
    caplog.set_level(logging.DEBUG)

    with django_capture_on_commit_callbacks(execute=True):
        assert deliver(fixture_bytes("inbound_text_stop.json")).status_code == 200
        assert deliver(fixture_bytes("inbound_text_stop.json"), signature="sha256=bad").status_code == 401

    assert opted_out(owner.merchant)[0] is True
    for record in caplog.records:
        message = record.getMessage()
        assert "9999990001" not in message
        assert "STOP" not in message


# --- GET handshake -----------------------------------------------------------------------------------------


def handshake(**params):
    return APIClient().get(URL, params)


def test_handshake_echoes_the_challenge_for_subscribe_and_the_correct_token():
    resp = handshake(**{"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1158201444"})

    assert resp.status_code == 200
    assert resp.content.decode() == "1158201444"


@pytest.mark.parametrize(
    "params",
    [
        {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
        {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1"},
        {"hub.mode": "subscribe", "hub.challenge": "1"},
        {"hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1"},
        {},
    ],
    ids=["wrong-token", "other-mode", "no-token", "no-mode", "nothing"],
)
def test_handshake_answers_403_for_a_wrong_token_another_mode_or_missing_parts(params):
    assert handshake(**params).status_code == 403


@pytest.mark.parametrize("token", ["", "x"])
def test_handshake_answers_403_when_the_verify_token_is_unset(settings, token):
    settings.META_WEBHOOK_VERIFY_TOKEN = ""
    resp = handshake(**{"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "1"})
    assert resp.status_code == 403
    assert resp.content.decode() != "1"
