"""W7 of 07-plan-change-replacement: the `replacement` member of
GET /billing/subscription (and of the `subscription` body the POSTs return).

    replacement: { target_plan: {id, name}, kind: "UPGRADE" | "DOWNGRADE",
                   authorized, committed, effective_at } | null

`authorized` is derived as equal to `committed` (no replacement status is
persisted). The read is local state only: no provider call, and neither the
replacement reference nor the retired reference is ever shown. OWNER and ADMIN read
it, as today. `effective_at` is the old period end for a downgrade and null for an
upgrade (an interpretation recorded in the API spec). Razorpay is only the
FakeProvider and is never called."""
import json

import pytest
from django.utils import timezone as dj_timezone

from billing.models import Subscription
from billing.tests.replacement_helpers import REPL_REF, set_replacement, set_retired
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

SUBSCRIPTION = "/api/v1/billing/subscription"
CANCEL_REPLACEMENT = "/api/v1/billing/replacement/cancel"


@pytest.fixture
def w(lifecycle_setup, session_client):
    w = lifecycle_setup
    w.client = session_client(w.a.user.email)
    return w


def get(client):
    return client.get(SUBSCRIPTION)


def period_end_iso(w):
    with tenant_context(w.a.merchant.id), tenant_atomic():
        end = Subscription.objects.get().current_period_end
    return end.isoformat().replace("+00:00", "Z")


def commit(w):
    with tenant_context(w.a.merchant.id), tenant_atomic():
        Subscription.objects.update(replacement_committed_at=dj_timezone.now())


# --- the member ---------------------------------------------------------------------------------


def test_no_replacement_is_null(w):
    w.sub(w.a, "ACTIVE")
    body = get(w.client).json()
    assert "replacement" in body and body["replacement"] is None


def test_no_subscription_is_null(w):
    assert get(w.client).json()["replacement"] is None


def test_a_pending_upgrade_replacement(w):
    w.sub(w.a, "ACTIVE", plan=w.small)
    set_replacement(w.a.merchant, plan=w.big)
    replacement = get(w.client).json()["replacement"]
    assert replacement == {
        "target_plan": {"id": str(w.big.pk), "name": w.big.name},
        "kind": "UPGRADE",
        "authorized": False,
        "committed": False,
        "effective_at": None,  # an upgrade takes effect when it is proven paid
    }


def test_a_pending_downgrade_replacement_takes_effect_at_the_old_period_end(w):
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, downgrade_to=w.small)
    replacement = get(w.client).json()["replacement"]
    assert replacement["kind"] == "DOWNGRADE"
    assert replacement["target_plan"] == {"id": str(w.small.pk), "name": w.small.name}
    assert (replacement["authorized"], replacement["committed"]) == (False, False)
    assert replacement["effective_at"] == period_end_iso(w)


def test_a_committed_downgrade_is_committed_and_authorized(w):
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, downgrade_to=w.small)
    commit(w)
    replacement = get(w.client).json()["replacement"]
    assert (replacement["committed"], replacement["authorized"]) == (True, True)  # authorized == committed
    assert replacement["effective_at"] == period_end_iso(w)  # still the old period end once committed


def test_the_member_never_shows_a_provider_reference_or_the_stored_plan_column(w):
    w.sub(w.a, "ACTIVE", plan=w.small)
    set_replacement(w.a.merchant, plan=w.big)
    set_retired(w.a.merchant)
    text = json.dumps(get(w.client).json())
    assert REPL_REF not in text and w.ref(w.a) not in text and "sub_test_retired" not in text
    assert set(get(w.client).json()["replacement"]) == {
        "target_plan",
        "kind",
        "authorized",
        "committed",
        "effective_at",
    }


def test_a_retired_ref_alone_is_not_a_replacement(w):
    w.sub(w.a, "ACTIVE")
    set_retired(w.a.merchant)
    assert get(w.client).json()["replacement"] is None


def test_the_existing_members_are_unchanged_beside_it(w):
    w.sub(w.a, "ACTIVE", plan=w.small)
    set_replacement(w.a.merchant, plan=w.big)
    body = get(w.client).json()
    assert body["status"] == "ACTIVE" and body["plan"]["id"] == str(w.small.pk)  # the old plan is in force
    assert body["pending_plan"] is None and body["can_send"] is True  # entitlement is untouched
    assert {"checkout", "next_action", "usage", "cancel_at_period_end"} <= set(body)


# --- local state only ----------------------------------------------------------------------------------------


def test_the_read_makes_no_provider_call(w):
    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, downgrade_to=w.small)
    commit(w)
    set_retired(w.a.merchant)
    w.provider.calls.clear()
    assert get(w.client).status_code == 200
    assert w.provider.calls == []


# --- lifecycle: the member follows the stored state ---------------------------------------------------------------


def test_it_disappears_when_the_replacement_is_abandoned(w):
    w.sub(w.a, "ACTIVE", plan=w.small)
    set_replacement(w.a.merchant, plan=w.big)
    from billing.tests.replacement_helpers import provide_replacement

    provide_replacement(w.provider, w.big, status="authenticated", paid=False)
    resp = w.client.post(CANCEL_REPLACEMENT, {}, format="json", HTTP_X_CSRFTOKEN=w.client.csrf)
    assert resp.status_code == 200
    assert resp.json()["replacement"] is None  # the POST's subscription body carries it too
    assert get(w.client).json()["replacement"] is None


