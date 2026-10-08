"""Celery tasks of the whatsapp app (spec 08 "Celery tasks", Definition of done
"Inbound webhook & opt-out", "Quality monitoring", and the Testing-Strategy
Celery tenant context scenario).

The per-merchant fan-outs register their follow-up enqueue with
transaction.on_commit, so tests that cross that boundary run inside
django_capture_on_commit_callbacks(execute=True) (Celery is eager)."""
import logging
from unittest.mock import Mock

import pytest

from accounts.models import Merchant
from core.tenancy import get_current_merchant_id, tenant_atomic, tenant_context
from customers.models import Customer
from whatsapp import services, tasks
from whatsapp.tests.conftest import http_error, load_fixture
from whatsapp.tests.helpers import ACCESS_TOKEN, CUSTOMER_PHONE, SHARED_PHONE_NUMBER_ID

pytestmark = pytest.mark.django_db


def customer_state(merchant, phone=CUSTOMER_PHONE):
    with tenant_context(merchant.id), tenant_atomic():
        row = Customer.objects.filter(phone=phone).first()
        return None if row is None else (row.opted_out, row.opted_out_at)


def delete_merchant(merchant):
    Merchant.objects.filter(pk=merchant.pk).update(status=Merchant.Status.DELETED)


def fail_for(monkeypatch, name, merchant_id):
    """Fault injection: the real service, except it raises in one merchant's
    context."""
    real = getattr(services, name)

    def wrapper(*args, **kwargs):
        if get_current_merchant_id() == merchant_id:
            raise RuntimeError("injected failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(services, name, wrapper)


# --- fan_out_inbound_opt_out (OD-1 (b)) ----------------------------------------------------------


@pytest.fixture
def world(make_merchant, make_customer, map_to_shared, make_location):
    """The phone is a Customer of A, B, C; D has the shared mapping but no
    customer; E is DELETED with a customer and a mapping."""
    a, b, c, d, e = (make_merchant(n).merchant for n in "ABCDE")
    for merchant in (a, b, c, e):
        make_customer(merchant, CUSTOMER_PHONE)
    for merchant in (a, b, d, e):
        map_to_shared(merchant)
    make_location(c)  # C: a location, but not mapped to the shared account
    delete_merchant(e)
    return {"A": a, "B": b, "C": c, "D": d, "E": e}


def test_a_stop_to_the_shared_number_opts_out_exactly_the_merchants_with_a_customer_and_a_shared_mapping(
    world, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        tasks.fan_out_inbound_opt_out(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)

    assert customer_state(world["A"])[0] is True
    assert customer_state(world["B"])[0] is True
    assert customer_state(world["C"]) == (False, None)  # no location mapped to the shared account
    assert customer_state(world["D"]) is None  # no Customer, and none is created
    assert customer_state(world["E"]) == (False, None)  # DELETED merchant skipped


def test_the_fan_out_is_idempotent(world, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        tasks.fan_out_inbound_opt_out(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)
    _, first_at = customer_state(world["A"])

    with django_capture_on_commit_callbacks(execute=True):
        tasks.fan_out_inbound_opt_out(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)

    assert customer_state(world["A"]) == (True, first_at)


def test_a_failure_for_one_merchant_does_not_stop_the_others_and_is_logged_without_the_phone(
    world, monkeypatch, caplog, django_capture_on_commit_callbacks
):
    fail_for(monkeypatch, "shared_opt_out_applies", world["A"].id)
    caplog.set_level(logging.DEBUG)

    with django_capture_on_commit_callbacks(execute=True):
        tasks.fan_out_inbound_opt_out(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)

    assert customer_state(world["A"]) == (False, None)
    assert customer_state(world["B"])[0] is True
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert str(world["A"].id) in logged and "RuntimeError" in logged
    assert "9999990001" not in logged


def test_an_unknown_phone_number_id_is_a_no_op(world, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        tasks.fan_out_inbound_opt_out("UNKNOWN-NUMBER", CUSTOMER_PHONE)

    assert customer_state(world["A"]) == (False, None)
    assert customer_state(world["B"]) == (False, None)


def test_a_non_shared_phone_number_id_is_a_no_op(world, make_own_account, django_capture_on_commit_callbacks):
    own = make_own_account(world["A"])

    with django_capture_on_commit_callbacks(execute=True):
        tasks.fan_out_inbound_opt_out(own.phone_number_id, CUSTOMER_PHONE)

    assert customer_state(world["A"]) == (False, None)


def test_the_fan_out_only_enqueues_per_merchant_opt_outs_for_applicable_merchants(
    world, monkeypatch, django_capture_on_commit_callbacks
):
    delay = Mock()
    monkeypatch.setattr(tasks.process_inbound_opt_out, "delay", delay)

    with django_capture_on_commit_callbacks(execute=True):
        tasks.fan_out_inbound_opt_out(SHARED_PHONE_NUMBER_ID, CUSTOMER_PHONE)

    enqueued = sorted(str(call.args[0]) for call in delay.call_args_list)
    assert enqueued == sorted([str(world["A"].id), str(world["B"].id)])
    assert all(call.args[1] == CUSTOMER_PHONE for call in delay.call_args_list)


def test_shared_opt_out_applies_is_scoped_to_the_current_merchant(world):
    # tenant_atomic issues the SET LOCAL RLS reads, as the real fan-out does.
    with tenant_context(world["A"].id), tenant_atomic():
        assert services.shared_opt_out_applies(CUSTOMER_PHONE) is True
        assert services.shared_opt_out_applies("+919999990099") is False
    with tenant_context(world["C"].id), tenant_atomic():
        assert services.shared_opt_out_applies(CUSTOMER_PHONE) is False
    with tenant_context(world["D"].id), tenant_atomic():
        assert services.shared_opt_out_applies(CUSTOMER_PHONE) is False


# --- process_inbound_opt_out (Celery tenant context) ------------------------------------------------


def test_process_inbound_opt_out_for_merchant_a_never_changes_merchant_bs_customer(world):
    tasks.process_inbound_opt_out(merchant_id=world["A"].id, phone=CUSTOMER_PHONE)

    assert customer_state(world["A"])[0] is True
    assert customer_state(world["B"]) == (False, None)


def test_process_inbound_opt_out_is_idempotent(world):
    tasks.process_inbound_opt_out(world["A"].id, CUSTOMER_PHONE)
    _, first_at = customer_state(world["A"])

    tasks.process_inbound_opt_out(world["A"].id, CUSTOMER_PHONE)

    assert customer_state(world["A"]) == (True, first_at)


def test_process_inbound_opt_out_runs_as_a_celery_task_with_an_explicit_merchant_id(world):
    tasks.process_inbound_opt_out.delay(world["B"].id, CUSTOMER_PHONE)

    assert customer_state(world["B"])[0] is True
    assert customer_state(world["A"]) == (False, None)


# --- poll_template_statuses --------------------------------------------------------------------------


def _template(merchant, name, **kwargs):
    from whatsapp.models import MessageTemplate

    with tenant_context(merchant.id), tenant_atomic():
        return MessageTemplate.objects.create(
            merchant_id=merchant.id, name=name, language="en", body="{{business_name}}", **kwargs
        )


@pytest.fixture
def poll_world(make_merchant):
    a, b, c, d, e = (make_merchant(n).merchant for n in "ABCDE")
    _template(a, "submitted", provider_template_id="P-A")  # PENDING, submitted: should poll
    _template(b, "unsubmitted")  # PENDING, never submitted
    _template(c, "done", provider_template_id="P-C", status="APPROVED")
    _template(d, "submitted", provider_template_id="P-D")
    _template(e, "submitted", provider_template_id="P-E")
    delete_merchant(e)
    return {"A": a, "B": b, "C": c, "D": d, "E": e}


def test_poll_enqueues_one_sync_per_merchant_with_a_submitted_pending_template_and_none_for_deleted_merchants(
    poll_world, monkeypatch, django_capture_on_commit_callbacks
):
    delay = Mock()
    monkeypatch.setattr(tasks.sync_merchant_templates, "delay", delay)

    with django_capture_on_commit_callbacks(execute=True):
        tasks.poll_template_statuses()

    enqueued = sorted(str(call.args[0]) for call in delay.call_args_list)
    assert enqueued == sorted([str(poll_world["A"].id), str(poll_world["D"].id)])


def test_poll_continues_with_other_merchants_when_one_fails(
    poll_world, monkeypatch, caplog, django_capture_on_commit_callbacks
):
    delay = Mock()
    monkeypatch.setattr(tasks.sync_merchant_templates, "delay", delay)
    fail_for(monkeypatch, "templates_pending_sync", poll_world["A"].id)
    caplog.set_level(logging.DEBUG)

    with django_capture_on_commit_callbacks(execute=True):
        tasks.poll_template_statuses()

    assert [str(c.args[0]) for c in delay.call_args_list] == [str(poll_world["D"].id)]
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert str(poll_world["A"].id) in logged and "RuntimeError" in logged


def test_poll_end_to_end_applies_the_meta_status_per_merchant(
    poll_world, shared_account, meta_http, django_capture_on_commit_callbacks
):
    meta_http.on("P-A", load_fixture("template_status_approved.json"))
    meta_http.on("P-D", load_fixture("template_status_rejected.json"))

    with django_capture_on_commit_callbacks(execute=True):
        tasks.poll_template_statuses()

    from whatsapp.models import MessageTemplate

    def status(merchant):
        with tenant_context(merchant.id), tenant_atomic():
            return set(MessageTemplate.objects.values_list("status", flat=True))

    assert status(poll_world["A"]) == {"APPROVED"}
    assert status(poll_world["D"]) == {"REJECTED"}
    assert status(poll_world["B"]) == {"PENDING"}
    assert not any("P-E" in url for url in meta_http.urls())


# --- monitor_shared_pool_quality -----------------------------------------------------------------------


def quality_logs(caplog):
    return [r for r in caplog.records if "whatsapp.shared_pool_quality_low" in r.getMessage()]


@pytest.mark.parametrize("rating", ["RED", "YELLOW"])
def test_a_low_quality_rating_emits_exactly_one_error_log_per_poll(shared_account, meta_http, caplog, rating):
    payload = load_fixture("quality_rating.json")
    payload["data"][0]["quality_rating"] = rating
    meta_http.on("/phone_numbers", payload)
    caplog.set_level(logging.DEBUG)

    tasks.monitor_shared_pool_quality()

    records = quality_logs(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert str(shared_account.id) in records[0].getMessage()
    assert "9999990001" not in caplog.text and ACCESS_TOKEN not in caplog.text

    tasks.monitor_shared_pool_quality()
    assert len(quality_logs(caplog)) == 2  # one per poll


@pytest.mark.parametrize("rating", ["GREEN", "UNKNOWN"])
def test_a_healthy_rating_emits_no_alert(shared_account, meta_http, caplog, rating):
    payload = load_fixture("quality_rating.json")
    payload["data"][0]["quality_rating"] = rating
    meta_http.on("/phone_numbers", payload)
    caplog.set_level(logging.DEBUG)

    tasks.monitor_shared_pool_quality()

    assert quality_logs(caplog) == []


def test_a_provider_error_is_logged_by_class_and_does_not_crash_the_beat_task(shared_account, meta_http, caplog):
    meta_http.on("/phone_numbers", http_error(500))
    caplog.set_level(logging.DEBUG)

    tasks.monitor_shared_pool_quality()  # does not raise

    assert quality_logs(caplog) == []
    assert "ProviderTransientError" in caplog.text
    assert ACCESS_TOKEN not in caplog.text and "SECRET-BODY-MARKER" not in caplog.text


def test_quality_monitoring_skips_a_non_active_shared_account(make_shared_account, meta_http, caplog):
    make_shared_account("SUSPENDED")

    tasks.monitor_shared_pool_quality()

    assert meta_http.requests == []


def test_quality_monitoring_without_a_shared_account_is_a_no_op(meta_http):
    tasks.monitor_shared_pool_quality()
    assert meta_http.requests == []
