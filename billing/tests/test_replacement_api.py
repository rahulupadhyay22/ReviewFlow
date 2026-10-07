"""W5 of 07-plan-change-replacement: POST /billing/checkout falling back to a
plan-change replacement (the replacement checkout), end to end through the API.

Razorpay is the FakeProvider. The classifier set and the G-2 verification field
ship empty/None, so each test that needs a classified refusal or a downgrade
supplies a clearly SYNTHETIC value by monkeypatch: no real Razorpay refusal or
field is known and none is asserted. Nothing here claims any Razorpay behavior;
D1 is untouched (the replacement is only created here, never proven paid)."""
from datetime import timedelta

import pytest
from django.utils import timezone as dj_timezone

from auditlog.models import AuditLog
from billing import razorpay, services
from billing.exceptions import BillingProviderRejected, BillingProviderUnavailable
from billing.models import Subscription
from billing.tests.replacement_helpers import REPL_REF, provide_replacement, set_replacement, set_retired
from billing.tests.snapshot_helpers import END, START, entity
from billing.tests.state_helpers import read_state
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

CHECKOUT = "/api/v1/billing/checkout"
SUBSCRIPTION = "/api/v1/billing/subscription"
SYNTHETIC = (400, "SYNTHETIC_CODE", "synthetic_reason")
WINDOW = 3600  # seconds


@pytest.fixture
def w(lifecycle_setup, session_client, settings, monkeypatch):
    """A (OWNER session) is ACTIVE on the small plan; the provider refuses the
    update with the synthetic classified refusal; both kinds are enabled."""
    w = lifecycle_setup
    w.client = session_client(w.a.user.email)
    w.sub(w.a, "ACTIVE", plan=w.small)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START)
    monkeypatch.setattr(razorpay, "UPDATE_UNSUPPORTED_REFUSALS", frozenset({SYNTHETIC}))
    monkeypatch.setattr(razorpay, "DOWNGRADE_START_FIELD", "synthetic_field")
    w.provider.update_error = BillingProviderRejected("SYNTHETIC_CODE", status=400, reason="synthetic_reason")
    settings.BILLING_REPLACEMENT_UPGRADE_ENABLED = True
    settings.BILLING_REPLACEMENT_DOWNGRADE_ENABLED = True
    settings.BILLING_REPLACEMENT_UPGRADE_AUTH_WINDOW = WINDOW
    settings.BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN = WINDOW
    return w


def state(w, owner=None):
    """read_state without the reconcile stamp: every checkout first fetches and
    re-applies the main snapshot (merged behavior), which restamps
    provider_synced_at and provider_status and changes nothing else."""
    snapshot = read_state((owner or w.a).merchant)
    snapshot.provider_synced_at = snapshot.provider_status = None
    return snapshot


def post(w, plan, **body):
    client = w.client
    return client.post(
        CHECKOUT, {"plan_id": str(plan.id), **body}, format="json", HTTP_X_CSRFTOKEN=client.csrf
    )


def sub_row(w, owner=None):
    with tenant_context((owner or w.a).merchant.id), tenant_atomic():
        return Subscription.objects.select_related("plan", "pending_plan", "replacement_plan").get()


def provider_writes(w):
    return [c for c in w.provider.calls if c[0] in ("create_subscription", "update_subscription", "cancel_subscription")]


def creates(w):
    return [c for c in w.provider.calls if c[0] == "create_subscription"]


def audit_rows(w, action):
    with tenant_context(w.a.merchant.id), tenant_atomic():
        return list(AuditLog.objects.filter(action=action).order_by("created_at"))


def to_downgrade(w):
    """A is on the big plan; the request will be for the small one."""
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(plan=w.big)
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="active", start=START)


# --- upgrade ------------------------------------------------------------------------------------


