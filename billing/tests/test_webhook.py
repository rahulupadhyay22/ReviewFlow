"""Razorpay billing webhook receiver (spec 07 "Webhook design" and the webhook
Definition of done; A5 event-id rule; F1 malformed-ref rule).

Every request is signed locally with a test secret: deterministic, no network.
Razorpay's API is the FakeProvider from conftest, so the sync the receiver
enqueues never leaves the process. Plain django_db tests never commit, so the
on-commit enqueue is observed with django_capture_on_commit_callbacks
(execute=True); with Celery in eager mode the sync then runs inline. That is a
test-only device: production enqueues after commit to a worker.

Developer checks; the formal feature-test pass is /test-feature."""
import contextvars
import json
import logging
import types

import pytest
from django.db import IntegrityError, connection
from django.test.utils import CaptureQueriesContext

from billing import razorpay, services, tasks
from billing.models import BillingEvent, PaymentAttempt, Subscription
from billing.services import event_id_failure
from billing.tests.webhook_helpers import (
    END,
    REF,
    SECRET,
    START,
    URL,
    ORIGINAL_FETCH_INVOICES,
    charged,
    deliver,
    deliver_committed,
    event,
    no_billing_rows,
    processed,
    rows,
    run_task,
    sign,
)
from core.tenancy import tenant_atomic, tenant_context
from integrations.throttling import WebhookIpRateThrottle

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("webhook_secret")]


# === the happy path ===========================================================


