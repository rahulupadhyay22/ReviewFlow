"""Fixture-based contract tests for MetaCloudProvider (spec 08 Definition of
done "Provider contract"). Payload fixtures are anonymized Meta samples, see
fixtures/README.md for which are documented-field-name reconstructions.
Only the HTTP call is mocked (`meta_http`)."""
import hashlib
import hmac
import logging
import urllib.error
import uuid
from datetime import UTC, datetime

import pytest

from whatsapp.models import MessageTemplate, WhatsAppAccount
from whatsapp.providers import get_provider, get_provider_by_name, meta_cloud
from whatsapp.providers.base import (
    InboundMessage,
    ProviderPermanentError,
    ProviderSendResult,
    ProviderTransientError,
    StatusUpdate,
    TemplateStatus,
)
from whatsapp.providers.meta_cloud import MetaCloudProvider
from whatsapp.tests.conftest import fixture_bytes, http_error, load_fixture
from whatsapp.tests.helpers import ACCESS_TOKEN, APP_SECRET, SHARED_PHONE_NUMBER_ID, SHARED_WABA_ID

pytestmark = pytest.mark.django_db

provider = MetaCloudProvider()
PHONE_DIGITS = "919999990001"
BODY = "Hi {{customer_name}}, thanks for visiting {{business_name}}! Review: {{review_link}}"


def account():
    return WhatsAppAccount(
        sender_type="SHARED_POOL",
        provider="meta_cloud",
        phone_number_id=SHARED_PHONE_NUMBER_ID,
        business_account_id=SHARED_WABA_ID,
        status="ACTIVE",
    )


def template(body=BODY):
    return MessageTemplate(name="Review ask", language="en", body=body)


def sign(raw: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


# --- parse_inbound_webhook ------------------------------------------------------------


def test_get_provider_dispatches_meta_cloud_accounts_to_the_meta_provider():
    assert isinstance(get_provider(account()), MetaCloudProvider)


def test_parse_inbound_stop_text():
    [message] = provider.parse_inbound_webhook(load_fixture("inbound_text_stop.json"))

    assert (message.phone_number_id, message.from_phone, message.text) == (
        SHARED_PHONE_NUMBER_ID,
        PHONE_DIGITS,
        "STOP",
    )
    assert isinstance(message, InboundMessage)


def test_parse_inbound_other_text():
    [message] = provider.parse_inbound_webhook(load_fixture("inbound_text_other.json"))

    assert (message.phone_number_id, message.from_phone, message.text) == (
        SHARED_PHONE_NUMBER_ID,
        PHONE_DIGITS,
        "Thanks, see you soon",
    )


def test_parse_inbound_reply_parses_the_message_and_the_contract_has_no_reply_context_field():
    # Spec 08 interface (amended 2026-10-07): InboundMessage(phone_number_id,
    # from_phone, text) -- no reply-context field. Meta's reply context.id is
    # unverified (M-4) and unused by OD-1 (b); Phase 11 may add it after
    # verification. The fixture is reconstructed (M-4: context.id UNVERIFIED).
    [message] = provider.parse_inbound_webhook(load_fixture("inbound_text_reply_with_context.json"))

    assert message.text == "STOP"
    assert not hasattr(message, "context_message_id")
    assert set(message.__dataclass_fields__) == {"phone_number_id", "from_phone", "text"}


def test_parse_inbound_ignores_status_only_deliveries():
    assert provider.parse_inbound_webhook(load_fixture("status_delivered.json")) == []


def test_parse_inbound_tolerates_malformed_payloads():
    for payload in ({}, {"entry": None}, {"entry": [{}]}, {"entry": [{"changes": [{"value": "x"}]}]}):
        assert provider.parse_inbound_webhook(payload) == []


# --- parse_status_webhook ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "wamid", "status", "timestamp"),
    [
        ("status_delivered.json", "wamid.TESTOUT0001", "DELIVERED", 1790000100),
        ("status_read.json", "wamid.TESTOUT0001", "READ", 1790000200),
        ("status_failed.json", "wamid.TESTOUT0002", "FAILED", 1790000300),
    ],
)
def test_parse_status_fixtures_to_status_updates(fixture, wamid, status, timestamp):
    [update] = provider.parse_status_webhook(load_fixture(fixture))

    assert isinstance(update, StatusUpdate)
    assert update.provider_message_id == wamid
    assert update.status == status
    assert update.occurred_at == datetime.fromtimestamp(timestamp, tz=UTC)