def test_an_upgrade_the_provider_cannot_update_creates_a_replacement(w):
    before = state(w)
    resp = post(w, w.big, acknowledge_no_credit=True)

    assert resp.status_code == 201, resp.content
    body = resp.json()
    new_ref = w.provider.entities and next(r for r in w.provider.entities if r.startswith("sub_created"))
    assert body["checkout"] == {
        "provider": "razorpay",
        "key_id": "rzp_test_keyid",
        "subscription_id": new_ref,
    }
    row = sub_row(w)
    assert (row.replacement_provider_ref, row.replacement_plan_id) == (new_ref, w.big.pk)
    assert row.replacement_committed_at is None and row.replacement_cancel_confirmed_at is None
    assert row.pending_plan_id is None  # an upgrade is not a scheduled downgrade
    # the existing plan, entitlement and status are exactly as they were
    after = state(w)
    assert (after.status, after.period, after.payments, after.usages) == (
        before.status,
        before.period,
        before.payments,
        before.usages,
    )
    assert row.plan_id == w.small.pk and row.payment_provider_ref == w.ref(w.a)
    assert after.audits == ["billing.replacement_started"]
    (started,) = audit_rows(w, "billing.replacement_started")
    assert started.metadata_json == {
        "kind": "UPGRADE",
        "from_plan": w.small.name,
        "to_plan": w.big.name,
        "plan_id": str(w.big.pk),
    }
    assert started.actor_user_id == w.a.user.id
    assert body["subscription"]["plan"]["id"] == str(w.small.pk)  # still the old plan


def test_the_provider_update_is_tried_first_then_one_create_that_starts_now_with_a_deadline(w):
    t0 = dj_timezone.now()
    assert post(w, w.big, acknowledge_no_credit=True).status_code == 201
    names = [c[0] for c in provider_writes(w)]
    assert names == ["update_subscription", "create_subscription"]  # C3: update first
    assert w.provider.calls[-1][0] == "create_subscription"
    (kwargs,) = w.provider.created_kwargs
    assert kwargs["start_at"] is None  # an upgrade starts immediately
    assert t0.timestamp() + WINDOW - 5 <= kwargs["expire_by"] <= dj_timezone.now().timestamp() + WINDOW + 5
    row = sub_row(w)
    assert int(row.replacement_expires_at.timestamp()) == kwargs["expire_by"]  # the window is in SECONDS
    assert ("cancel_subscription", w.ref(w.a), False) not in w.provider.calls
    assert not [c for c in w.provider.calls if c[0] == "cancel_subscription"]  # old subscription untouched


