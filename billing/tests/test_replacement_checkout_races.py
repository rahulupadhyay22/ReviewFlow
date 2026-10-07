"""07-plan-change-replacement, Definition of done "Concurrency": a double replacement
checkout, and the replacement switch versus a merchant cancel, each end in one
consistent state. Real threads and real transactions; thread_helpers.race makes the
first thread hold the Subscription row lock and observes the second waiting on it
(no sleeps). Razorpay is the FakeProvider; the classifier set and the G-2 field are
SYNTHETIC monkeypatched values (no real Razorpay behavior is claimed). The switch
versus the sweep, the PAST_DUE transition and the commit versus a cancel live in
test_replacement_races.py and test_replacement_commit_concurrency.py."""
import pytest

from billing import razorpay, services
from billing.exceptions import BillingProviderRejected
from billing.tests.replacement_helpers import REPL_REF, provide_replacement, set_replacement
from billing.tests.snapshot_helpers import START, entity
from billing.tests.state_helpers import read_state
from billing.tests.thread_helpers import race
from core.tenancy import tenant_context

pytestmark = pytest.mark.django_db(transaction=True)

CHECKOUT = "/api/v1/billing/checkout"
CANCEL = "/api/v1/billing/subscription/cancel"
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


def test_a_double_replacement_checkout_ends_with_one_replacement_and_one_provider_create(
    w, monkeypatch, session_client
):
    second_client = session_client(w.a.user.email)
    results = []

    def checkout(client):
        def run():
            resp = client.post(
                CHECKOUT,
                {"plan_id": str(w.big.id), "acknowledge_no_credit": True},
                format="json",
                HTTP_X_CSRFTOKEN=client.csrf,
            )
            results.append((resp.status_code, resp.json()["checkout"]))

        return run

    race(monkeypatch, first=checkout(w.client), second=checkout(second_client))

    assert sorted(code for code, _ in results) == [200, 201]  # one created it, the other got it back
    assert results[0][1] == results[1][1]  # the same checkout, one provider subscription
    assert w.provider.count("create_subscription") == 1
    assert w.provider.count("update_subscription") == 1  # the loser made no provider call at all
    state = read_state(w.a.merchant)
    assert state.replacement_provider_ref == results[0][1]["subscription_id"]
    assert state.audits == ["billing.replacement_started"]  # one replacement, one audit row


@pytest.mark.parametrize("order", ["switch_first", "cancel_first"])
def test_the_replacement_switch_versus_a_merchant_cancel_ends_in_one_consistent_state(w, monkeypatch, order):
    set_replacement(w.a.merchant, plan=w.big)
    provide_replacement(w.provider, w.big)  # active and paid: the switch is due
    old_ref = w.ref(w.a)
    initial = read_state(w.a.merchant)
    answers = []

    def switch():
        with tenant_context(w.a.merchant.id):
            services._sync_replacement()

    def cancel():
        resp = w.client.post(CANCEL, {}, format="json", HTTP_X_CSRFTOKEN=w.client.csrf)
        answers.append((resp.status_code, resp.json().get("error", {}).get("code")))

    first, second = (switch, cancel) if order == "switch_first" else (cancel, switch)
    race(monkeypatch, first=first, second=second)

    state = read_state(w.a.merchant)
    cancels = [c for c in w.provider.calls if c[0] == "cancel_subscription"]
    # whoever wins, the replacement was switched exactly once and nothing half-done is left
    assert state.status == "ACTIVE"
    assert (state.replacement_provider_ref, state.retired_provider_ref, state.retired_kind) == (
        None,
        old_ref,
        "SWITCHED_OLD",
    )
    assert len(state.usages) == len(initial.usages) + 1 and len(state.payments) == 1
    assert state.audits.count("billing.plan_changed") == 1
    if order == "switch_first":
        # the cancel then runs on the new subscription, as a normal period-end cancel
        assert answers == [(200, None)]
        assert state.cancel_at_period_end is True
        assert state.audits == ["billing.plan_changed", "billing.cancellation_requested"]
        assert cancels == [("cancel_subscription", REPL_REF, True)]
    else:
        # the replacement is `active`: the cancel is refused and nothing was cancelled
        assert answers == [(409, "replacement_activating")]
        assert state.cancel_at_period_end is False
        assert state.audits == ["billing.plan_changed"]
        assert cancels == []