def test_parse_status_ignores_inbound_message_deliveries():
    assert provider.parse_status_webhook(load_fixture("inbound_text_stop.json")) == []


def test_parse_status_failed_fixture_keeps_its_error_code_and_valid_output_is_unchanged():
    [update] = provider.parse_status_webhook(load_fixture("status_failed.json"))

    assert update == StatusUpdate("wamid.TESTOUT0002", "FAILED", datetime.fromtimestamp(1790000300, tz=UTC), 131026)


# --- parse_status_webhook: malformed signed bodies are skipped, never raised --------------

_MISSING = object()  # also used by the inbound malformed-payload tests below


def _statuses(statuses):
    payload = load_fixture("status_delivered.json")
    payload["entry"][0]["changes"][0]["value"]["statuses"] = statuses
    return payload


def _status(**overrides):
    status = {"id": "wamid.TESTOUT0009", "status": "delivered", "timestamp": "1790000100", "recipient_id": "x"}
    status.update(overrides)
    return {key: val for key, val in status.items() if val is not _MISSING}


@pytest.mark.parametrize("statuses", [7, "delivered", {"0": {}}, None, True], ids=["int", "string", "object", "null", "bool"])
def test_parse_status_a_non_list_statuses_field_yields_nothing(statuses):
    assert provider.parse_status_webhook(_statuses(statuses)) == []


@pytest.mark.parametrize("item", ["x", 5, None, ["delivered"], True], ids=["string", "int", "null", "list", "bool"])
def test_parse_status_a_non_object_item_is_skipped(item):
    assert provider.parse_status_webhook(_statuses([item])) == []


@pytest.mark.parametrize(
    "bad_id", [_MISSING, None, "", {"id": "x"}, ["x"], True, False], ids=["missing", "null", "empty", "object", "list", "true", "false"]
)
def test_parse_status_an_unusable_id_is_skipped(bad_id):
    assert provider.parse_status_webhook(_statuses([_status(id=bad_id)])) == []


@pytest.mark.parametrize(
    "name", [_MISSING, None, "", "unknown-state", ["delivered"], {"s": "delivered"}, 5],
    ids=["missing", "null", "empty", "unknown", "list", "object", "int"],
)
def test_parse_status_an_unusable_status_name_is_skipped(name):
    assert provider.parse_status_webhook(_statuses([_status(status=name)])) == []


@pytest.mark.parametrize("errors", [{"a": 1}, "x", 7, None, [], [None], ["x"], [[]], [{"code": "131026"}], [{"code": True}], [{"code": None}]])
def test_parse_status_malformed_errors_give_no_code_and_never_raise(errors):
    [update] = provider.parse_status_webhook(_statuses([_status(status="failed", errors=errors)]))

    assert update.status == "FAILED"
    assert update.error_code is None


@pytest.mark.parametrize("stamp", [_MISSING, None, "", "x", {"t": 1}, ["1"], 10**400, float("nan")])
def test_parse_status_a_bad_timestamp_gives_no_time_but_keeps_the_update(stamp):
    [update] = provider.parse_status_webhook(_statuses([_status(timestamp=stamp)]))

    assert update.occurred_at is None
    assert update.status == "DELIVERED"


def test_parse_status_accepts_an_integer_id_and_numeric_timestamps():
    [update] = provider.parse_status_webhook(_statuses([_status(id=1234567890, timestamp=1790000100)]))

    assert update.provider_message_id == "1234567890"
    assert update.occurred_at == datetime.fromtimestamp(1790000100, tz=UTC)


