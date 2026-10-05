"""D1 stays fail-closed (spec 07 "Open: invoice retrieval (D1)"): the REAL
billing.razorpay.fetch_invoices raises NotImplementedError, caught nowhere, so no
path pretends a reconciliation succeeded. D1 is a KNOWN pre-production gate;
nothing here implements or works around it, and nothing here claims anything
about Razorpay's real invoice API (FakeProvider answers every other call).

Adds what test_d1_fail_closed.py / test_api.py / test_tasks.py do not:
- the same stop reached through the real HTTP views (the exception must
  propagate out of view and error handler, not become a 4xx, 5xx body or 2xx);
- the CANCELLED twin of C2;
- the sync on every local state that needs the paid-invoice proof;
- read-only endpoints never reach the invoice fetch."""
from datetime import timedelta

import pytest

from billing import razorpay, services
from billing.tests.snapshot_helpers import END, REF, START, entity, ts
from billing.tests.state_helpers import read_state
from billing.tests.webhook_helpers import ORIGINAL_FETCH_INVOICES
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

CHECKOUT = "/api/v1/billing/checkout"
CANCEL = "/api/v1/billing/subscription/cancel"
WRITES = ("create_subscription", "update_subscription", "cancel_subscription")


@pytest.fixture
def w(lifecycle_setup, session_client, monkeypatch):
    monkeypatch.setattr(razorpay, "fetch_invoices", ORIGINAL_FETCH_INVOICES)
    lifecycle_setup.client = session_client(lifecycle_setup.a.user.email)
    return lifecycle_setup


def provider_writes(w):
    return [c for c in w.provider.calls if c[0] in WRITES]


def post(w, path, body=None):
    return w.client.post(path, body or {}, format="json", HTTP_X_CSRFTOKEN=w.client.csrf)


# --- through the HTTP views -------------------------------------------------


def test_c1_http_checkout_on_an_incomplete_row_whose_provider_is_active_propagates_the_stop(w):
    w.sub(w.a, "INCOMPLETE")
    w.provider.entities[REF] = entity(w.small, status="active", start=START)
    before = read_state(w.a.merchant)
    with pytest.raises(NotImplementedError):
        post(w, CHECKOUT, {"plan_id": str(w.small.id)})
    assert read_state(w.a.merchant) == before
    assert provider_writes(w) == []


MIRROR_NEUTRAL = {"provider_status": None, "provider_synced_at": None}


@pytest.mark.parametrize("status", ["CANCELLED", "EXPIRED"])
def test_c2_http_checkout_on_an_ended_row_whose_provider_is_active_propagates_the_stop(w, status):
    w.sub(w.a, status, provider_status="halted")
    w.provider.entities[REF] = entity(w.small, status="active", start=END)
    before = read_state(w.a.merchant)
    with pytest.raises(NotImplementedError):
        post(w, CHECKOUT, {"plan_id": str(w.small.id)})
    assert read_state(w.a.merchant) == before
    assert provider_writes(w) == []  # no replacement subscription, no cancel


def test_c3_http_checkout_on_an_active_row_with_a_new_provider_period_propagates_the_stop(w):
    w.sub(w.a, "ACTIVE")
    w.provider.entities[REF] = entity(w.small, status="active", start=END)
    before = read_state(w.a.merchant)
    with pytest.raises(NotImplementedError):
        post(w, CHECKOUT, {"plan_id": str(w.big.id)})
    assert read_state(w.a.merchant) == before
    assert provider_writes(w) == []


def test_c4_http_upgrade_whose_update_moves_the_period_propagates_the_stop_and_claims_nothing(w):
    w.sub(w.a, "ACTIVE")
    w.provider.entities[REF] = entity(w.small, status="active", start=START)
    w.provider.update_extra = {"current_start": ts(END), "current_end": ts(END + timedelta(days=30))}
    before = read_state(w.a.merchant)
    with pytest.raises(NotImplementedError):
        post(w, CHECKOUT, {"plan_id": str(w.big.id)})
    after = read_state(w.a.merchant)
    # Nothing is claimed: plan, period, payments, usage, audit and events are unchanged. Only the
    # provider mirror may move (spec: provider_status is stored on every apply, including "no
    # change" cells): checkout's pre-update reconcile applied the same-period snapshot.
    assert vars(after) | MIRROR_NEUTRAL == vars(before) | MIRROR_NEUTRAL
    assert after.provider_status in (None, "active")
    assert ("update_subscription", REF, "plan_big", "now") in w.provider.calls  # the documented C4 window


def test_k1_http_cancel_on_an_active_row_with_a_new_provider_period_propagates_the_stop(w):
    w.sub(w.a, "ACTIVE")
    w.provider.entities[REF] = entity(w.small, status="active", start=END)
    before = read_state(w.a.merchant)
    with pytest.raises(NotImplementedError):
        post(w, CANCEL)
    after = read_state(w.a.merchant)
    assert after == before and after.cancel_at_period_end is False
    assert provider_writes(w) == []


# --- the service path for the CANCELLED twin of C2 ----------------------------


def test_c2_service_checkout_on_a_cancelled_row_whose_provider_is_active_stops_and_creates_nothing(w):
    w.sub(w.a, "CANCELLED", provider_status="halted")
    w.provider.entities[REF] = entity(w.small, status="active", start=END)
    before = read_state(w.a.merchant)
    with tenant_context(w.a.merchant.id), tenant_atomic():
        with pytest.raises(NotImplementedError):
            services.start_checkout(actor=w.a, plan_id=w.small.id)
    assert read_state(w.a.merchant) == before
    assert provider_writes(w) == []


# --- the sync, for every state that needs the paid-invoice proof --------------


@pytest.mark.parametrize(
    ("status", "provider_start"),
    [
        ("INCOMPLETE", START),
        ("PAST_DUE", START),
        ("CANCELLED", START),
        ("EXPIRED", START),
        ("ACTIVE", END),  # a period advance
    ],
)
def test_the_sync_stops_at_the_unimplemented_fetch_for_every_state_that_needs_proof(
    w, status, provider_start
):
    w.sub(w.a, status)
    w.provider.entities[REF] = entity(w.small, status="active", start=provider_start)
    before = read_state(w.a.merchant)
    with tenant_context(w.a.merchant.id):
        with pytest.raises(NotImplementedError):
            services.sync_subscription()
    after = read_state(w.a.merchant)
    assert after == before  # status, period, ledger, usage, audits and events all unchanged
    assert provider_writes(w) == []


# --- read-only endpoints never need the invoice list --------------------------


def test_reading_plans_subscription_and_merchant_never_reaches_the_invoice_fetch(w, monkeypatch):
    def boom(ref):
        raise AssertionError("a read-only endpoint called fetch_invoices")

    monkeypatch.setattr(razorpay, "fetch_invoices", boom)
    w.sub(w.a, "PAST_DUE")
    for path in ("/api/v1/billing/plans", "/api/v1/billing/subscription", "/api/v1/merchant"):
        assert w.client.get(path).status_code == 200, path
    assert w.provider.calls == []


def test_the_real_stop_marker_validates_the_ref_and_raises_for_a_valid_one(settings):
    settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = "rzp_test_keyid", "key_secret_value"
    with pytest.raises(NotImplementedError):
        ORIGINAL_FETCH_INVOICES("sub_ValidRef0001")
    with pytest.raises(ValueError):
        ORIGINAL_FETCH_INVOICES("../not-a-ref")
