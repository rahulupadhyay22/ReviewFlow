"""A STOP sent to a merchant's own number (spec 08-whatsapp-embedded-signup
ES-3; Definition of done "Inbound (single receiver)").

The merchant comes only from the stored account row, read after the Meta
signature has verified, through the SELECT-only whatsapp_number_lookup policy.
Celery is eager (root conftest), so the signed POST runs the whole path."""
import hashlib
import hmac
import json
import logging
from unittest.mock import Mock

import pytest
from rest_framework.test import APIClient

from core.tenancy import tenant_atomic, tenant_context
from customers.models import Customer
from whatsapp import tasks
from whatsapp.tests.conftest import load_fixture
from whatsapp.tests.helpers import APP_SECRET, CUSTOMER_PHONE

pytestmark = pytest.mark.django_db

URL = "/api/v1/webhooks/whatsapp"
OWN_PHONE_A = "200000000000001"
OWN_PHONE_B = "200000000000002"


def stop_to(phone_number_id, text="STOP"):
    payload = load_fixture("inbound_text_stop.json")
    value = payload["entry"][0]["changes"][0]["value"]
    value["metadata"]["phone_number_id"] = phone_number_id
    value["messages"][0]["text"]["body"] = text
    return json.dumps(payload).encode()


def post_signed(raw):
    signature = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return APIClient().post(
        URL, data=raw, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=signature
    )


def customer_state(merchant, phone=CUSTOMER_PHONE):
    with tenant_context(merchant.id), tenant_atomic():
        row = Customer.objects.filter(phone=phone).first()
        return None if row is None else (row.opted_out, row.opted_out_at)


@pytest.fixture(autouse=True)
def _settings(meta_settings):
    return meta_settings


@pytest.fixture
def world(make_merchant, make_customer, make_own_account):
    """A and B each own a number and have a customer with the same phone; C
    has a customer with that phone and no own number (it uses the shared pool
    or nothing)."""
    a, b, c = (make_merchant(n).merchant for n in "ABC")
    for merchant in (a, b, c):
        make_customer(merchant, CUSTOMER_PHONE)
    own_a = make_own_account(a, phone_number_id=OWN_PHONE_A)
    own_b = make_own_account(b, phone_number_id=OWN_PHONE_B)
    return {"A": a, "B": b, "C": c, "own_a": own_a, "own_b": own_b}


def test_a_signed_stop_to_a_merchants_own_number_opts_out_only_that_merchant(world):
    resp = post_signed(stop_to(OWN_PHONE_A))

    assert resp.status_code == 200
    assert customer_state(world["A"])[0] is True
    assert customer_state(world["B"]) == (False, None)  # same phone, other merchant's number
    assert customer_state(world["C"]) == (False, None)  # a shared-pool style merchant


@pytest.mark.parametrize("status", ["ACTIVE", "PENDING", "SUSPENDED"])
def test_a_stop_is_honoured_whatever_the_own_numbers_status(
    make_merchant, make_customer, make_own_account, status
):
    merchant = make_merchant("A").merchant
    make_customer(merchant, CUSTOMER_PHONE)
    make_own_account(merchant, status=status, phone_number_id=OWN_PHONE_A)

    assert post_signed(stop_to(OWN_PHONE_A)).status_code == 200

    assert customer_state(merchant)[0] is True


def test_a_disconnected_numbers_stop_is_still_honoured(world):
    """Disconnect clears the token and suspends the number, and does not
    unsubscribe the app, so a STOP still arrives and is honoured."""
    from whatsapp import services

    with tenant_context(world["A"].id):
        services.disconnect_own_number(account=world["own_a"], actor=None)

    assert post_signed(stop_to(OWN_PHONE_A)).status_code == 200

    assert customer_state(world["A"])[0] is True


def test_five_duplicate_deliveries_opt_out_once(world):
    raw = stop_to(OWN_PHONE_A)
    post_signed(raw)
    _, first_at = customer_state(world["A"])

    for _ in range(4):
        assert post_signed(raw).status_code == 200

    assert customer_state(world["A"]) == (True, first_at)


def test_an_unknown_number_answers_200_and_changes_nothing(world):
    assert post_signed(stop_to("299999999999999")).status_code == 200

    for name in "ABC":
        assert customer_state(world[name]) == (False, None)


def test_a_non_stop_text_to_an_own_number_changes_nothing(world):
    assert post_signed(stop_to(OWN_PHONE_A, text="hello there")).status_code == 200

    assert customer_state(world["A"]) == (False, None)


def test_the_view_still_runs_zero_queries_for_an_own_number_stop(world, django_assert_num_queries, monkeypatch):
    # Only the broker call is replaced, so this isolates the view: the lookup
    # happens later, in the task, never in the request.
    publish = Mock()
    monkeypatch.setattr(tasks, "publish_opt_out_fan_out", publish)

    with django_assert_num_queries(0):
        resp = post_signed(stop_to(OWN_PHONE_A))

    assert resp.status_code == 200
    publish.assert_called_once()


def test_an_unsigned_delivery_to_an_own_number_is_401_and_does_nothing(world, django_assert_num_queries):
    with django_assert_num_queries(0):
        resp = APIClient().post(URL, data=stop_to(OWN_PHONE_A), content_type="application/json")

    assert resp.status_code == 401
    assert customer_state(world["A"]) == (False, None)


def test_the_own_number_branch_opts_out_inline_for_the_owning_merchant_only(world, monkeypatch):
    """Foundation Amendment A-1: inline, no second broker hop."""
    delay = Mock()
    monkeypatch.setattr(tasks.process_inbound_opt_out, "delay", delay)

    tasks.fan_out_inbound_opt_out(OWN_PHONE_A, CUSTOMER_PHONE)

    delay.assert_not_called()
    assert customer_state(world["A"])[0] is True
    assert customer_state(world["B"]) == (False, None)
    assert customer_state(world["C"]) == (False, None)


def test_the_own_number_path_logs_no_phone_number(world, caplog):
    caplog.set_level(logging.DEBUG)

    post_signed(stop_to(OWN_PHONE_A))

    assert "9999990001" not in caplog.text


def test_process_inbound_opt_out_for_a_never_changes_bs_customer(world):
    tasks.process_inbound_opt_out(world["A"].id, CUSTOMER_PHONE)

    assert customer_state(world["A"])[0] is True
    assert customer_state(world["B"]) == (False, None)


def test_the_shared_pool_fan_out_is_unchanged(
    world, shared_account, map_to_shared, django_capture_on_commit_callbacks
):
    """OD-1 (b) stays exactly as it was: C maps a location to the shared
    account, so a STOP to the shared number opts out C and not A or B. The
    shared fan-out enqueues per merchant on commit, so the callbacks are
    captured and executed."""
    from whatsapp.tests.helpers import SHARED_PHONE_NUMBER_ID

    map_to_shared(world["C"])

    with django_capture_on_commit_callbacks(execute=True):
        assert post_signed(stop_to(SHARED_PHONE_NUMBER_ID)).status_code == 200

    assert customer_state(world["C"])[0] is True
    assert customer_state(world["A"]) == (False, None)
    assert customer_state(world["B"]) == (False, None)