@pytest.mark.parametrize(
    "payload",
    [{"entry": "x"}, {"entry": [1, None]}, {"entry": [{"changes": "x"}]}, {"entry": [{"changes": [None, 3]}]},
     {"entry": [{"changes": [{"value": ["x"]}]}]}],
    ids=["entry-string", "entry-items", "changes-string", "change-items", "value-list"],
)
def test_parse_status_tolerates_wrongly_typed_envelope_levels(payload):
    assert provider.parse_status_webhook(payload) == []


def test_parse_status_keeps_the_valid_updates_in_a_mixed_delivery():
    statuses = [
        "garbage",
        _status(id=None),
        _status(status="unknown-state"),
        _status(id="wamid.TESTOUT0010", status="failed", errors={"a": 1}),
        _status(id="wamid.TESTOUT0011", status="read", timestamp="x"),
        _status(id="wamid.TESTOUT0012", status="failed", errors=[{"code": 131026}]),
    ]
    updates = provider.parse_status_webhook(_statuses(statuses))

    assert [(u.provider_message_id, u.status, u.error_code) for u in updates] == [
        ("wamid.TESTOUT0010", "FAILED", None),
        ("wamid.TESTOUT0011", "READ", None),
        ("wamid.TESTOUT0012", "FAILED", 131026),
    ]


def test_a_mixed_delivery_parses_to_one_message_and_one_status():
    payload = load_fixture("inbound_stop_with_status.json")

    assert [m.text for m in provider.parse_inbound_webhook(payload)] == ["STOP"]
    assert [u.status for u in provider.parse_status_webhook(payload)] == ["DELIVERED"]


# --- fetch_template_status / fetch_quality_rating ---------------------------------------


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("template_status_approved.json", "APPROVED"),
        ("template_status_rejected.json", "REJECTED"),
        ("template_status_pending.json", "PENDING"),
    ],
)
def test_fetch_template_status_maps_the_fixture(meta_http, fixture, expected):
    meta_http.on("900000000000001", load_fixture(fixture))

    result = provider.fetch_template_status(account(), "900000000000001")

    assert isinstance(result, TemplateStatus)
    assert result.status == expected
    request = meta_http.requests[0]
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == f"Bearer {ACCESS_TOKEN}"


@pytest.mark.parametrize("meta_status", ["DISABLED", "DELETED", "PENDING_DELETION", "ARCHIVED"])
def test_fetch_template_status_treats_terminal_non_approved_states_as_rejected(meta_http, meta_status):
    meta_http.on("900000000000001", {"id": "900000000000001", "status": meta_status})
    assert provider.fetch_template_status(account(), "900000000000001").status == "REJECTED"


@pytest.mark.parametrize("meta_status", ["IN_APPEAL", "PAUSED", "LIMIT_EXCEEDED", "IN_REVIEW"])
def test_fetch_template_status_treats_other_states_as_pending(meta_http, meta_status):
    meta_http.on("900000000000001", {"id": "900000000000001", "status": meta_status})
    assert provider.fetch_template_status(account(), "900000000000001").status == "PENDING"


def test_fetch_quality_rating_returns_metas_string_for_the_accounts_number(meta_http):
    meta_http.on("/phone_numbers", load_fixture("quality_rating.json"))

    assert provider.fetch_quality_rating(account()) == "GREEN"
    assert SHARED_WABA_ID in meta_http.urls()[0]


def test_fetch_quality_rating_returns_the_rating_of_the_matching_phone_number_only(meta_http):
    meta_http.on(
        "/phone_numbers",
        {
            "data": [
                {"id": "OTHER-NUMBER", "quality_rating": "RED"},
                {"id": SHARED_PHONE_NUMBER_ID, "quality_rating": "YELLOW"},
            ]
        },
    )
    assert provider.fetch_quality_rating(account()) == "YELLOW"


# --- submit_template ---------------------------------------------------------------------


