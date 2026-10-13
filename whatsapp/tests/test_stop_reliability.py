"""STOP opt-out reliability (foundation Amendment A-1, 2026-10-12): a broker
failure answers 503 so Meta redelivers; the fan-out opts out inline, attempts
every merchant, and retries only the failures, bounded, never silently.

Celery is eager (root conftest), so `.delay()` runs the task inline and a
`self.retry()` re-runs it synchronously (the countdown is ignored), which lets
the tests count executions exactly."""
import hashlib
import hmac
import json
import logging
from unittest.mock import Mock

import pytest
from django.db import OperationalError
from kombu.exceptions import OperationalError as BrokerError
from rest_framework.test import APIClient

from config import celery_app
from core.tenancy import tenant_atomic, tenant_context
from customers.models import Customer
from whatsapp import services, tasks
from whatsapp.exceptions import OptOutFanOutIncomplete, OptOutTargetLookupFailed
from whatsapp.tests.conftest import fixture_bytes, load_fixture
from whatsapp.tests.helpers import APP_SECRET, CUSTOMER_PHONE, SHARED_PHONE_NUMBER_ID

pytestmark = pytest.mark.django_db

URL = "/api/v1/webhooks/whatsapp"
OWN_PHONE_A = "200000000000001"
OWN_PHONE_B = "200000000000002"
PHONE_DIGITS = "919999990001"


@pytest.fixture(autouse=True)
def _settings(meta_settings):
    return meta_settings


# --- helpers -------------------------------------------------------------------------------------


