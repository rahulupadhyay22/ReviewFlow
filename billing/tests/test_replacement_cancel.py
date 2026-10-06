"""W6 of 07-plan-change-replacement: cancelling a pending replacement.

`POST /billing/replacement/cancel` (OWNER only; 200 idempotent, 409
replacement_activating for an `active` replacement, 409 replacement_committed for a
committed downgrade) and the replacement-aware merchant cancel
(`POST /billing/subscription/cancel`): a non-active replacement is cancelled first
(502 and nothing changed on a failure), an `active` one is never cancelled (409), a
committed downgrade replacement is cancelled too, then the normal cancel runs.

Razorpay is only the FakeProvider. GET /billing/subscription makes no provider call and
exposes no ref."""
import logging

import pytest
from django.utils import timezone as dj_timezone
from rest_framework.test import APIClient

from auditlog.models import AuditLog
from billing.exceptions import BillingProviderRejected, BillingProviderUnavailable
from billing.models import Subscription
from billing.tests.replacement_helpers import REPL_REF, provide_replacement, set_replacement, set_retired
from billing.tests.snapshot_helpers import START, entity
from billing.tests.state_helpers import read_state
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

CANCEL_REPLACEMENT = "/api/v1/billing/replacement/cancel"
CANCEL = "/api/v1/billing/subscription/cancel"
SUBSCRIPTION = "/api/v1/billing/subscription"
ABANDONED = "billing.replacement_abandoned"


@pytest.fixture
def w(lifecycle_setup, session_client):
    """A (OWNER session) is ACTIVE on the small plan with an upgrade replacement to
    the big plan that the provider reports `authenticated`."""
    w = lifecycle_setup
    w.client = session_client(w.a.user.email)
    w.sub(w.a, "ACTIVE", plan=w.small)
    set_replacement(w.a.merchant, plan=w.big)
    provide_replacement(w.provider, w.big, status="authenticated", paid=False)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START)
    return w


def post(client, path):
    return client.post(path, {}, format="json", HTTP_X_CSRFTOKEN=client.csrf)


def row(w, owner=None):
    with tenant_context((owner or w.a).merchant.id), tenant_atomic():
        return Subscription.objects.select_related("plan", "pending_plan").get()


def state(w, owner=None):
    """read_state without the reconcile stamp (a merchant cancel first fetches and
    re-applies the main snapshot, which restamps provider_synced_at/provider_status)."""
    snapshot = read_state((owner or w.a).merchant)
    snapshot.provider_synced_at = snapshot.provider_status = None
    return snapshot


def cancels(w):
    return [c for c in w.provider.calls if c[0] == "cancel_subscription"]


def committed_downgrade(w):
    """A is on the big plan with a committed downgrade replacement (old cancel confirmed)."""
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(
            plan=w.big,
            replacement_plan=w.small,
            pending_plan=w.small,
            replacement_committed_at=dj_timezone.now(),
            replacement_cancel_confirmed_at=dj_timezone.now(),
        )
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="active", start=START)
    provide_replacement(w.provider, w.small, status="authenticated", paid=False)


# --- POST /billing/replacement/cancel ------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["created", "authenticated"])
def test_the_owner_cancels_a_pending_replacement(w, status):
    provide_replacement(w.provider, w.big, status=status, paid=False)
    before = state(w)
    resp = post(w.client, CANCEL_REPLACEMENT)

    assert resp.status_code == 200, resp.content
    r = row(w)
    assert (r.replacement_provider_ref, r.replacement_plan_id, r.replacement_expires_at) == (None, None, None)
    assert (r.retired_provider_ref, r.retired_kind) == (REPL_REF, "ABANDONED_REPLACEMENT")
    assert cancels(w) == [("cancel_subscription", REPL_REF, False)]  # the provider subscription is cancelled
    # the existing plan, entitlement and the old subscription are untouched
    after = state(w)
    assert (after.status, after.period, r.plan_id, r.payment_provider_ref) == (
        before.status,
        before.period,
        w.small.pk,
        w.ref(w.a),
    )
    assert after.audits == [ABANDONED]
    with tenant_context(w.a.merchant.id), tenant_atomic():
        audit = AuditLog.objects.get(action=ABANDONED)
    assert audit.actor_user_id == w.a.user.id
    assert audit.metadata_json == {"reason": "merchant_cancelled", "plan_name": w.big.name}
    assert REPL_REF not in resp.content.decode()  # the body shows no ref


def test_a_repeat_is_an_idempotent_200_with_no_provider_call(w):
    assert post(w.client, CANCEL_REPLACEMENT).status_code == 200
    calls, audits = len(w.provider.calls), state(w).audits
    again = post(w.client, CANCEL_REPLACEMENT)
    assert again.status_code == 200
    assert len(w.provider.calls) == calls and state(w).audits == audits