def test_submit_template_posts_to_the_waba_and_returns_the_provider_template_id(meta_http):
    meta_http.on("/message_templates", load_fixture("template_create_response.json"))
    tmpl = template()

    template_id = provider.submit_template(account(), tmpl)

    assert template_id == "900000000000001"
    request = meta_http.requests[0]
    assert request.get_method() == "POST"
    assert f"/{SHARED_WABA_ID}/message_templates" in request.full_url
    sent = meta_http.body()
    assert sent["language"] == "en"
    assert sent["category"] in ("UTILITY", "MARKETING")
    assert sent["name"] != tmpl.name  # never the merchant's free-text label
    assert sent["name"] == sent["name"].lower() and " " not in sent["name"]
    assert any(c.get("type", "").upper() == "BODY" and c.get("text") == BODY for c in sent["components"])


def test_every_meta_call_sets_a_timeout_and_the_platform_bearer_token(meta_http):
    meta_http.on("/message_templates", load_fixture("template_create_response.json"))
    provider.submit_template(account(), template())

    assert meta_http.timeouts[0] is not None and meta_http.timeouts[0] > 0
    assert meta_http.requests[0].get_header("Authorization") == f"Bearer {ACCESS_TOKEN}"


# --- send -----------------------------------------------------------------------------------


def test_send_builds_the_request_from_the_named_variables_and_parses_the_response(meta_http):
    meta_http.on("/messages", load_fixture("send_response.json"))
    variables = {
        "customer_name": "Asha",
        "business_name": "Seed Cafe",
        "location_name": "Indiranagar",
        "review_link": "https://example.com/review/abc",
    }
    tmpl = template()

    result = provider.send(account(), "+919999990001", tmpl, variables)

    assert result == ProviderSendResult(provider_message_id="wamid.TESTOUT0001")
    request = meta_http.requests[0]
    assert f"/{SHARED_PHONE_NUMBER_ID}/messages" in request.full_url
    body = meta_http.body()
    assert body["messaging_product"] == "whatsapp"
    assert body["to"].lstrip("+") == PHONE_DIGITS
    assert body["type"] == "template"
    assert body["template"]["language"]["code"] == "en"
    texts = [
        p["text"]
        for component in body["template"]["components"]
        if component["type"] == "body"
        for p in component["parameters"]
    ]
    # Placeholders in the order they appear in the body; unused variables are not sent.
    assert texts == ["Asha", "Seed Cafe", "https://example.com/review/abc"]


# --- verify_signature --------------------------------------------------------------------------


@pytest.fixture
def raw_body(meta_settings):
    return fixture_bytes("inbound_text_stop.json")


def test_verify_signature_accepts_a_correct_signature_over_the_raw_fixture_body(raw_body):
    assert provider.verify_signature(raw_body, sign(raw_body)) is True


def test_verify_signature_rejects_a_tampered_body(raw_body):
    assert provider.verify_signature(raw_body + b" ", sign(raw_body)) is False


def test_verify_signature_rejects_a_wrong_secret(raw_body):
    assert provider.verify_signature(raw_body, sign(raw_body, "another-secret")) is False


@pytest.mark.parametrize("header", [None, "", "sha256=", "sha256=not-hex", "deadbeef", "md5=abc"])
def test_verify_signature_rejects_missing_and_malformed_headers(raw_body, header):
    assert provider.verify_signature(raw_body, header) is False


def test_verify_signature_fails_closed_when_the_app_secret_is_unset(raw_body, settings):
    settings.META_APP_SECRET = ""
    assert provider.verify_signature(raw_body, sign(raw_body, "")) is False