def test_a_signed_charged_event_is_stored_and_the_sync_activates_from_fetched_state(
    webhook_setup, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        resp = deliver(event())
    assert resp.status_code == 200 and resp.json() == {}
    assert len(callbacks) == 1  # one sync enqueued, after commit
    r = rows(webhook_setup.a.merchant)
    assert r["status"] == "ACTIVE"
    assert [e[0] for e in r["events"]] == ["evt_test_0001"] and r["events"][0][1] is not None
    assert r["payments"] == ["pay_TestPay0000001"]  # from the paid invoice, via the sync
    assert r["usage"] == 1
    assert r["audits"] == ["billing.subscription_activated"]
    assert webhook_setup.provider.names() == ["fetch_subscription", "fetch_invoices"]


def test_the_receiver_itself_makes_no_provider_call_and_writes_no_audit_row(
    webhook_setup, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        assert deliver(event()).status_code == 200
    assert webhook_setup.provider.calls == []  # the sync runs later, not in the request
    r = rows(webhook_setup.a.merchant)
    assert r["status"] == "INCOMPLETE" and r["audits"] == []
    assert len(r["events"]) == 1 and r["events"][0][1] is None  # stored, unprocessed
    assert len(callbacks) == 1


def test_the_same_delivery_five_times_is_one_event_one_transition_one_audit_row(
    webhook_setup, django_capture_on_commit_callbacks
):
    enqueued = 0
    for _ in range(5):
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            assert deliver(event()).status_code == 200
        enqueued += len(callbacks)
    r = rows(webhook_setup.a.merchant)
    assert len(r["events"]) == 1 and r["payments"] == ["pay_TestPay0000001"]
    assert r["audits"] == ["billing.subscription_activated"]
    assert enqueued == 1  # a duplicate is a no-op: no second enqueue


def test_out_of_order_pending_after_charged_while_the_provider_says_active_changes_nothing(
    webhook_setup, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        deliver(event(), event_id="evt_charged")
    with django_capture_on_commit_callbacks(execute=True):
        deliver(event("subscription.pending", status="pending"), event_id="evt_pending")
    r = rows(webhook_setup.a.merchant)
    assert r["status"] == "ACTIVE"
    assert sorted(e[0] for e in r["events"]) == ["evt_charged", "evt_pending"]
    assert r["audits"] == ["billing.subscription_activated"]


def test_event_ids_are_compared_exactly_and_case_sensitively(webhook_setup, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=False):
        assert deliver(event(), event_id="evt_Case").status_code == 200
        assert deliver(event(), event_id="evt_case").status_code == 200
    assert sorted(e[0] for e in rows(webhook_setup.a.merchant)["events"]) == ["evt_Case", "evt_case"]


# === signature and event id: rejected before anything is read ===================


def _rejected_with_no_queries(**kwargs):
    with CaptureQueriesContext(connection) as ctx:
        resp = deliver(event(), **kwargs)
    assert resp.status_code == 401
    assert resp.json() == {"error": {"code": "invalid_signature", "message": "Invalid webhook signature."}}
    assert ctx.captured_queries == []  # no lookup, no write, nothing read
    return resp


def test_missing_wrong_altered_and_wrong_key_signatures_are_the_identical_401(webhook_setup):
    body = json.dumps(event()).encode()
    _rejected_with_no_queries(signature=None)
    _rejected_with_no_queries(signature="")
    _rejected_with_no_queries(signature="0" * 64)
    _rejected_with_no_queries(signature=sign(body, "another-secret"))
    resp = deliver(body + b" ", raw=True, signature=sign(body))  # altered after signing
    assert resp.status_code == 401
    no_billing_rows(webhook_setup.a.merchant, webhook_setup.b.merchant)


def test_an_empty_webhook_secret_rejects_even_an_empty_key_signature(webhook_setup, settings):
    settings.RAZORPAY_WEBHOOK_SECRET = ""
    _rejected_with_no_queries(secret="")
    no_billing_rows(webhook_setup.a.merchant)


@pytest.mark.parametrize(
    "event_id",
    [
        None,  # missing
        "",  # empty
        "e" * 65,  # too long
        "evt 1",  # whitespace
        " evt_1",  # surrounding whitespace is not trimmed
        "evt_1\t",
        "evt_\x01",  # control character
        "evt_\x7f",
        "evt_é",  # non-ASCII
    ],
)
def test_an_invalid_event_id_is_the_identical_401_with_nothing_stored(webhook_setup, event_id):
    _rejected_with_no_queries(event_id=event_id)
    no_billing_rows(webhook_setup.a.merchant)


def test_a_64_character_printable_event_id_is_accepted(webhook_setup, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=False):
        assert deliver(event(), event_id="!" + "~" * 62 + "Z").status_code == 200
    assert len(rows(webhook_setup.a.merchant)["events"]) == 1


@pytest.mark.parametrize(
    "value, category",
    [
        (None, "missing"),
        (12345, "not_string"),
        (b"evt_1", "not_string"),
        ("", "empty"),
        ("x" * 65, "too_long"),
        ("evt 1", "invalid_characters"),
        ("evt_1" + chr(10), "invalid_characters"),  # fullmatch: no trailing newline
        ("évt", "invalid_characters"),
        ("evt_1", None),
        ("x" * 64, None),
        ("!~", None),
    ],
)
def test_the_event_id_validator_returns_the_failure_category(value, category):
    assert event_id_failure(value) == category


def test_rejections_log_the_category_only_never_the_id_signature_body_or_secret(webhook_setup, caplog):
    secret_id = "evt_SENSITIVE" + "x" * 60  # too long
    body = json.dumps(event(notes={"marker": "BODY-MARKER"})).encode()
    with caplog.at_level(logging.DEBUG):
        deliver(body, raw=True, event_id=secret_id)
        deliver(body, raw=True, signature="f" * 64)
    text = caplog.text
    assert "too_long" in text
    for forbidden in (secret_id, "evt_SENSITIVE", str(len(secret_id)), "BODY-MARKER", "f" * 64, SECRET, sign(body)):
        assert forbidden not in text


def test_the_per_ip_throttle_answers_429_before_the_signature_is_checked(webhook_setup, monkeypatch):
    monkeypatch.setattr(WebhookIpRateThrottle, "THROTTLE_RATES", {"webhook_ip": "2/min"})
    for _ in range(2):
        assert deliver(event(), signature="bad").status_code == 401
    resp = deliver(event())  # validly signed, but over the limit
    assert resp.status_code == 429 and "Retry-After" in resp
    no_billing_rows(webhook_setup.a.merchant)


# === payload and ref handling =====================================================


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"text"', b"{}", b'{"event": 7}', b""])
def test_a_signed_body_that_is_not_an_event_object_is_400_invalid_payload(webhook_setup, body):
    resp = deliver(body, raw=True)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_payload"
    no_billing_rows(webhook_setup.a.merchant)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("payload"),
        lambda p: p["payload"].pop("subscription"),
        lambda p: p["payload"].update(subscription="nope"),
        lambda p: p["payload"]["subscription"].pop("entity"),
        lambda p: p["payload"]["subscription"]["entity"].pop("id"),
    ],
)
def test_an_event_without_a_subscription_entity_is_200_with_nothing_stored(webhook_setup, mutate):
    payload = event()
    mutate(payload)
    with CaptureQueriesContext(connection) as ctx:
        resp = deliver(payload)
    assert resp.status_code == 200 and resp.json() == {}
    assert ctx.captured_queries == []
    no_billing_rows(webhook_setup.a.merchant)


@pytest.mark.parametrize(
    "bad_ref", [None, 12345, ["sub_1"], "", "sub-1", "sub 1", "x" * 65, "sub_1" + chr(10), "MALFORMED-REF-MARKER"]
)
def test_a_malformed_subscription_ref_is_200_with_no_lookup_and_is_never_logged(webhook_setup, bad_ref, caplog):
    payload = event()
    payload["payload"]["subscription"]["entity"]["id"] = bad_ref
    with caplog.at_level(logging.DEBUG), CaptureQueriesContext(connection) as ctx:
        resp = deliver(payload)
    assert resp.status_code == 200 and resp.json() == {}
    assert ctx.captured_queries == []  # no billing_ref_lookup, nothing stored
    assert "MALFORMED-REF-MARKER" not in caplog.text
    no_billing_rows(webhook_setup.a.merchant)


def test_an_unknown_but_well_formed_ref_is_200_with_nothing_stored(webhook_setup):
    resp = deliver(event(ref="sub_NobodyKnowsThis"))
    assert resp.status_code == 200 and resp.json() == {}
    no_billing_rows(webhook_setup.a.merchant, webhook_setup.b.merchant)
    assert webhook_setup.provider.calls == []


def test_any_signed_event_with_a_subscription_entity_is_stored(webhook_setup, django_capture_on_commit_callbacks):
    """The spec filters only on the subscription entity: no event-type allowlist."""
    with django_capture_on_commit_callbacks(execute=False):
        deliver(event("subscription.updated"), event_id="evt_u")
        deliver(event("subscription.authenticated", status="authenticated"), event_id="evt_a")
    assert sorted(e[0] for e in rows(webhook_setup.a.merchant)["events"]) == ["evt_a", "evt_u"]


# === tenancy ============================================================================


def test_the_merchant_comes_only_from_the_stored_ref_never_from_the_payload(
    webhook_setup, django_capture_on_commit_callbacks
):
    payload = event(notes={"merchant_id": str(webhook_setup.b.merchant.id)})
    payload["payload"]["subscription"]["entity"]["notes"] = {"merchant_id": str(webhook_setup.b.merchant.id)}
    with django_capture_on_commit_callbacks(execute=True):
        assert deliver(payload).status_code == 200
    assert rows(webhook_setup.a.merchant)["status"] == "ACTIVE"
    b = rows(webhook_setup.b.merchant)
    assert b["events"] == [] and b["status"] == "ACTIVE" and b["audits"] == []


def test_an_event_for_b_s_ref_writes_only_b_s_rows(webhook_setup, django_capture_on_commit_callbacks):
    webhook_setup.provider.add("sub_B", status="active", plan_id="plan_x", current_start=START, current_end=END)
    with django_capture_on_commit_callbacks(execute=False):
        assert deliver(event(ref="sub_B"), event_id="evt_for_b").status_code == 200
    assert [e[0] for e in rows(webhook_setup.b.merchant)["events"]] == ["evt_for_b"]
    assert rows(webhook_setup.a.merchant)["events"] == []


def test_a_logged_in_dashboard_session_of_another_merchant_does_not_wrap_the_webhook(
    webhook_setup, login, django_capture_on_commit_callbacks
):
    client, resp = login(webhook_setup.b.user.email)
    assert resp.status_code == 200
    with django_capture_on_commit_callbacks(execute=True):
        assert deliver(event(), client=client).status_code == 200  # judged by signature alone
    assert rows(webhook_setup.a.merchant)["status"] == "ACTIVE"
    assert rows(webhook_setup.b.merchant)["events"] == []
    assert deliver(event(), client=client, signature="bad").status_code == 401  # the session never helps


# === fail closed ===========================================================================


def test_a_broker_failure_at_enqueue_still_returns_200_and_the_event_stays_unprocessed(
    webhook_setup, monkeypatch, django_capture_on_commit_callbacks, caplog
):
    def broken_delay(*args, **kwargs):
        raise ConnectionError("redis://user:pass@host:6379")

    monkeypatch.setattr(tasks.sync_subscription, "delay", broken_delay)
    with caplog.at_level(logging.WARNING), django_capture_on_commit_callbacks(execute=True):
        resp = deliver(event())
    assert resp.status_code == 200
    r = rows(webhook_setup.a.merchant)
    assert r["status"] == "INCOMPLETE" and len(r["events"]) == 1 and r["events"][0][1] is None
    assert "ConnectionError" in caplog.text and "pass@host" not in caplog.text


def test_active_without_a_qualifying_invoice_never_activates(webhook_setup, django_capture_on_commit_callbacks):
    webhook_setup.provider.invoices = [{**webhook_setup.provider.invoices[0], "status": "issued", "payment_id": None, "amount_due": 10000}]
    with django_capture_on_commit_callbacks(execute=True):
        assert deliver(event()).status_code == 200
    r = rows(webhook_setup.a.merchant)
    assert r["status"] == "INCOMPLETE" and r["usage"] == 0 and r["payments"] == []
    assert r["events"][0][1] is None  # stays unprocessed for the sweep
    assert r["audits"] == []


def test_a_provider_failure_during_the_sync_applies_nothing(webhook_setup, django_capture_on_commit_callbacks):
    from billing.exceptions import BillingProviderUnavailable

    webhook_setup.provider.fetch_error = BillingProviderUnavailable()
    with django_capture_on_commit_callbacks(execute=True):
        assert deliver(event()).status_code == 200
    r = rows(webhook_setup.a.merchant)
    assert r["status"] == "INCOMPLETE" and r["events"][0][1] is None and r["audits"] == []


def test_d1_the_unimplemented_invoice_fetch_fails_closed(
    webhook_setup, monkeypatch, django_capture_on_commit_callbacks, caplog
):
    """With the real fetch_invoices (an explicit D1 stop marker), the
    webhook-triggered sync cannot prove the period paid. Nothing is
    activated, no period or usage is created, and the event stays
    unprocessed. In this eager test the error surfaces inside the
    robust-enqueue guard and is logged; in production the worker task
    itself raises."""
    monkeypatch.setattr(razorpay, "fetch_invoices", ORIGINAL_FETCH_INVOICES)
    with caplog.at_level(logging.WARNING), django_capture_on_commit_callbacks(execute=True):
        assert deliver(event()).status_code == 200
    # The D1 marker is what stopped the sync: the subscription was fetched,
    # the (faked) invoice list never was, and the guard logged the class name.
    assert webhook_setup.provider.names() == ["fetch_subscription"]
    assert "failed with NotImplementedError" in caplog.text
    r = rows(webhook_setup.a.merchant)
    assert r["status"] == "INCOMPLETE" and r["usage"] == 0 and r["payments"] == []
    assert len(r["events"]) == 1 and r["events"][0][1] is None
    assert r["audits"] == []
    with tenant_context(webhook_setup.a.merchant.id), tenant_atomic():
        sub = Subscription.objects.get()
        assert sub.current_period_start is None and sub.current_period_end is None
        assert sub.provider_synced_at is None  # no snapshot was applied


def test_the_receiver_writes_no_payment_attempt_the_sync_does_from_the_paid_invoice(
    webhook_setup, django_capture_on_commit_callbacks
):
    """Decided 2026-10-01: the charged payload carries no invoice and so no
    paid_at; the only ledger writer is the sync, from the qualifying invoice."""
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        assert deliver(event()).status_code == 200
    assert rows(webhook_setup.a.merchant)["payments"] == []  # nothing from the payload

    for callback in callbacks:
        callback()  # now run the sync the receiver enqueued
    with tenant_context(webhook_setup.a.merchant.id), tenant_atomic():
        [payment] = list(PaymentAttempt.objects.all())
    assert payment.provider_attempt_id == "pay_TestPay0000001"
    assert int(payment.attempted_at.timestamp()) == START + 60  # the invoice's paid_at


def test_a_payload_payment_id_unknown_to_any_invoice_is_never_recorded(
    webhook_setup, django_capture_on_commit_callbacks
):
    payload = event()
    payload["payload"]["payment"]["entity"]["id"] = "pay_OnlyInThePayload"
    webhook_setup.provider.invoices = []  # nothing proves a payment
    with django_capture_on_commit_callbacks(execute=True):
        assert deliver(payload).status_code == 200
    assert rows(webhook_setup.a.merchant)["payments"] == []


@pytest.mark.parametrize("event_type", ["", "e" * 65, "subscription." + "x" * 200])
def test_an_empty_or_over_long_event_type_is_400_with_nothing_stored(webhook_setup, event_type, caplog):
    """Decided 2026-10-01: never truncated, never stored, never logged, no
    lookup, and no 500 from the 64-character column."""
    with caplog.at_level(logging.DEBUG), CaptureQueriesContext(connection) as ctx:
        resp = deliver(event(event_type))
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_payload"
    assert ctx.captured_queries == []
    no_billing_rows(webhook_setup.a.merchant)
    if event_type:
        assert event_type not in caplog.text


def test_a_64_character_event_type_is_accepted_and_stored_unchanged(webhook_setup, django_capture_on_commit_callbacks):
    event_type = "subscription." + "y" * 51
    assert len(event_type) == 64
    with django_capture_on_commit_callbacks(execute=False):
        assert deliver(event(event_type)).status_code == 200
    with tenant_context(webhook_setup.a.merchant.id), tenant_atomic():
        assert BillingEvent.objects.get().event_type == event_type


def test_the_receiver_requires_no_tenant_context_and_runs_with_none(webhook_setup, rf):
    from core.tenancy import get_current_merchant_id

    assert get_current_merchant_id() is None
    body = json.dumps(event(ref="sub_NobodyKnowsThis")).encode()
    request = rf.post(
        URL,
        data=body,
        content_type="application/json",
        HTTP_X_RAZORPAY_SIGNATURE=sign(body),
        HTTP_X_RAZORPAY_EVENT_ID="evt_direct",
    )
    assert services.receive_webhook(request=request) is None


# === log-line injection through the event type ==============================


FORGED = "subscription.charged" + chr(10) + "WARNING forged: payment captured" + chr(13) + chr(27) + "[2J"
CONTROL = (chr(10), chr(13), chr(27))


@pytest.mark.parametrize("ref", ["bad ref", "sub_NobodyKnowsThis"])
def test_a_control_character_event_type_is_escaped_in_the_log(webhook_setup, ref, caplog):
    """Both "ignored" log lines (malformed ref, unknown ref) log the event type
    escaped, so a signed body cannot start a forged log line."""
    with caplog.at_level(logging.DEBUG):
        assert deliver(event(FORGED, ref=ref)).status_code == 200
    messages = [r.getMessage() for r in caplog.records if r.name.startswith("billing")]
    assert any("ignored" in m for m in messages)
    for m in messages:
        assert not any(c in m for c in CONTROL)
    assert repr(FORGED) in caplog.text


@pytest.mark.parametrize("ref", ["bad ref", "sub_NobodyKnowsThis"])
def test_the_ignored_log_lines_never_carry_the_provider_event_id(webhook_setup, ref, caplog):
    """Malformed ref and unknown ref: each logs one fixed reason and the event
    type, never the x-razorpay-event-id."""
    event_id = "evt_LeakMarker_42"
    with caplog.at_level(logging.DEBUG):
        assert deliver(event(ref=ref), event_id=event_id).status_code == 200
    ignored = [r.getMessage() for r in caplog.records if r.name.startswith("billing") and "ignored" in r.getMessage()]
    reason = "malformed subscription ref" if ref == "bad ref" else "no matching subscription"
    assert ignored == [f"Razorpay webhook ('subscription.charged') ignored: {reason}"]
    assert event_id not in caplog.text


def test_a_control_character_event_type_is_stored_unaltered(webhook_setup, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=False):
        assert deliver(event(FORGED)).status_code == 200
    with tenant_context(webhook_setup.a.merchant.id), tenant_atomic():
        assert BillingEvent.objects.get().event_type == FORGED


# === one sync per committed event; the task skips only observed events ======


def test_a_replay_burst_queues_one_task_per_event_but_only_the_first_fetches(
    webhook_setup, queued, django_capture_on_commit_callbacks
):
    a = webhook_setup.a.merchant
    body = json.dumps(event()).encode()
    for n in range(5):
        deliver_committed(django_capture_on_commit_callbacks, body, raw=True, event_id=f"evt_replay_{n}")
    assert queued == [a.id] * 5  # every committed event has its own sync
    for merchant_id in queued:
        run_task(merchant_id)
    assert webhook_setup.provider.count("fetch_subscription") == 1
    assert set(processed(a).values()) == {True}
    assert rows(a)["status"] == "ACTIVE"


def test_the_task_skips_the_fetch_only_when_no_event_of_its_merchant_is_unprocessed(
    webhook_setup, queued, django_capture_on_commit_callbacks
):
    a, b = webhook_setup.a.merchant, webhook_setup.b.merchant
    run_task(a.id)  # no event at all
    assert webhook_setup.provider.count("fetch_subscription") == 0

    deliver_committed(django_capture_on_commit_callbacks, event(), event_id="evt_a")
    run_task(a.id)
    run_task(a.id)  # a duplicate task for an event already observed
    assert webhook_setup.provider.count("fetch_subscription") == 1

    # B's unprocessed event never makes A's task fetch, and B's own task does.
    webhook_setup.provider.add("sub_B", status="active", plan_id="plan_none", current_start=START, current_end=END)
    deliver_committed(django_capture_on_commit_callbacks, event(ref="sub_B"), event_id="evt_b")
    run_task(a.id)
    assert webhook_setup.provider.count("fetch_subscription") == 1
    run_task(b.id)
    assert ("fetch_subscription", "sub_B") in webhook_setup.provider.calls


def test_an_event_received_while_a_sync_is_fetching_is_observed_by_its_own_task(
    webhook_setup, queued, monkeypatch, django_capture_on_commit_callbacks
):
    a = webhook_setup.a.merchant
    deliver_committed(django_capture_on_commit_callbacks, event(), event_id="evt_before")
    fake_fetch = razorpay.fetch_subscription
    arrived = []

    def fetch_while_a_webhook_arrives(ref):
        if not arrived:
            arrived.append(True)
            # The receiver runs with no tenant context, as in production.
            contextvars.Context().run(
                deliver_committed, django_capture_on_commit_callbacks, event(), event_id="evt_during"
            )
        return fake_fetch(ref)

    monkeypatch.setattr(razorpay, "fetch_subscription", fetch_while_a_webhook_arrives)
    run_task(queued[0])
    assert processed(a) == {"evt_before": True, "evt_during": False}  # this fetch may not reflect it
    assert queued == [a.id, a.id]  # ...so it queued its own task
    run_task(queued[1])
    assert processed(a) == {"evt_before": True, "evt_during": True}
    assert webhook_setup.provider.count("fetch_subscription") == 2


def test_while_the_period_is_not_proven_paid_every_task_fetches_and_nothing_is_stranded(
    webhook_setup, queued, django_capture_on_commit_callbacks
):
    a = webhook_setup.a.merchant
    webhook_setup.provider.invoices = []  # no qualifying invoice: never settled
    for n in range(3):
        deliver_committed(django_capture_on_commit_callbacks, event(), event_id=f"evt_unpaid_{n}")
    for merchant_id in queued:
        run_task(merchant_id)
    assert webhook_setup.provider.count("fetch_subscription") == 3
    assert set(processed(a).values()) == {False}
    assert rows(a)["status"] == "INCOMPLETE"  # fail closed


def test_an_event_whose_enqueue_was_lost_is_observed_by_the_next_events_task(
    webhook_setup, monkeypatch, django_capture_on_commit_callbacks
):
    """A broker failure (or a process exit) after commit loses that event's
    task. Nothing suppresses the next event's task, and its sync covers both."""
    a = webhook_setup.a.merchant
    attempts = []

    def delay_failing_once(merchant_id):
        attempts.append(merchant_id)
        if len(attempts) == 1:
            raise ConnectionError("broker down")

    monkeypatch.setattr(tasks.sync_subscription, "delay", delay_failing_once)
    deliver_committed(django_capture_on_commit_callbacks, event(), event_id="evt_lost")
    assert attempts == [a.id] and processed(a) == {"evt_lost": False}  # that event's task is gone
    deliver_committed(django_capture_on_commit_callbacks, event(), event_id="evt_next")
    assert attempts == [a.id, a.id]  # nothing suppressed the next event's task
    run_task(a.id)
    assert processed(a) == {"evt_lost": True, "evt_next": True}


# === only the provider/event-id unique violation is a duplicate ==================


def _integrity_error(sqlstate, constraint):
    """An IntegrityError shaped like Django's wrapper of a psycopg error."""

    class DriverError(Exception):
        pass

    cause = DriverError()
    cause.sqlstate = sqlstate
    cause.diag = types.SimpleNamespace(constraint_name=constraint)
    exc = IntegrityError("simulated")
    exc.__cause__ = cause
    return exc


def test_the_duplicate_constraint_name_is_the_models_unique_constraint():
    assert services.EVENT_UNIQUE_CONSTRAINT in {c.name for c in BillingEvent._meta.constraints}


@pytest.mark.parametrize(
    "sqlstate, constraint, duplicate",
    [
        ("23505", services.EVENT_UNIQUE_CONSTRAINT, True),
        ("23505", "billing_subscription_ref_uniq", False),  # another unique constraint
        ("23502", services.EVENT_UNIQUE_CONSTRAINT, False),  # not a unique violation
        (None, None, False),
    ],
)
def test_only_a_unique_violation_of_the_event_constraint_is_a_duplicate(sqlstate, constraint, duplicate):
    assert services._is_duplicate_event(_integrity_error(sqlstate, constraint)) is duplicate


def test_an_integrity_error_without_a_driver_cause_is_not_a_duplicate():
    assert services._is_duplicate_event(IntegrityError("no cause")) is False


def test_an_unrelated_constraint_failure_on_the_event_insert_is_never_a_duplicate(
    webhook_setup, queued, monkeypatch
):
    """It must surface (an error, nothing stored, nothing queued), not be
    answered as a duplicate 200 that silently drops the event."""

    def failing_create(**kwargs):
        raise _integrity_error("23505", "billing_subscription_ref_uniq")

    monkeypatch.setattr(BillingEvent.objects, "create", failing_create)
    with pytest.raises(IntegrityError):
        deliver(event())
    assert queued == []
    no_billing_rows(webhook_setup.a.merchant)


# === the enqueue guard stays broad, by design =====================================


def test_any_exception_from_the_enqueue_is_still_a_200_logged_by_class_name(
    webhook_setup, monkeypatch, django_capture_on_commit_callbacks, caplog
):
    """Not only connection errors: the event is committed, so a 500 would make
    Razorpay retry the same id, which is then a duplicate and queues nothing."""

    def broken_delay(merchant_id):
        raise RuntimeError("detail-that-must-not-be-logged")

    monkeypatch.setattr(tasks.sync_subscription, "delay", broken_delay)
    with caplog.at_level(logging.WARNING), django_capture_on_commit_callbacks(execute=True):
        assert deliver(event()).status_code == 200
    assert [e[0] for e in rows(webhook_setup.a.merchant)["events"]] == ["evt_test_0001"]
    assert "RuntimeError" in caplog.text and "detail-that-must-not-be-logged" not in caplog.text
