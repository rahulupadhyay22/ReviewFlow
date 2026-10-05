"""D1 fail-closed on the request paths (spec 07 "Open: invoice retrieval (D1)
- BLOCKER", paths C2, C3, C4 and K1). billing.razorpay.fetch_invoices is the
REAL function here: an explicit NotImplementedError stop, caught nowhere. Each
path must raise it and fabricate nothing locally: no status, plan or period
change, no PaymentAttempt, no UsageRecord, no audit row.

C1 (INCOMPLETE checkout reconcile) is in test_api.py; the sync and maintenance
paths are in test_tasks.py, test_webhook.py and test_webhook_sync_gate.py.

These tests verify ReviewFlow's local handling of unavailable invoice data.
They do NOT verify anything about Razorpay's real invoice API."""
from datetime import timedelta

import pytest

from billing import razorpay, services
from billing.tests.snapshot_helpers import END, REF, START, entity, ts
from billing.tests.state_helpers import read_state
from billing.tests.webhook_helpers import ORIGINAL_FETCH_INVOICES
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(lifecycle_setup, monkeypatch):
    monkeypatch.setattr(razorpay, "fetch_invoices", ORIGINAL_FETCH_INVOICES)
    return lifecycle_setup


def as_owner(w, fn, **kwargs):
    """A service call inside A's request-style transaction."""
    owner = w.a
    with tenant_context(owner.merchant.id), tenant_atomic():
        return fn(actor=owner, **kwargs)


def provider_writes(w):
    return [c for c in w.provider.calls if c[0] in ("create_subscription", "update_subscription", "cancel_subscription")]


def test_c2_checkout_on_an_expired_row_whose_provider_is_active_stops_and_creates_nothing(w):
    w.sub(w.a, "EXPIRED", provider_status="halted")
    w.provider.entities[REF] = entity(w.small, status="active", start=END)  # a new provider period
    before = read_state(w.a.merchant)
    with pytest.raises(NotImplementedError):
        as_owner(w, services.start_checkout, plan_id=w.small.id)
    assert read_state(w.a.merchant) == before
    assert provider_writes(w) == []  # no replacement subscription created


def test_c3_checkout_reconcile_of_an_active_row_with_a_different_provider_period_stops(w):
    w.sub(w.a, "ACTIVE")
    w.provider.entities[REF] = entity(w.small, status="active", start=END)
    before = read_state(w.a.merchant)
    with pytest.raises(NotImplementedError):
        as_owner(w, services.start_checkout, plan_id=w.big.id)
    assert read_state(w.a.merchant) == before
    assert provider_writes(w) == []  # the upgrade never reached Razorpay


def test_c4_an_upgrade_whose_update_moves_the_period_claims_nothing_locally(w):
    """The documented C4 window: Razorpay's update has already happened, but
    ReviewFlow cannot verify the invoice, so the request fails and the plan,
    period and entitlement stay as they were."""
    w.sub(w.a, "ACTIVE")
    w.provider.entities[REF] = entity(w.small, status="active", start=START)  # same period: no invoices needed yet
    w.provider.update_extra = {"current_start": ts(END), "current_end": ts(END + timedelta(days=30))}
    before = read_state(w.a.merchant)
    with pytest.raises(NotImplementedError):
        as_owner(w, services.start_checkout, plan_id=w.big.id)
    after = read_state(w.a.merchant)
    assert after == before  # plan, period, payments, usage, audits unchanged
    assert ("update_subscription", REF, "plan_big", "now") in w.provider.calls


def test_k1_cancel_reconcile_of_an_active_row_with_a_different_provider_period_stops(w):
    w.sub(w.a, "ACTIVE")
    w.provider.entities[REF] = entity(w.small, status="active", start=END)
    before = read_state(w.a.merchant)
    with pytest.raises(NotImplementedError):
        as_owner(w, services.cancel_subscription)
    after = read_state(w.a.merchant)
    assert after == before and after.cancel_at_period_end is False
    assert provider_writes(w) == []  # no cancel sent to Razorpay