# --- error classification ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        http_error(500),
        http_error(503),
        http_error(429),
        urllib.error.URLError("connection refused"),
        TimeoutError(),
    ],
    ids=["500", "503", "429", "network", "timeout"],
)
def test_transient_failures_raise_provider_transient_error(meta_http, failure):
    meta_http.on("/messages", failure)

    with pytest.raises(ProviderTransientError):
        provider.send(account(), "+919999990001", template(), {"customer_name": "A", "business_name": "B", "review_link": "L"})


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_a_4xx_validation_failure_raises_provider_permanent_error(meta_http, code):
    meta_http.on("/messages", http_error(code))

    with pytest.raises(ProviderPermanentError):
        provider.send(account(), "+919999990001", template(), {"customer_name": "A", "business_name": "B", "review_link": "L"})


@pytest.mark.parametrize("code", [400, 500])
def test_exception_messages_never_contain_the_phone_token_or_response_body(meta_http, code):
    meta_http.on("/messages", http_error(code))
    variables = {"customer_name": "SecretName", "business_name": "B", "review_link": "L"}

    with pytest.raises((ProviderPermanentError, ProviderTransientError)) as exc_info:
        provider.send(account(), "+919999990001", template(), variables)

    text = str(exc_info.value) + repr(exc_info.value)
    for secret in (PHONE_DIGITS, ACCESS_TOKEN, "SECRET-BODY-MARKER", "SecretName"):
        assert secret not in text


def test_template_submission_failures_are_classified_too(meta_http):
    meta_http.on("/message_templates", http_error(400))
    with pytest.raises(ProviderPermanentError):
        provider.submit_template(account(), template())

    meta_http.routes.clear()
    meta_http.on("/message_templates", http_error(500))
    with pytest.raises(ProviderTransientError):
        provider.submit_template(account(), template())


# --- get_provider_by_name ---------------------------------------------------------------


def test_get_provider_by_name_returns_the_meta_provider_without_an_account():
    assert isinstance(get_provider_by_name("meta_cloud"), MetaCloudProvider)
    with pytest.raises(KeyError):
        get_provider_by_name("not-a-provider")


# --- find_template_id (spec 08 code review; gate M-5f) -----------------------------------------
#
# Three outcomes that must never be confused: the id (found), None (the list was
# scanned to its end and the template is not there) and ProviderTransientError
# (inconclusive: the caller must not conclude "not submitted").

LOOKUP_ID = "TEST-TPL-MATCH"


def lookup_template(language="en"):
    # find_template_id reads only the pk (the derived name) and the language.
    return MessageTemplate(pk=uuid.UUID(int=1), name="Review ask", language=language, body=BODY)


def test_find_template_id_matches_on_the_first_page(meta_http):
    meta_http.queue.append(load_fixture("template_list_page2.json"))  # a single, last page

    assert provider.find_template_id(account(), lookup_template()) == LOOKUP_ID

    assert len(meta_http.requests) == 1
    assert f"/{SHARED_WABA_ID}/message_templates?" in meta_http.urls()[0]
    assert meta_http.requests[0].get_method() == "GET"


def test_find_template_id_follows_the_cursor_to_a_match_on_the_second_page(meta_http):
    meta_http.queue.extend([load_fixture("template_list_page1.json"), load_fixture("template_list_page2.json")])

    assert provider.find_template_id(account(), lookup_template()) == LOOKUP_ID

    assert len(meta_http.requests) == 2
    assert "after=" not in meta_http.urls()[0]
    assert "after=CURSOR-PAGE-2" in meta_http.urls()[1]


def test_find_template_id_does_not_match_the_same_name_in_another_language(meta_http):
    last_page = load_fixture("template_list_page1.json")  # holds the name, but only in "hi"
    last_page["paging"].pop("next")  # ... and is the end of the list
    meta_http.queue.append(last_page)

    assert provider.find_template_id(account(), lookup_template(language="en")) is None


def test_find_template_id_does_not_match_a_different_name(meta_http):
    meta_http.queue.append({"data": [{"id": "X", "name": "rf_" + uuid.UUID(int=9).hex, "language": "en"}]})

    assert provider.find_template_id(account(), lookup_template()) is None