@pytest.mark.parametrize("body", [{}, {"acknowledge_no_credit": False}])
def test_an_upgrade_replacement_without_the_acknowledgement_is_422_and_changes_nothing(w, body):
    before = state(w)
    resp = post(w, w.big, **body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "no_credit_acknowledgement_required"
    assert state(w) == before
    assert creates(w) == []  # checked before any create call
    assert sub_row(w).replacement_provider_ref is None


def test_the_client_resends_with_true_after_the_422(w):
    assert post(w, w.big).status_code == 422
    assert post(w, w.big, acknowledge_no_credit=True).status_code == 201
    assert len(creates(w)) == 1


def test_a_successful_provider_update_ignores_and_never_records_the_acknowledgement(w):
    w.provider.update_error = None  # Razorpay can update this subscription
    resp = post(w, w.big, acknowledge_no_credit=True)
    assert resp.status_code == 200
    assert creates(w) == []
    row = sub_row(w)
    assert row.replacement_provider_ref is None and row.replacement_plan_id is None
    for audit in audit_rows(w, "billing.plan_changed"):
        assert "acknowledged_no_credit" not in audit.metadata_json
    assert audit_rows(w, "billing.replacement_started") == []


# --- downgrade ----------------------------------------------------------------------------------


def test_a_downgrade_replacement_starts_at_the_period_end_and_needs_no_acknowledgement(w):
    to_downgrade(w)
    resp = post(w, w.small)  # no acknowledgement
    assert resp.status_code == 201, resp.content
    (kwargs,) = w.provider.created_kwargs
    assert kwargs["start_at"] == int(END.timestamp())  # C2: the next period, not now
    cutoff = int(END.timestamp()) - WINDOW  # old period end minus the configured margin
    assert kwargs["expire_by"] == cutoff - 1  # strictly before the cutoff, by exactly 1 second
    row = sub_row(w)
    assert row.replacement_plan_id == w.small.pk and row.pending_plan_id == w.small.pk
    assert row.plan_id == w.big.pk  # the old plan stays in force until the switch
    assert row.replacement_committed_at is None  # only the commit step (W6) sets it
    assert int(row.replacement_expires_at.timestamp()) == kwargs["expire_by"]
    (started,) = audit_rows(w, "billing.replacement_started")
    assert started.metadata_json["kind"] == "DOWNGRADE"
    assert [c[0] for c in provider_writes(w)] == ["update_subscription", "create_subscription"]
    assert not [c for c in w.provider.calls if c[0] == "cancel_subscription"]  # never scheduled at creation


def test_a_downgrade_with_the_verification_field_unrecorded_is_not_created(w, monkeypatch):
    """G-2: until the field is recorded no payer is asked to authorize a downgrade."""
    to_downgrade(w)
    monkeypatch.setattr(razorpay, "DOWNGRADE_START_FIELD", None)
    before = state(w)
    resp = post(w, w.small)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "plan_change_unsupported"
    assert state(w) == before and creates(w) == []


def test_a_downgrade_past_the_commit_cutoff_is_not_created(w, settings):
    to_downgrade(w)
    remaining = int((END - dj_timezone.now()).total_seconds())
    settings.BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN = remaining + 60  # the cutoff has passed
    before = state(w)
    resp = post(w, w.small)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "plan_change_unsupported"
    assert state(w) == before and creates(w) == []


# --- flags, classifier, windows ---------------------------------------------------------------------


def test_with_both_flags_off_the_merged_409_stands(w, settings):
    settings.BILLING_REPLACEMENT_UPGRADE_ENABLED = False
    before = state(w)
    resp = post(w, w.big, acknowledge_no_credit=True)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "plan_change_unsupported"
    assert state(w) == before and creates(w) == []


def test_the_shipped_empty_classifier_set_classifies_nothing_even_with_the_flags_on(w, monkeypatch):
    monkeypatch.setattr(razorpay, "UPDATE_UNSUPPORTED_REFUSALS", frozenset())
    resp = post(w, w.big, acknowledge_no_credit=True)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "plan_change_unsupported"
    assert creates(w) == []


@pytest.mark.parametrize(
    "error",
    [
        BillingProviderRejected("SYNTHETIC_CODE", status=422, reason="synthetic_reason"),
        BillingProviderRejected("OTHER", status=400, reason="synthetic_reason"),
        BillingProviderRejected("SYNTHETIC_CODE", status=400, reason="other_reason"),
        BillingProviderRejected("SYNTHETIC_CODE", status=400),
        BillingProviderRejected(),
    ],
)
def test_an_unclassified_refusal_never_creates_a_replacement(w, error):
    w.provider.update_error = error
    resp = post(w, w.big, acknowledge_no_credit=True)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "plan_change_unsupported"
    assert creates(w) == []


def test_a_provider_outage_on_the_update_is_502_and_never_a_replacement(w):
    w.provider.update_error = BillingProviderUnavailable()
    resp = post(w, w.big, acknowledge_no_credit=True)
    assert resp.status_code == 502 and creates(w) == []


def test_the_flags_are_independent(w, settings):
    settings.BILLING_REPLACEMENT_DOWNGRADE_ENABLED = False
    assert post(w, w.big, acknowledge_no_credit=True).status_code == 201  # upgrade on, downgrade off


def test_the_downgrade_flag_alone_does_not_enable_upgrades(w, settings):
    settings.BILLING_REPLACEMENT_UPGRADE_ENABLED = False
    assert post(w, w.big, acknowledge_no_credit=True).status_code == 409
    to_downgrade(w)
    assert post(w, w.small).status_code == 201  # downgrade on, upgrade off


@pytest.mark.parametrize("value", [None, 0, -5, "3600", 1.5, True])
def test_an_unset_or_invalid_window_is_503_billing_not_configured(w, settings, value):
    settings.BILLING_REPLACEMENT_UPGRADE_AUTH_WINDOW = value
    before = state(w)
    resp = post(w, w.big, acknowledge_no_credit=True)
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "billing_not_configured"
    assert state(w) == before and creates(w) == []


def test_an_unset_downgrade_margin_is_503_for_that_kind(w, settings):
    to_downgrade(w)
    settings.BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN = None
    resp = post(w, w.small)
    assert resp.status_code == 503 and creates(w) == []


# --- pending replacement short-circuit --------------------------------------------------------------


def test_a_repeat_of_the_same_plan_returns_the_existing_checkout_with_no_provider_call(w):
    first = post(w, w.big, acknowledge_no_credit=True)
    calls_before, audits_before = len(w.provider.calls), read_state(w.a.merchant).audits
    again = post(w, w.big)  # no acknowledgement needed to see it again
    assert again.status_code == 200
    assert again.json()["checkout"] == first.json()["checkout"]
    assert len(w.provider.calls) == calls_before  # no fetch, no update, no create
    assert read_state(w.a.merchant).audits == audits_before  # no audit row


@pytest.mark.parametrize("which", ["current", "other", "same_price"])
def test_any_other_plan_while_one_is_pending_is_409_replacement_in_progress(w, make_plan, which):
    assert post(w, w.big, acknowledge_no_credit=True).status_code == 201
    other = {"current": w.small, "other": make_plan("Scale", price="2999.00"), "same_price": make_plan("Alt")}[which]
    calls_before = len(w.provider.calls)
    resp = post(w, other, acknowledge_no_credit=True)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "replacement_in_progress"
    assert len(w.provider.calls) == calls_before
    assert len(creates(w)) == 1  # at most two provider subscriptions


def test_a_downgrade_replacement_repeat_returns_its_checkout_not_the_empty_pending_plan_answer(w):
    to_downgrade(w)
    first = post(w, w.small)
    again = post(w, w.small)
    assert again.status_code == 200 and again.json()["checkout"] == first.json()["checkout"]
    assert len(creates(w)) == 1


def test_the_past_due_answer_comes_before_the_replacement_one(w):
    set_replacement(w.a.merchant, plan=w.big)
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(
            status="PAST_DUE", past_due_at=dj_timezone.now(), dunning_stage=0, provider_status="halted"
        )
    resp = post(w, w.big)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "subscription_past_due"


def test_an_occupied_retired_slot_blocks_a_new_replacement_even_when_acknowledged(w):
    set_retired(w.a.merchant)
    before = state(w)
    resp = post(w, w.big, acknowledge_no_credit=True)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "replacement_in_progress"
    assert state(w) == before and creates(w) == []


def test_with_a_retired_ref_a_provider_update_checkout_still_works(w):
    """The retired slot blocks only the fallback path."""
    w.provider.update_error = None
    set_retired(w.a.merchant)
    assert post(w, w.big).status_code == 200


# --- create failures leave nothing behind -------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        BillingProviderUnavailable(),
        BillingProviderRejected("BAD_REQUEST_ERROR", status=400),
        {"status": "created"},  # no id
        {"id": "sub bad id!", "status": "created"},  # an unusable id is never stored
        {"id": "x" * 65, "status": "created"},
        {"id": 12345},
    ],
)
def test_a_failed_or_malformed_create_is_502_with_nothing_stored(w, failure):
    if isinstance(failure, Exception):
        w.provider.create_error = failure
    else:
        w.provider.create_entity = failure
    before = state(w)
    resp = post(w, w.big, acknowledge_no_credit=True)
    assert resp.status_code == 502
    assert state(w) == before
    assert sub_row(w).replacement_plan_id is None
    assert audit_rows(w, "billing.replacement_started") == []