def signed(raw: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def deliver(raw: bytes, signature="auto"):
    extra = {}
    sig = signed(raw) if signature == "auto" else signature
    if sig is not None:
        extra["HTTP_X_HUB_SIGNATURE_256"] = sig
    return APIClient().post(URL, data=raw, content_type="application/json", **extra)


def stop_payload(phone_number_id, *texts_and_senders):
    """A delivery for `phone_number_id` with one message per (text, from)."""
    payload = load_fixture("inbound_text_stop.json")
    value = payload["entry"][0]["changes"][0]["value"]
    value["metadata"]["phone_number_id"] = phone_number_id
    template = value["messages"][0]
    messages = []
    for index, (text, sender) in enumerate(texts_and_senders or [("STOP", PHONE_DIGITS)]):
        message = json.loads(json.dumps(template))
        message["text"]["body"] = text
        message["from"] = sender
        message["id"] = f"wamid.TESTRELIABILITY{index}"
        messages.append(message)
    value["messages"] = messages
    return json.dumps(payload).encode()


def customer_state(merchant, phone=CUSTOMER_PHONE):
    with tenant_context(merchant.id), tenant_atomic():
        row = Customer.objects.filter(phone=phone).first()
        return None if row is None else (row.opted_out, row.opted_out_at)


def broker_down(monkeypatch):
    delay = Mock(side_effect=BrokerError("broker unreachable"))
    monkeypatch.setattr(tasks.fan_out_inbound_opt_out, "apply_async", delay)
    return delay


@pytest.fixture
def real_retries(monkeypatch):
    """The root conftest sets task_eager_propagates=True, under which Celery
    raises Retry to the caller instead of re-running the task. Switching it off
    here makes `.delay()` run every retry synchronously (the countdown is
    ignored), and the final outcome is read from the task result."""
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", False)


@pytest.fixture
def own(make_merchant, make_customer, make_own_account):
    """A and B each own a number and have a customer with the same phone."""
    a, b = make_merchant("A").merchant, make_merchant("B").merchant
    for merchant in (a, b):
        make_customer(merchant, CUSTOMER_PHONE)
    make_own_account(a, phone_number_id=OWN_PHONE_A)
    make_own_account(b, phone_number_id=OWN_PHONE_B)
    return {"A": a, "B": b}


@pytest.fixture
def shared_world(make_merchant, make_customer, map_to_shared):
    """A, B, C each have a customer with the phone and a location mapped to
    the shared account."""
    merchants = {n: make_merchant(n).merchant for n in "ABC"}
    for merchant in merchants.values():
        make_customer(merchant, CUSTOMER_PHONE)
        map_to_shared(merchant)
    return merchants


class Recorder:
    """Wraps the real per-merchant service and fails on demand: `fail` maps a
    merchant id to how many of its first attempts should fail."""

    def __init__(self, monkeypatch, fail=None, error=OperationalError):
        self.real = services.opt_out_inbound_for_merchant
        self.fail = dict(fail or {})
        self.error = error
        self.calls = []
        monkeypatch.setattr(services, "opt_out_inbound_for_merchant", self)

    def __call__(self, merchant_id, phone, *, shared):
        self.calls.append(merchant_id)
        if self.fail.get(merchant_id, 0) > 0:
            self.fail[merchant_id] -= 1
            raise self.error("injected failure")
        return self.real(merchant_id, phone, shared=shared)

    def count(self, merchant):
        return self.calls.count(merchant.id)


# =============================================================================================
# The webhook view: 503 when a STOP cannot be published
# =============================================================================================


def test_a_stop_that_cannot_be_published_answers_503_with_zero_queries_and_no_phone_logged(
    monkeypatch, caplog, django_assert_num_queries
):
    broker_down(monkeypatch)
    caplog.set_level(logging.DEBUG)
    raw = stop_payload(SHARED_PHONE_NUMBER_ID)

    with django_assert_num_queries(0):
        resp = deliver(raw)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "service_unavailable"
    assert PHONE_DIGITS not in resp.content.decode()
    assert "whatsapp.optout_enqueue_failed" in caplog.text
    assert "BrokerError" in caplog.text or "OperationalError" in caplog.text  # class name only
    assert PHONE_DIGITS not in caplog.text and CUSTOMER_PHONE not in caplog.text
    assert "broker unreachable" not in caplog.text  # not the exception message


def test_a_non_stop_text_answers_200_and_never_touches_the_broker(monkeypatch):
    delay = broker_down(monkeypatch)

    resp = deliver(stop_payload(SHARED_PHONE_NUMBER_ID, ("hello there", PHONE_DIGITS)))

    assert resp.status_code == 200
    delay.assert_not_called()


def test_a_status_only_delivery_answers_200_with_the_broker_down(monkeypatch):
    delay = broker_down(monkeypatch)

    resp = deliver(fixture_bytes("status_delivered.json"))

    assert resp.status_code == 200
    delay.assert_not_called()


def test_an_invalid_signature_is_401_before_any_broker_call(monkeypatch):
    delay = broker_down(monkeypatch)

    resp = deliver(stop_payload(SHARED_PHONE_NUMBER_ID), signature="sha256=" + "0" * 64)

    assert resp.status_code == 401
    delay.assert_not_called()


def test_a_missing_signature_is_401_before_any_broker_call(monkeypatch):
    delay = broker_down(monkeypatch)

    assert deliver(stop_payload(SHARED_PHONE_NUMBER_ID), signature=None).status_code == 401
    delay.assert_not_called()


def test_a_mixed_batch_stops_at_the_first_failed_publish_and_answers_503(monkeypatch):
    delay = Mock(side_effect=[None, BrokerError("down"), None])
    monkeypatch.setattr(tasks.fan_out_inbound_opt_out, "apply_async", delay)
    raw = stop_payload(
        SHARED_PHONE_NUMBER_ID,
        ("STOP", "919999990001"),
        ("STOP", "919999990002"),
        ("STOP", "919999990003"),
    )

    resp = deliver(raw)

    assert resp.status_code == 503
    assert delay.call_count == 2  # the third was never attempted: fail fast


def test_a_delivery_mixing_a_stop_and_statuses_answers_503_when_the_stop_fails(monkeypatch):
    delay = broker_down(monkeypatch)

    resp = deliver(fixture_bytes("inbound_stop_with_status.json"))

    assert resp.status_code == 503
    assert delay.call_count == 1


def test_a_healthy_broker_still_answers_200_and_publishes_once_per_stop(monkeypatch):
    delay = Mock()
    monkeypatch.setattr(tasks.fan_out_inbound_opt_out, "apply_async", delay)

    resp = deliver(stop_payload(SHARED_PHONE_NUMBER_ID, ("STOP", "919999990001"), ("STOP", "919999990002")))

    assert resp.status_code == 200
    assert delay.call_count == 2


def test_redelivery_after_the_broker_recovers_opts_out_exactly_once(own, monkeypatch):
    raw = stop_payload(OWN_PHONE_A)
    real_publish = tasks.fan_out_inbound_opt_out.apply_async

    broker_down(monkeypatch)
    assert deliver(raw).status_code == 503
    assert customer_state(own["A"]) == (False, None)  # nothing was lost silently: Meta will retry

    monkeypatch.setattr(tasks.fan_out_inbound_opt_out, "apply_async", real_publish)  # broker is back
    assert deliver(raw).status_code == 200  # Meta's redelivery
    _, first_at = customer_state(own["A"])
    assert customer_state(own["A"])[0] is True

    assert deliver(raw).status_code == 200  # a duplicate delivery
    assert customer_state(own["A"]) == (True, first_at)  # idempotent: the first time is kept
    assert customer_state(own["B"]) == (False, None)  # tenant isolation


# =============================================================================================
# The fan-out: inline, every merchant attempted, failures retried and bounded
# =============================================================================================


def test_one_failing_merchant_is_retried_alone_and_the_others_are_not_rerun(
    shared_world, monkeypatch, real_retries
):
    a, b, c = shared_world["A"], shared_world["B"], shared_world["C"]
    recorder = Recorder(monkeypatch, fail={b.id: 1})

    result = tasks.fan_out_inbound_opt_out.delay(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)

    assert result.successful()
    for merchant in (a, b, c):
        assert customer_state(merchant)[0] is True
    assert recorder.count(a) == 1  # attempted once, not redone by the retry
    assert recorder.count(c) == 1
    assert recorder.count(b) == 2  # failed, then succeeded on the restricted retry


def test_the_retry_is_bounded_and_exhaustion_is_logged_without_the_phone(
    shared_world, monkeypatch, caplog, real_retries
):
    a, b, c = shared_world["A"], shared_world["B"], shared_world["C"]
    recorder = Recorder(monkeypatch, fail={b.id: 99})
    caplog.set_level(logging.DEBUG)

    result = tasks.fan_out_inbound_opt_out.delay(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)

    assert result.failed()  # not a silent success
    assert isinstance(result.result, OptOutFanOutIncomplete)
    assert recorder.count(b) == 1 + tasks.OPT_OUT_MAX_RETRIES  # the first run plus 5 retries, no more
    assert recorder.count(a) == 1 and recorder.count(c) == 1
    assert customer_state(a)[0] is True and customer_state(c)[0] is True  # the others still done
    assert customer_state(b) == (False, None)  # the failure is surfaced, not hidden
    exhausted = [r.getMessage() for r in caplog.records if "whatsapp.optout_fanout_exhausted" in r.getMessage()]
    assert len(exhausted) == 1
    assert str(b.id) in exhausted[0] and "1" in exhausted[0]
    assert PHONE_DIGITS not in caplog.text and CUSTOMER_PHONE not in caplog.text


def test_several_failures_in_one_run_are_all_retried_and_none_dropped(
    shared_world, monkeypatch, real_retries
):
    a, b, c = shared_world["A"], shared_world["B"], shared_world["C"]
    recorder = Recorder(monkeypatch, fail={a.id: 1, c.id: 2})

    result = tasks.fan_out_inbound_opt_out.delay(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)

    assert result.successful()
    assert all(customer_state(m)[0] is True for m in (a, b, c))
    assert (recorder.count(a), recorder.count(b), recorder.count(c)) == (2, 1, 3)


def test_a_retry_with_merchant_ids_only_touches_those_merchants(shared_world, monkeypatch):
    a, b, c = shared_world["A"], shared_world["B"], shared_world["C"]
    recorder = Recorder(monkeypatch)

    tasks.fan_out_inbound_opt_out(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE, merchant_ids=[str(b.id)])

    assert recorder.calls == [b.id]
    assert customer_state(b)[0] is True
    assert customer_state(a) == (False, None) and customer_state(c) == (False, None)


def test_a_failure_to_resolve_the_target_is_retried_not_swallowed(shared_world, monkeypatch, real_retries):
    real = services.is_shared_pool_number
    state = {"calls": 0}

    def flaky(phone_number_id):
        state["calls"] += 1
        if state["calls"] == 1:
            raise OperationalError("database blip")
        return real(phone_number_id)

    monkeypatch.setattr(services, "is_shared_pool_number", flaky)

    result = tasks.fan_out_inbound_opt_out.delay(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)

    assert result.successful()
    assert state["calls"] == 2
    assert all(customer_state(m)[0] is True for m in shared_world.values())


def test_an_unresolvable_target_that_never_recovers_is_bounded_and_logged(
    shared_world, monkeypatch, caplog, real_retries
):
    state = {"calls": 0}

    def always_down(phone_number_id):
        state["calls"] += 1
        raise OperationalError("database down")

    monkeypatch.setattr(services, "is_shared_pool_number", always_down)
    caplog.set_level(logging.DEBUG)

    result = tasks.fan_out_inbound_opt_out.delay(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)

    assert result.failed()
    # Sanitized: only the original class name survives (observability), never
    # the driver text, and nothing is chained behind it.
    assert isinstance(result.result, OptOutTargetLookupFailed)
    assert "OperationalError" in str(result.result)
    assert state["calls"] == 1 + tasks.OPT_OUT_MAX_RETRIES
    assert "whatsapp.optout_fanout_exhausted" in caplog.text
    assert CUSTOMER_PHONE not in caplog.text


def test_re_running_the_fan_out_is_idempotent_and_keeps_the_first_time(shared_world):
    tasks.fan_out_inbound_opt_out.delay(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)
    first = {name: customer_state(m) for name, m in shared_world.items()}

    tasks.fan_out_inbound_opt_out.delay(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)

    assert {name: customer_state(m) for name, m in shared_world.items()} == first
    assert all(opted for opted, _ in first.values())


# --- own numbers ------------------------------------------------------------------------------------


def test_an_own_number_stop_is_applied_inline_and_never_touches_another_merchant(own, monkeypatch):
    delay = Mock()
    monkeypatch.setattr(tasks.process_inbound_opt_out, "delay", delay)

    tasks.fan_out_inbound_opt_out.delay(OWN_PHONE_A, CUSTOMER_PHONE)

    delay.assert_not_called()  # no second broker hop
    assert customer_state(own["A"])[0] is True
    assert customer_state(own["B"]) == (False, None)


def test_an_own_number_failure_is_retried(own, monkeypatch, real_retries):
    recorder = Recorder(monkeypatch, fail={own["A"].id: 2})

    result = tasks.fan_out_inbound_opt_out.delay(OWN_PHONE_A, CUSTOMER_PHONE)

    assert result.successful()
    assert customer_state(own["A"])[0] is True
    assert recorder.count(own["A"]) == 3


def test_a_retry_list_cannot_widen_an_own_number_to_another_merchant(own, monkeypatch):
    """merchant_ids only ever narrows: naming B for A's number changes nobody."""
    recorder = Recorder(monkeypatch)

    tasks.fan_out_inbound_opt_out(OWN_PHONE_A, CUSTOMER_PHONE, merchant_ids=[str(own["B"].id)])

    assert recorder.calls == []
    assert customer_state(own["A"]) == (False, None)
    assert customer_state(own["B"]) == (False, None)


def test_an_unknown_number_is_a_no_op(own, monkeypatch):
    recorder = Recorder(monkeypatch)

    tasks.fan_out_inbound_opt_out.delay("299999999999999", CUSTOMER_PHONE)

    assert recorder.calls == []
    assert customer_state(own["A"]) == (False, None)


# --- worker-loss and retry configuration -------------------------------------------------------------


def test_the_opt_out_tasks_are_configured_for_safe_redelivery_and_bounded_retry():
    assert tasks.fan_out_inbound_opt_out.acks_late is True
    assert tasks.fan_out_inbound_opt_out.reject_on_worker_lost is True
    assert tasks.fan_out_inbound_opt_out.max_retries == tasks.OPT_OUT_MAX_RETRIES == 5
    assert tasks.process_inbound_opt_out.acks_late is True
    assert tasks.process_inbound_opt_out.reject_on_worker_lost is True
    # Not scheduled with eta/countdown: the retry countdown is a retry delay only.
    assert tasks.fan_out_inbound_opt_out.queue == "whatsapp"


def test_the_fan_out_takes_only_ids_and_the_phone_as_arguments():
    import inspect

    params = list(inspect.signature(tasks.fan_out_inbound_opt_out.run).parameters)
    assert params == ["phone_number_id", "phone", "merchant_ids"]


# =============================================================================================
# Quality-fix pass 3: target resolution in services, bounded publish, NoReturn helper
# =============================================================================================


def _delete_merchant(merchant):
    from accounts.models import Merchant

    Merchant.objects.filter(pk=merchant.pk).update(status=Merchant.Status.DELETED)


def test_the_shared_number_resolves_to_every_non_deleted_merchant(shared_world, make_merchant):
    deleted = make_merchant("Gone").merchant
    _delete_merchant(deleted)

    shared, merchant_ids = services.resolve_inbound_opt_out_targets(SHARED_PHONE_NUMBER_ID)

    assert shared is True
    assert {m.id for m in shared_world.values()} <= set(merchant_ids)
    assert deleted.id not in merchant_ids


@pytest.mark.parametrize("status", ["ACTIVE", "PENDING", "SUSPENDED"])
def test_an_own_number_resolves_to_its_merchant_only_whatever_its_status(
    make_merchant, make_own_account, status
):
    owner = make_merchant("Owner").merchant
    make_merchant("Other")
    make_own_account(owner, status=status, phone_number_id="200000000000555")

    assert services.resolve_inbound_opt_out_targets("200000000000555") == (False, [owner.id])


@pytest.mark.parametrize("unknown", ["299999999999999", "", "OWN-PHONE-ID-1", "12 3", "1" * 65])
def test_an_unknown_or_impossible_number_resolves_to_nothing(shared_account, unknown):
    assert services.resolve_inbound_opt_out_targets(unknown) == (False, [])


def test_a_resolution_failure_is_not_swallowed_into_unknown_number(monkeypatch):
    def boom(phone_number_id):
        raise OperationalError("database down")

    monkeypatch.setattr(services, "is_shared_pool_number", boom)

    with pytest.raises(OperationalError):
        services.resolve_inbound_opt_out_targets(SHARED_PHONE_NUMBER_ID)


def test_the_task_takes_its_targets_from_the_service(shared_world, monkeypatch):
    seen = {}
    real = services.resolve_inbound_opt_out_targets

    def spy(phone_number_id):
        seen["asked"] = phone_number_id
        return real(phone_number_id)

    monkeypatch.setattr(services, "resolve_inbound_opt_out_targets", spy)

    tasks.fan_out_inbound_opt_out.delay(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)

    assert seen["asked"] == SHARED_PHONE_NUMBER_ID
    assert all(customer_state(m)[0] is True for m in shared_world.values())


# --- the bounded publish -------------------------------------------------------------------------


def test_the_publish_uses_the_configured_timeout_and_retry_policy(monkeypatch, settings):
    from contextlib import contextmanager

    settings.WHATSAPP_WEBHOOK_PUBLISH_CONNECT_TIMEOUT = 1.5
    settings.WHATSAPP_WEBHOOK_PUBLISH_MAX_RETRIES = 2
    seen = {}
    marker = object()

    @contextmanager
    def fake_connection(**kwargs):
        seen["connection_kwargs"] = kwargs
        yield marker

    publish = Mock()
    monkeypatch.setattr(celery_app, "connection_for_write", fake_connection)
    monkeypatch.setattr(tasks.fan_out_inbound_opt_out, "apply_async", publish)

    tasks.publish_opt_out_fan_out(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)

    assert seen["connection_kwargs"] == {
        "connect_timeout": 1.5,
        "transport_options": {"socket_connect_timeout": 1.5, "socket_timeout": 1.5},
    }
    kwargs = publish.call_args.kwargs
    assert kwargs["args"] == (SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)
    assert kwargs["connection"] is marker
    assert kwargs["retry"] is True
    assert kwargs["retry_policy"] == {
        "max_retries": 2,
        "interval_start": 0,
        "interval_step": 0.2,
        "interval_max": 0.2,
    }


def test_the_publish_defaults_are_a_short_bound():
    from django.conf import settings as django_settings

    assert django_settings.WHATSAPP_WEBHOOK_PUBLISH_CONNECT_TIMEOUT == 2.0
    assert django_settings.WHATSAPP_WEBHOOK_PUBLISH_MAX_RETRIES == 1


@pytest.mark.parametrize(
    "broker_url",
    ["redis://127.0.0.1:1/0", "redis://10.255.255.1:6379/0"],
    ids=["connection-refused", "black-holed"],
)
def test_an_unreachable_broker_answers_503_within_the_configured_bound(
    broker_url, monkeypatch, settings, django_assert_num_queries
):
    """A REAL publish (Celery made non-eager) against an unreachable broker.
    With Celery's defaults this took 4.5 s (refused) to 21 s (black hole) when
    measured; the configured bound is (retries + 1) * timeout + retries * 0.2.
    The assertion is an upper bound, so a network that fails faster passes."""
    import time

    settings.WHATSAPP_WEBHOOK_PUBLISH_CONNECT_TIMEOUT = 1.0
    settings.WHATSAPP_WEBHOOK_PUBLISH_MAX_RETRIES = 1
    bound = (1 + 1) * 1.0 + 1 * 0.2
    monkeypatch.setattr(celery_app.conf, "task_always_eager", False)
    # conf.broker_url cannot be overridden at runtime; aim the dedicated
    # publish connection at the unreachable broker explicitly.
    real_connection = celery_app.connection_for_write
    monkeypatch.setattr(
        celery_app, "connection_for_write", lambda **kw: real_connection(url=broker_url, **kw)
    )

    started = time.monotonic()
    with django_assert_num_queries(0):
        resp = deliver(stop_payload(SHARED_PHONE_NUMBER_ID))
    elapsed = time.monotonic() - started

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "service_unavailable"
    assert elapsed < bound + 1.5, f"took {elapsed:.2f}s, bound {bound:.1f}s"


# --- the non-returning retry helper and the log cap -----------------------------------------------


class _FakeTask:
    max_retries = 5

    def __init__(self, retries, retry_raises=True):
        from types import SimpleNamespace

        self.request = SimpleNamespace(retries=retries)
        self.retry_calls = []
        self._retry_raises = retry_raises

    def retry(self, **kwargs):
        self.retry_calls.append(kwargs)
        if self._retry_raises:
            raise RuntimeError("Retry scheduled")
        return RuntimeError("retry returned instead of raising")


def test_the_retry_helper_always_raises_while_retries_remain():
    task = _FakeTask(retries=0)

    with pytest.raises(RuntimeError, match="Retry scheduled"):
        tasks._retry_opt_out_fan_out(task, "123", CUSTOMER_PHONE, [], OptOutFanOutIncomplete(1))

    kwargs = task.retry_calls[0]
    assert kwargs["args"] == ("123", CUSTOMER_PHONE)
    assert kwargs["kwargs"] == {"merchant_ids": []}
    assert 0 <= kwargs["countdown"] <= 300


def test_the_retry_helper_never_returns_even_if_retry_does():
    task = _FakeTask(retries=0, retry_raises=False)

    # `raise task.retry(...)` raises whatever retry returned; the helper cannot
    # fall through to the caller's following statements.
    with pytest.raises(RuntimeError, match="retry returned instead of raising"):
        tasks._retry_opt_out_fan_out(task, "123", CUSTOMER_PHONE, [], OptOutFanOutIncomplete(1))


def test_at_the_bound_the_helper_raises_the_original_error_and_logs_a_capped_id_list(caplog):
    import uuid

    ids = [uuid.uuid4() for _ in range(30)]
    task = _FakeTask(retries=5)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(OptOutFanOutIncomplete):
        tasks._retry_opt_out_fan_out(task, "123", CUSTOMER_PHONE, ids, OptOutFanOutIncomplete(30))

    assert task.retry_calls == []  # no further retry at the bound
    line = next(r.getMessage() for r in caplog.records if "optout_fanout_exhausted" in r.getMessage())
    assert "count=30" in line  # the full count is always logged
    logged_ids = [i for i in ids if str(i) in line]
    assert logged_ids == ids[:20]  # only the first 20 ids
    assert CUSTOMER_PHONE not in caplog.text


def test_a_lookup_failure_at_the_bound_logs_all_and_raises_what_it_is_given(caplog):
    task = _FakeTask(retries=5)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(OperationalError):
        tasks._retry_opt_out_fan_out(task, "123", CUSTOMER_PHONE, None, OperationalError("down"))

    assert "count=all" in caplog.text and "failed_merchants=None" in caplog.text


# --- sanitized lookup failure + result-backend bound (hardening pass 5) ---------------------------

SECRET_DRIVER_TEXT = 'connection to server at "db.internal.example" (10.1.2.3) failed: password=hunter2'


def test_a_lookup_failure_never_exposes_the_driver_text_in_logs_exceptions_or_chain(
    real_retries, monkeypatch, caplog
):
    seen = []
    real_retry = tasks.fan_out_inbound_opt_out.retry

    def spy_retry(*args, **kwargs):
        seen.append(kwargs["exc"])
        return real_retry(*args, **kwargs)

    def down(phone_number_id):
        raise OperationalError(SECRET_DRIVER_TEXT)

    monkeypatch.setattr(services, "is_shared_pool_number", down)
    monkeypatch.setattr(tasks.fan_out_inbound_opt_out, "retry", spy_retry)
    caplog.set_level(logging.DEBUG)

    result = tasks.fan_out_inbound_opt_out.delay(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)

    assert result.failed()
    error = result.result
    assert isinstance(error, OptOutTargetLookupFailed)
    assert str(error) == "Inbound opt-out target lookup failed (OperationalError)."
    # nothing chained behind the raised exception, and every exception handed to
    # Celery's retry (which prints it) was the sanitized one too
    assert error.__cause__ is None
    assert error.__suppress_context__ or error.__context__ is None
    assert seen and all(isinstance(e, OptOutTargetLookupFailed) for e in seen)
    for secret in ("hunter2", "db.internal.example", "10.1.2.3"):
        assert secret not in caplog.text
        assert secret not in str(error)
        assert all(secret not in str(e) for e in seen)
    assert CUSTOMER_PHONE not in caplog.text


def test_the_fan_out_task_does_not_store_a_result():
    # Pinned because a stored result makes the publish call the RESULT BACKEND,
    # which publish_opt_out_fan_out's timeouts do not reach (measured 64 s).
    assert tasks.fan_out_inbound_opt_out.ignore_result is True


@pytest.mark.parametrize(
    "dead_url",
    ["redis://127.0.0.1:1/0", "redis://10.255.255.1:6379/0"],
    ids=["connection-refused", "black-holed"],
)
def test_the_503_is_bounded_when_the_broker_and_the_result_backend_are_both_unreachable(
    dead_url, monkeypatch, settings, django_assert_num_queries
):
    """A REAL non-eager publish with BOTH the broker connection and the app's
    result backend aimed at an unreachable host. Measured before the task set
    ignore_result: about 64 s (RuntimeError). Asserts a short upper bound, not a
    guarantee: the computed ceiling is (retries + 1) * timeout + retries * 0.2."""
    import time

    from celery.backends.redis import RedisBackend

    settings.WHATSAPP_WEBHOOK_PUBLISH_CONNECT_TIMEOUT = 1.0
    settings.WHATSAPP_WEBHOOK_PUBLISH_MAX_RETRIES = 1
    ceiling = (1 + 1) * 1.0 + 1 * 0.2
    monkeypatch.setattr(celery_app.conf, "task_always_eager", False)
    real_connection = celery_app.connection_for_write
    monkeypatch.setattr(
        celery_app, "connection_for_write", lambda **kw: real_connection(url=dead_url, **kw)
    )
    # the app's result backend (what send_task consults) is dead as well
    monkeypatch.setattr(celery_app, "_backend_cache", RedisBackend(app=celery_app, url=dead_url))

    started = time.monotonic()
    with django_assert_num_queries(0):
        resp = deliver(stop_payload(SHARED_PHONE_NUMBER_ID))
    elapsed = time.monotonic() - started

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "service_unavailable"
    assert elapsed < ceiling + 1.5, f"took {elapsed:.2f}s, computed ceiling {ceiling:.1f}s"