@pytest.mark.parametrize("page", [{"data": []}, {}, {"data": [], "paging": {"cursors": {"after": "C"}}}])
def test_find_template_id_returns_none_only_for_a_complete_list_with_no_match(meta_http, page):
    meta_http.queue.append(page)

    assert provider.find_template_id(account(), lookup_template()) is None
    assert len(meta_http.requests) == 1  # no `next`: the scan is complete


def test_find_template_id_is_inconclusive_at_the_page_cap_not_a_not_found(meta_http, monkeypatch, caplog):
    monkeypatch.setattr(meta_cloud, "_FIND_MAX_PAGES", 2)
    endless = load_fixture("template_list_page1.json")  # no match, always a next page
    meta_http.queue.extend([endless, endless, endless])

    with caplog.at_level(logging.WARNING, logger="whatsapp.providers.meta_cloud"):
        with pytest.raises(ProviderTransientError):
            provider.find_template_id(account(), lookup_template())

    assert len(meta_http.requests) == 2  # it stopped at the cap
    assert any("page cap" in record.getMessage() for record in caplog.records)


def test_find_template_id_with_a_matching_item_without_an_id_is_inconclusive(meta_http):
    page = load_fixture("template_list_page2.json")
    del page["data"][0]["id"]
    meta_http.queue.append(page)

    with pytest.raises(ProviderTransientError):
        provider.find_template_id(account(), lookup_template())


def test_find_template_id_with_a_next_page_but_no_cursor_is_inconclusive(meta_http):
    page = load_fixture("template_list_page1.json")
    del page["paging"]["cursors"]
    meta_http.queue.append(page)

    with pytest.raises(ProviderTransientError):
        provider.find_template_id(account(), lookup_template())


@pytest.mark.parametrize(
    "status, error", [(503, ProviderTransientError), (429, ProviderTransientError), (400, ProviderPermanentError)]
)
def test_find_template_id_lets_request_failures_propagate(meta_http, status, error):
    meta_http.queue.append(http_error(status))

    with pytest.raises(error):
        provider.find_template_id(account(), lookup_template())


# --- provider ids are one URL path segment (security review) ---------------------------------------

HOSTILE = "../evil?x=1#f"
HOSTILE_SEGMENT = "..%2Fevil%3Fx%3D1%23f"


def assert_one_safe_segment(url, tail):
    assert f"/{HOSTILE_SEGMENT}/{tail}" in url
    assert "evil?x=1" not in url and "#f" not in url


def test_business_account_id_is_quoted_in_every_graph_path(meta_http):
    hostile_waba = account()
    hostile_waba.business_account_id = HOSTILE
    meta_http.queue.extend(
        [load_fixture("template_create_response.json"), {"data": []}, load_fixture("quality_rating.json")]
    )

    provider.submit_template(hostile_waba, template())
    provider.find_template_id(hostile_waba, lookup_template())
    provider.fetch_quality_rating(hostile_waba)

    urls = meta_http.urls()
    assert_one_safe_segment(urls[0], "message_templates")
    assert_one_safe_segment(urls[1], "message_templates?")
    assert_one_safe_segment(urls[2], "phone_numbers?")


def test_phone_number_id_is_quoted_in_the_send_path(meta_http):
    hostile_sender = account()
    hostile_sender.phone_number_id = HOSTILE
    meta_http.queue.append(load_fixture("send_response.json"))

    provider.send(
        hostile_sender,
        "+919999990001",
        template(),
        {"customer_name": "A", "business_name": "B", "review_link": "https://example.com/r"},
    )

    assert_one_safe_segment(meta_http.urls()[0], "messages")


# --- malformed inbound payloads: skipped item by item, never an exception -----------------


def _inbound(messages=None, metadata=None):
    payload = load_fixture("inbound_text_stop.json")
    value = payload["entry"][0]["changes"][0]["value"]
    if messages is not None:
        value["messages"] = messages
    if metadata is not None:
        value["metadata"] = metadata
    return payload


def _msg(**overrides):
    message = {"from": PHONE_DIGITS, "id": "wamid.TEST", "timestamp": "1", "type": "text", "text": {"body": "STOP"}}
    message.update(overrides)
    return {key: val for key, val in message.items() if val is not _MISSING}