def test_with_nothing_pending_it_is_a_200_no_op(w):
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(
            replacement_provider_ref=None, replacement_plan=None, replacement_expires_at=None
        )
    w.provider.calls.clear()
    assert post(w.client, CANCEL_REPLACEMENT).status_code == 200
    assert w.provider.calls == []


def test_an_active_replacement_is_409_and_never_cancelled(w):
    provide_replacement(w.provider, w.big, status="active", paid=False)
    before = state(w)
    resp = post(w.client, CANCEL_REPLACEMENT)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "replacement_activating"
    assert state(w) == before and cancels(w) == []


def test_a_committed_downgrade_cannot_be_abandoned_to_keep_the_old_plan(w):
    committed_downgrade(w)
    before = state(w)
    resp = post(w.client, CANCEL_REPLACEMENT)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "replacement_committed"
    assert state(w) == before and cancels(w) == []
    assert row(w).replacement_provider_ref == REPL_REF  # nothing un-cancels the old subscription


@pytest.mark.parametrize(
    "error", [BillingProviderUnavailable(), BillingProviderRejected("BAD_REQUEST_ERROR", status=400)]
)
def test_a_failed_provider_cancel_is_502_and_nothing_changes(w, error):
    w.provider.cancel_errors[REPL_REF] = error
    before = state(w)
    resp = post(w.client, CANCEL_REPLACEMENT)
    assert resp.status_code == 502
    assert state(w) == before and row(w).replacement_provider_ref == REPL_REF


def test_an_unreadable_replacement_is_502_and_nothing_changes(w):
    w.provider.fetch_errors[REPL_REF] = BillingProviderUnavailable()
    before = state(w)
    resp = post(w.client, CANCEL_REPLACEMENT)
    assert resp.status_code == 502 and state(w) == before and cancels(w) == []


@pytest.mark.parametrize("status", ["pending", "halted", "cancelled", "expired"])
def test_a_failed_replacement_is_retired_locally_with_no_provider_cancel(w, status):
    provide_replacement(w.provider, w.big, status=status, paid=False)
    assert post(w.client, CANCEL_REPLACEMENT).status_code == 200
    assert cancels(w) == []
    assert row(w).retired_provider_ref == REPL_REF


@pytest.mark.parametrize("status", ["paused", "completed", "something_new"])
def test_a_status_reviewflow_never_touches_is_409_provider_state_unsupported(w, status):
    provide_replacement(w.provider, w.big, status=status, paid=False)
    before = state(w)
    resp = post(w.client, CANCEL_REPLACEMENT)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "subscription_provider_state_unsupported"
    assert state(w) == before and cancels(w) == []


def test_an_occupied_retired_slot_is_never_overwritten(w):
    set_retired(w.a.merchant)
    before = state(w)
    resp = post(w.client, CANCEL_REPLACEMENT)
    assert resp.status_code == 409 and state(w) == before


# --- permissions, CSRF, API keys, tenant isolation --------------------------------------------------------------


@pytest.mark.parametrize("role", ["ADMIN", "MANAGER", "VIEWER"])
def test_only_the_owner_can_cancel_a_replacement(w, add_member, session_client, role):
    member = add_member(w.a.merchant, role, f"{role.lower()}@example.com")
    client = session_client(member.user.email)
    before = state(w)
    assert post(client, CANCEL_REPLACEMENT).status_code == 403
    assert state(w) == before and w.provider.calls == []


def test_an_api_key_a_missing_csrf_token_and_no_session_are_refused(w, make_key):
    _, raw = make_key(w.a)
    keyed = APIClient(enforce_csrf_checks=True)
    assert keyed.post(CANCEL_REPLACEMENT, {}, format="json", HTTP_AUTHORIZATION=f"Bearer {raw}").status_code == 403
    assert w.client.post(CANCEL_REPLACEMENT, {}, format="json").status_code == 403  # no CSRF token
    assert APIClient(enforce_csrf_checks=True).post(CANCEL_REPLACEMENT, {}, format="json").status_code in (401, 403)
    assert w.provider.calls == [] and row(w).replacement_provider_ref == REPL_REF


def test_merchant_b_cannot_cancel_a_replacement_of_a(w, session_client):
    w.sub(w.b, "ACTIVE", plan=w.small)
    b_before = state(w, w.b)
    b_client = session_client(w.b.user.email)
    assert post(b_client, CANCEL_REPLACEMENT).status_code == 200  # B has none: a no-op
    assert state(w, w.b) == b_before
    assert row(w).replacement_provider_ref == REPL_REF and cancels(w) == []


def test_the_body_is_ignored(w):
    resp = w.client.post(
        CANCEL_REPLACEMENT, {"merchant_id": str(w.b.merchant.id), "ref": "sub_x"}, format="json",
        HTTP_X_CSRFTOKEN=w.client.csrf,
    )
    assert resp.status_code == 200 and cancels(w) == [("cancel_subscription", REPL_REF, False)]


