"""07-plan-change-replacement, Definition of done "the billing_write throttle
applies": the replacement checkout path and POST /billing/replacement/cancel.
Per-user, 10/min by default (test_billing_rate_settings.py). The classifier set and
the G-2 field ship empty/None, so the SYNTHETIC values below are monkeypatched; no
real Razorpay refusal is claimed. Razorpay is the FakeProvider."""
import pytest

from billing import razorpay
from billing.exceptions import BillingProviderRejected
from billing.tests.replacement_helpers import REPL_REF, provide_replacement, set_replacement
from billing.tests.snapshot_helpers import START, entity
from billing.tests.state_helpers import read_state

pytestmark = pytest.mark.django_db

CHECKOUT = "/api/v1/billing/checkout"
REPLACEMENT_CANCEL = "/api/v1/billing/replacement/cancel"
SYNTHETIC = (400, "SYNTHETIC_CODE", "synthetic_reason")


@pytest.fixture
def w(lifecycle_setup, session_client, settings, monkeypatch):
    w = lifecycle_setup
    w.client = session_client(w.a.user.email)
    w.sub(w.a, "ACTIVE", plan=w.small)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START)
    monkeypatch.setattr(razorpay, "UPDATE_UNSUPPORTED_REFUSALS", frozenset({SYNTHETIC}))
    w.provider.update_error = BillingProviderRejected("SYNTHETIC_CODE", status=400, reason="synthetic_reason")
    settings.BILLING_REPLACEMENT_UPGRADE_ENABLED = True
    settings.BILLING_REPLACEMENT_UPGRADE_AUTH_WINDOW = 3600
    return w


def post(w, url, body):
    return w.client.post(url, body, format="json", HTTP_X_CSRFTOKEN=w.client.csrf)


def test_the_replacement_checkout_path_is_throttled_at_the_eleventh_request(w):
    body = {"plan_id": str(w.big.id), "acknowledge_no_credit": True}
    codes = [post(w, CHECKOUT, body).status_code for _ in range(11)]
    # the first creates the replacement, the repeats return it (no provider call), the 11th is throttled
    assert codes == [201] + [200] * 9 + [429]
    assert w.provider.count("create_subscription") == 1


def test_a_throttled_replacement_checkout_changes_nothing(w):
    body = {"plan_id": str(w.big.id), "acknowledge_no_credit": True}
    for _ in range(10):
        post(w, CHECKOUT, body)
    before, calls = read_state(w.a.merchant), list(w.provider.calls)
    assert post(w, CHECKOUT, body).status_code == 429
    assert read_state(w.a.merchant) == before and w.provider.calls == calls


def test_the_replacement_cancel_endpoint_is_throttled_at_the_eleventh_request(w):
    set_replacement(w.a.merchant, plan=w.big)
    provide_replacement(w.provider, w.big, status="created", paid=False)
    codes = [post(w, REPLACEMENT_CANCEL, {}).status_code for _ in range(11)]
    assert codes == [200] * 10 + [429]  # idempotent 200s, then throttled
    assert w.provider.calls.count(("cancel_subscription", REPL_REF, False)) == 1  # abandoned once only


def test_the_throttle_is_per_user_so_another_merchants_owner_is_not_throttled(w, session_client):
    set_replacement(w.a.merchant, plan=w.big)
    provide_replacement(w.provider, w.big, status="created", paid=False)
    for _ in range(11):
        post(w, REPLACEMENT_CANCEL, {})
    other = session_client(w.b.user.email)
    resp = other.post(REPLACEMENT_CANCEL, {}, format="json", HTTP_X_CSRFTOKEN=other.csrf)
    assert resp.status_code != 429