@pytest.mark.parametrize("text_value", ["STOP", ["STOP"], 7, None, True], ids=["string", "list", "int", "null", "bool"])
def test_parse_inbound_a_non_object_text_yields_no_text(text_value):
    [message] = provider.parse_inbound_webhook(_inbound([_msg(text=text_value)]))
    assert message.text is None


@pytest.mark.parametrize(
    "body", [123, None, ["STOP"], {"x": "STOP"}, True], ids=["int", "null", "list", "object", "bool"]
)
def test_parse_inbound_a_non_string_body_yields_no_text(body):
    [message] = provider.parse_inbound_webhook(_inbound([_msg(text={"body": body})]))
    assert message.text is None


def test_parse_inbound_a_text_object_without_body_yields_no_text():
    [message] = provider.parse_inbound_webhook(_inbound([_msg(text={})]))
    assert message.text is None


@pytest.mark.parametrize("kind", ["button", "interactive", "image", "reaction", "brand-new-type", None, ["text"]])
def test_parse_inbound_a_non_text_type_never_yields_text_even_with_a_body(kind):
    [message] = provider.parse_inbound_webhook(_inbound([_msg(type=kind)]))
    assert message.text is None


@pytest.mark.parametrize(
    "sender",
    [_MISSING, None, "", {"wa": PHONE_DIGITS}, [PHONE_DIGITS], True, False],
    ids=["missing", "null", "empty", "object", "list", "true", "false"],
)
def test_parse_inbound_a_message_without_a_usable_sender_is_skipped(sender):
    assert provider.parse_inbound_webhook(_inbound([_msg(**{"from": sender})])) == []


@pytest.mark.parametrize(
    "metadata",
    [{}, {"phone_number_id": None}, {"phone_number_id": {"id": "1"}}, {"phone_number_id": True}, "x", ["x"]],
    ids=["empty", "null-id", "object-id", "bool-id", "string", "list"],
)
def test_parse_inbound_without_a_usable_phone_number_id_yields_nothing(metadata):
    assert provider.parse_inbound_webhook(_inbound(metadata=metadata)) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"entry": "x"},
        {"entry": ["x", 1, None]},
        {"entry": [{"changes": "x"}]},
        {"entry": [{"changes": ["x", None, 3]}]},
        {"entry": [{"changes": [{"value": ["x"]}]}]},
    ],
    ids=["entry-string", "entry-items", "changes-string", "change-items", "value-list"],
)
def test_parse_inbound_tolerates_wrongly_typed_envelope_levels(payload):
    assert provider.parse_inbound_webhook(payload) == []


@pytest.mark.parametrize("messages", ["STOP", {"0": "STOP"}, 5], ids=["string", "object", "int"])
def test_parse_inbound_a_non_list_messages_field_yields_nothing(messages):
    assert provider.parse_inbound_webhook(_inbound(messages=messages)) == []


def test_parse_inbound_accepts_integer_ids_as_text():
    payload = _inbound(
        [_msg(**{"from": int(PHONE_DIGITS)})], metadata={"phone_number_id": int(SHARED_PHONE_NUMBER_ID)}
    )
    [message] = provider.parse_inbound_webhook(payload)
    assert (message.phone_number_id, message.from_phone, message.text) == (SHARED_PHONE_NUMBER_ID, PHONE_DIGITS, "STOP")


def test_parse_inbound_keeps_the_valid_message_in_a_mixed_delivery():
    messages = [
        "garbage",
        None,
        _msg(text="STOP"),
        _msg(text={"body": 1}),
        _msg(**{"from": None}),
        _msg(type="button"),
        _msg(),  # the one valid text STOP
    ]
    parsed = provider.parse_inbound_webhook(_inbound(messages))

    assert [m.text for m in parsed] == [None, None, None, "STOP"]
    assert all(m.from_phone == PHONE_DIGITS for m in parsed)