def test_the_post_bodies_carry_the_member_too(w, settings, monkeypatch):
    """The checkout response's `subscription` is the same body."""
    from billing import razorpay
    from billing.exceptions import BillingProviderRejected
    from billing.tests.snapshot_helpers import START, entity

    w.sub(w.a, "ACTIVE", plan=w.small)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START)
    monkeypatch.setattr(razorpay, "UPDATE_UNSUPPORTED_REFUSALS", frozenset({(400, "SYNTHETIC", "synthetic")}))
    w.provider.update_error = BillingProviderRejected("SYNTHETIC", status=400, reason="synthetic")
    settings.BILLING_REPLACEMENT_UPGRADE_ENABLED = True
    settings.BILLING_REPLACEMENT_UPGRADE_AUTH_WINDOW = 3600
    resp = w.client.post(
        "/api/v1/billing/checkout",
        {"plan_id": str(w.big.id), "acknowledge_no_credit": True},
        format="json",
        HTTP_X_CSRFTOKEN=w.client.csrf,
    )
    assert resp.status_code == 201
    assert resp.json()["subscription"]["replacement"]["kind"] == "UPGRADE"
    assert resp.json()["subscription"]["replacement"]["target_plan"]["id"] == str(w.big.pk)


# --- roles and tenant isolation ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["ADMIN"])
def test_an_admin_reads_it_too(w, add_member, session_client, role):
    w.sub(w.a, "ACTIVE", plan=w.small)
    set_replacement(w.a.merchant, plan=w.big)
    member = add_member(w.a.merchant, role, f"{role.lower()}@example.com")
    body = get(session_client(member.user.email)).json()
    assert body["replacement"]["kind"] == "UPGRADE"
    assert REPL_REF not in json.dumps(body)


@pytest.mark.parametrize("role", ["MANAGER", "VIEWER"])
def test_manager_and_viewer_have_no_access(w, add_member, session_client, role):
    w.sub(w.a, "ACTIVE", plan=w.small)
    set_replacement(w.a.merchant, plan=w.big)
    member = add_member(w.a.merchant, role, f"{role.lower()}@example.com")
    assert get(session_client(member.user.email)).status_code == 403


def test_another_merchant_sees_nothing_of_it(w, session_client):
    w.sub(w.a, "ACTIVE", plan=w.small)
    set_replacement(w.a.merchant, plan=w.big)
    w.sub(w.b, "ACTIVE", plan=w.small)
    body = get(session_client(w.b.user.email)).json()
    assert body["replacement"] is None
    assert REPL_REF not in json.dumps(body)


# --- effective_at while committed, and the member after the replacement is gone ----------------------------


def test_a_committed_downgrades_effective_at_stays_the_old_period_end_until_it_switches(w):
    from billing.tests.replacement_helpers import provide_replacement
    from billing.tests.snapshot_helpers import END, START, entity
    from billing import services

    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, downgrade_to=w.small)
    commit(w)
    old_end = period_end_iso(w)
    for _ in range(2):  # reads do not change it
        assert get(w.client).json()["replacement"]["effective_at"] == old_end
    # the replacement is paid and active: the switch clears the member
    w.provider.entities[w.ref(w.a)] = entity(w.big, status="active", start=START)
    provide_replacement(w.provider, w.small, status="active", start=END)
    with tenant_context(w.a.merchant.id):
        services._sync_replacement()
    body = get(w.client).json()
    assert body["replacement"] is None  # no stale effective_at after the switch
    assert body["plan"]["id"] == str(w.small.pk) and body["pending_plan"] is None


# --- abandonment (not through the merchant endpoint): GET shows no stale state -----------------------------------


@pytest.mark.parametrize("kind", ["UPGRADE", "DOWNGRADE"])
def test_after_the_sweep_abandons_a_failed_replacement_get_shows_none_and_no_stale_state(w, kind):
    from billing import services
    from billing.tests.replacement_helpers import provide_replacement

    if kind == "UPGRADE":
        w.sub(w.a, "ACTIVE", plan=w.small)
        set_replacement(w.a.merchant, plan=w.big)
        provide_replacement(w.provider, w.big, status="halted", paid=False)
    else:
        w.sub(w.a, "ACTIVE", plan=w.big)
        set_replacement(w.a.merchant, downgrade_to=w.small)
        provide_replacement(w.provider, w.small, status="halted", paid=False)
    assert get(w.client).json()["replacement"]["kind"] == kind  # visible while pending
    with tenant_context(w.a.merchant.id):
        services._sync_replacement()  # abandoned: the ref moved to the retired slot
    resp = get(w.client)
    body = resp.json()
    assert body["replacement"] is None
    assert body["pending_plan"] is None  # a downgrade replacement's pending plan is gone too
    assert body["status"] == "ACTIVE" and body["can_send"] is True  # the old plan is unchanged
    assert REPL_REF not in resp.content.decode()  # the abandoned ref sits in the retired slot, unexposed


def test_after_the_past_due_transition_abandons_it_get_shows_none(w):
    from billing import services
    from billing.tests.replacement_helpers import provide_replacement
    from billing.tests.snapshot_helpers import START, entity

    w.sub(w.a, "ACTIVE", plan=w.small)
    set_replacement(w.a.merchant, plan=w.big)
    provide_replacement(w.provider, w.big, status="authenticated", paid=False)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="halted", start=START)
    assert get(w.client).json()["replacement"] is not None
    with tenant_context(w.a.merchant.id):
        services._sync_main()  # the PAST_DUE transition abandons a non-active replacement
    body = get(w.client).json()
    assert body["status"] == "PAST_DUE" and body["replacement"] is None