def test_a_create_returning_the_current_subscriptions_own_id_is_never_stored(w):
    w.provider.create_entity = {"id": w.ref(w.a), "status": "created"}
    resp = post(w, w.big, acknowledge_no_credit=True)
    assert resp.status_code == 502 and sub_row(w).replacement_provider_ref is None


# --- roles, CSRF, API keys, tenant isolation, hygiene ---------------------------------------------------


@pytest.mark.parametrize("role", ["ADMIN", "MANAGER", "VIEWER"])
def test_only_the_owner_can_start_a_replacement(w, add_member, session_client, role):
    member = add_member(w.a.merchant, role, f"{role.lower()}@example.com")
    client = session_client(member.user.email)
    before = state(w)
    resp = client.post(
        CHECKOUT, {"plan_id": str(w.big.id), "acknowledge_no_credit": True}, format="json", HTTP_X_CSRFTOKEN=client.csrf
    )
    assert resp.status_code == 403
    assert state(w) == before and w.provider.calls == []


def test_an_api_key_and_a_missing_csrf_token_are_refused(w, make_key):
    _, raw = make_key(w.a)
    from rest_framework.test import APIClient

    keyed = APIClient(enforce_csrf_checks=True)
    resp = keyed.post(
        CHECKOUT, {"plan_id": str(w.big.id), "acknowledge_no_credit": True}, format="json",
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    assert resp.status_code == 403
    no_csrf = w.client.post(CHECKOUT, {"plan_id": str(w.big.id), "acknowledge_no_credit": True}, format="json")
    assert no_csrf.status_code == 403
    assert creates(w) == [] and sub_row(w).replacement_provider_ref is None


def test_merchant_b_cannot_see_or_affect_a_replacement_of_a(w, session_client):
    assert post(w, w.big, acknowledge_no_credit=True).status_code == 201
    w.sub(w.b, "ACTIVE", plan=w.small)
    b_before = state(w, w.b)
    b_client = session_client(w.b.user.email)
    # B asking for the same plan A is waiting on is just B's own checkout: no
    # short-circuit, no A checkout payload, nothing of A's changed
    w.provider.entities[w.ref(w.b)] = entity(w.small, status="active", start=START, ref=w.ref(w.b))
    resp = b_client.post(
        CHECKOUT, {"plan_id": str(w.big.id), "acknowledge_no_credit": True}, format="json", HTTP_X_CSRFTOKEN=b_client.csrf
    )
    assert resp.status_code == 201
    a_ref = sub_row(w).replacement_provider_ref
    b_ref = sub_row(w, w.b).replacement_provider_ref
    assert a_ref != b_ref and resp.json()["checkout"]["subscription_id"] == b_ref
    assert state(w, w.b) != b_before  # B has its own replacement now
    assert sub_row(w).replacement_provider_ref == a_ref  # A's is untouched


def test_the_replacement_ref_never_appears_in_the_subscription_read_and_nothing_logs_it(w, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        created = post(w, w.big, acknowledge_no_credit=True)
        got = w.client.get(SUBSCRIPTION)
    ref = created.json()["checkout"]["subscription_id"]
    assert got.status_code == 200 and ref not in got.content.decode()
    assert ref not in caplog.text and w.ref(w.a) not in caplog.text
    (started,) = audit_rows(w, "billing.replacement_started")
    assert ref not in str(started.metadata_json)


def test_a_client_supplied_merchant_or_plan_target_is_ignored(w):
    resp = post(w, w.big, acknowledge_no_credit=True, merchant_id=str(w.b.merchant.id), replacement_plan="x")
    assert resp.status_code == 201
    with tenant_context(w.b.merchant.id), tenant_atomic():
        assert not Subscription.objects.exists()


# --- the stored target lines up with what W4 does next -------------------------------------------------------


def test_a_created_replacement_is_switched_by_the_existing_sweep_once_paid(w):
    """Creation (W5) then proof (W4): an authenticated replacement waits; a paid
    active one switches. The target plan recorded at creation is cleared."""
    ref = post(w, w.big, acknowledge_no_credit=True).json()["checkout"]["subscription_id"]
    provide_replacement(w.provider, w.big, status="authenticated", paid=False, ref=ref)
    with tenant_context(w.a.merchant.id):
        services._sync_replacement()
    assert sub_row(w).replacement_provider_ref == ref  # still waiting
    provide_replacement(w.provider, w.big, ref=ref)
    with tenant_context(w.a.merchant.id):
        services._sync_replacement()
    row = sub_row(w)
    assert (row.plan_id, row.payment_provider_ref) == (w.big.pk, ref)
    assert (row.replacement_provider_ref, row.replacement_plan_id) == (None, None)
    assert row.retired_provider_ref == w.ref(w.a)


def test_set_replacement_helper_target_matches_the_short_circuit(w):
    set_replacement(w.a.merchant, plan=w.big)
    resp = post(w, w.big)
    assert resp.status_code == 200 and resp.json()["checkout"]["subscription_id"] == REPL_REF