# --- merchant cancel of the subscription ---------------------------------------------------------------------------


def test_a_merchant_cancel_cancels_a_non_active_replacement_first_then_runs_the_normal_cancel(w):
    resp = post(w.client, CANCEL)
    assert resp.status_code == 200, resp.content
    r = row(w)
    assert r.cancel_at_period_end is True  # the normal cancel ran
    assert (r.replacement_provider_ref, r.retired_provider_ref) == (None, REPL_REF)
    assert cancels(w) == [
        ("cancel_subscription", REPL_REF, False),  # the replacement first
        ("cancel_subscription", w.ref(w.a), True),  # then the subscription, at its cycle end
    ]
    assert state(w).audits == [ABANDONED, "billing.cancellation_requested"]


def test_a_failed_replacement_cancel_is_502_and_the_subscription_cancel_does_not_run(w):
    w.provider.cancel_errors[REPL_REF] = BillingProviderUnavailable()
    before = state(w)
    resp = post(w.client, CANCEL)
    assert resp.status_code == 502
    assert state(w) == before and row(w).cancel_at_period_end is False
    assert cancels(w) == [("cancel_subscription", REPL_REF, False)]  # the old subscription is untouched


def test_an_active_replacement_makes_the_merchant_cancel_409(w):
    provide_replacement(w.provider, w.big, status="active", paid=False)
    before = state(w)
    resp = post(w.client, CANCEL)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "replacement_activating"
    assert state(w) == before and cancels(w) == [] and row(w).cancel_at_period_end is False


def test_a_committed_downgrade_replacement_is_cancelled_too_then_the_normal_cancel_runs(w):
    committed_downgrade(w)
    resp = post(w.client, CANCEL)
    assert resp.status_code == 200, resp.content
    r = row(w)
    assert (r.replacement_provider_ref, r.replacement_committed_at, r.replacement_cancel_confirmed_at) == (
        None,
        None,
        None,
    )
    assert (r.retired_provider_ref, r.retired_kind, r.cancel_at_period_end) == (REPL_REF, "ABANDONED_REPLACEMENT", True)
    assert r.pending_plan_id is None  # the downgrade it set is gone
    assert cancels(w)[0] == ("cancel_subscription", REPL_REF, False)
    assert cancels(w)[-1] == ("cancel_subscription", w.ref(w.a), True)


def test_a_past_due_row_cancels_its_non_active_replacement_then_cancels_at_once(w):
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(
            status="PAST_DUE", past_due_at=dj_timezone.now(), dunning_stage=0, provider_status="halted"
        )
    resp = post(w.client, CANCEL)
    assert resp.status_code == 200
    r = row(w)
    assert (r.status, r.replacement_provider_ref, r.retired_provider_ref) == ("CANCELLED", None, REPL_REF)
    assert cancels(w)[0] == ("cancel_subscription", REPL_REF, False)


def test_a_past_due_row_with_an_active_replacement_is_409(w):
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(
            status="PAST_DUE", past_due_at=dj_timezone.now(), dunning_stage=0, provider_status="halted"
        )
    provide_replacement(w.provider, w.big, status="active", paid=False)
    before = state(w)
    resp = post(w.client, CANCEL)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "replacement_activating"
    assert state(w) == before and cancels(w) == []


def test_without_a_replacement_the_merchant_cancel_is_unchanged(w):
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(
            replacement_provider_ref=None, replacement_plan=None, replacement_expires_at=None
        )
    assert post(w.client, CANCEL).status_code == 200
    assert cancels(w) == [("cancel_subscription", w.ref(w.a), True)]


def test_an_already_cancelling_subscription_with_a_replacement_still_cancels_it_first(w):
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(cancel_at_period_end=True)
    assert post(w.client, CANCEL).status_code == 200
    assert row(w).replacement_provider_ref is None and cancels(w) == [("cancel_subscription", REPL_REF, False)]


# --- GET: no provider call, no refs ---------------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["pending", "committed", "retired"])
def test_get_makes_no_provider_call_and_exposes_no_ref(w, shape):
    if shape == "committed":
        committed_downgrade(w)
    if shape == "retired":
        set_retired(w.a.merchant)
    w.provider.calls.clear()
    resp = w.client.get(SUBSCRIPTION)
    assert resp.status_code == 200
    assert w.provider.calls == []
    text = resp.content.decode()
    assert REPL_REF not in text and w.ref(w.a) not in text and "sub_test_retired" not in text


def test_nothing_is_logged_with_a_ref(w, caplog):
    w.provider.cancel_errors[REPL_REF] = BillingProviderUnavailable()
    with caplog.at_level(logging.DEBUG):
        post(w.client, CANCEL_REPLACEMENT)
        del w.provider.cancel_errors[REPL_REF]
        post(w.client, CANCEL_REPLACEMENT)
    assert REPL_REF not in caplog.text and w.ref(w.a) not in caplog.text
