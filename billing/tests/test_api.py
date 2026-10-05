"""Billing API (spec 07 W5): GET /billing/plans, GET /billing/subscription,
POST /billing/checkout, POST /billing/subscription/cancel. Razorpay is the
FakeProvider from conftest (billing.razorpay replaced at its module boundary):
no test makes a network call. Developer checks; the formal feature-test pass is
/test-feature.

Not covered on purpose, because the spec leaves it open (D1) and the code stops
there: billing.razorpay.fetch_invoices (pagination is not established, so every
test that needs invoices patches it)."""
from datetime import timedelta

import pytest
from django.utils import timezone as dj_timezone

from accounts.models import TeamMember
from auditlog.models import AuditLog
from billing import razorpay, services
from billing.exceptions import BillingProviderRejected, BillingProviderUnavailable
from billing.models import BillingEvent, PaymentAttempt, Subscription, UsageRecord
from billing.tests.webhook_helpers import ORIGINAL_FETCH_INVOICES
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

PLANS = "/api/v1/billing/plans"
SUBSCRIPTION = "/api/v1/billing/subscription"
CHECKOUT = "/api/v1/billing/checkout"
CANCEL = "/api/v1/billing/subscription/cancel"

REF = "sub_test_ref"
NOW = dj_timezone.now().replace(microsecond=0)
START = NOW - timedelta(days=10)  # whole seconds: Razorpay sends unix seconds
END = START + timedelta(days=30)


def ts(dt):
    return int(dt.timestamp())


def invoice(start=START, **over):
    base = {
        "id": "inv_1",
        "subscription_id": REF,
        "status": "paid",
        "amount_due": 0,
        "payment_id": "pay_1",
        "billing_start": ts(start),
        "billing_end": ts(start + timedelta(days=30)),
        "paid_at": ts(start) + 3600,
    }
    base.update(over)
    return base


@pytest.fixture
def s(make_merchant, make_plan, make_subscription, session_client, provider):
    """One merchant with an OWNER session, three offered plans and a
    subscription builder. Razorpay is the fake provider."""

    class S:
        pass

    x = S()
    x.owner = make_merchant("A")
    x.small = make_plan("Starter", price="999.00", quota=100, provider_plan_id="plan_small")
    x.big = make_plan("Growth", price="1999.00", quota=500, provider_plan_id="plan_big")
    x.same_price = make_plan("Pro", price="999.00", quota=300, provider_plan_id="plan_same")
    x.client = session_client(x.owner.user.email)
    x.provider = provider

    def sub(status="ACTIVE", plan=None, ref=REF, merchant=None, **kwargs):
        if status != "INCOMPLETE":
            kwargs.setdefault("current_period_start", START)
            kwargs.setdefault("current_period_end", END)
        if status == "PAST_DUE":
            kwargs.setdefault("past_due_at", NOW)
            kwargs.setdefault("dunning_stage", 0)
            kwargs.setdefault("provider_status", "halted")
        return make_subscription(
            (merchant or x.owner).merchant, plan or x.small, status=status, ref=ref, **kwargs
        )

    def remote(status="active", plan="plan_small", start=START, ref=REF, **fields):
        """What Razorpay reports for the stored ref."""
        return provider.add(
            ref,
            status=status,
            plan_id=plan,
            current_start=ts(start),
            current_end=ts(start + timedelta(days=30)),
            **fields,
        )

    x.sub, x.remote = sub, remote
    return x


def post(client, path, body=None):
    return client.post(path, body or {}, format="json", HTTP_X_CSRFTOKEN=client.csrf)


def row(x, merchant=None):
    with tenant_context((merchant or x.owner).merchant_id), tenant_atomic():
        return Subscription.objects.select_related("plan", "pending_plan").get()


def audits(x, action=None):
    with tenant_context(x.owner.merchant_id), tenant_atomic():
        qs = AuditLog.objects.filter(action__startswith="billing.")
        if action:
            qs = qs.filter(action=action)
        return list(qs.order_by("created_at"))


def writes(provider):
    """Provider calls that change something at Razorpay."""
    return [n for n in provider.names() if n in ("create_subscription", "update_subscription", "cancel_subscription")]


def unchanged(x, before):
    after = row(x)
    for field in ("status", "plan_id", "payment_provider_ref", "cancel_at_period_end", "pending_plan_id"):
        assert getattr(after, field) == getattr(before, field), field


# === GET /billing/plans ======================================================


def test_plans_lists_only_offered_plans_by_price_without_the_provider_plan_id(s, make_plan):
    make_plan("Business", price="0.00", quota=1, provider_plan_id="plan_retired", is_active=False)
    resp = s.client.get(PLANS)
    assert resp.status_code == 200
    body = resp.json()
    assert [p["monthly_price"] for p in body["results"]] == ["999.00", "999.00", "1999.00"]
    assert {p["name"] for p in body["results"]} == {"Starter", "Pro", "Growth"}
    assert set(body["results"][0]) == {"id", "name", "monthly_price", "currency", "quota_requests", "features"}
    assert "provider_plan_id" not in resp.content.decode() and "plan_small" not in resp.content.decode()
    assert body["next_cursor"] is None


def test_plans_is_cursor_paginated(s):
    first = s.client.get(PLANS, {"limit": 2}).json()
    assert len(first["results"]) == 2 and first["next_cursor"]
    second = s.client.get(PLANS, {"limit": 2, "cursor": first["next_cursor"]}).json()
    assert len(second["results"]) == 1 and second["next_cursor"] is None
    names = [p["name"] for p in first["results"] + second["results"]]
    assert sorted(names) == ["Growth", "Pro", "Starter"]


@pytest.mark.parametrize("role", ["OWNER", "ADMIN"])
def test_owner_and_admin_can_read_plans_and_the_subscription(s, add_member, session_client, role):
    member = add_member(s.owner.merchant, role, f"{role.lower()}@example.com")
    client = session_client(member.user.email)
    assert client.get(PLANS).status_code == 200
    assert client.get(SUBSCRIPTION).status_code == 200


@pytest.mark.parametrize("role", ["MANAGER", "VIEWER"])
def test_manager_and_viewer_have_no_billing_access(s, add_member, session_client, role):
    member = add_member(s.owner.merchant, role, f"{role.lower()}@example.com")
    client = session_client(member.user.email)
    assert client.get(PLANS).status_code == 403
    assert client.get(SUBSCRIPTION).status_code == 403
    assert post(client, CHECKOUT, {"plan_id": str(s.small.id)}).status_code == 403
    assert post(client, CANCEL).status_code == 403
    assert s.provider.calls == []


def test_admin_cannot_change_billing(s, add_member, session_client):
    member = add_member(s.owner.merchant, "ADMIN", "admin@example.com")
    client = session_client(member.user.email)
    assert post(client, CHECKOUT, {"plan_id": str(s.small.id)}).status_code == 403
    assert post(client, CANCEL).status_code == 403
    assert s.provider.calls == []


def test_writes_without_csrf_and_bearer_keys_are_refused_on_every_endpoint(s, make_key, login):
    client, resp = login(s.owner.user.email)
    assert resp.status_code == 200
    body = {"plan_id": str(s.small.id)}
    assert client.post(CHECKOUT, body, format="json").status_code == 403  # session, no CSRF token
    assert client.post(CANCEL, {}, format="json").status_code == 403

    _, raw = make_key(s.owner)
    from rest_framework.test import APIClient

    keyed = APIClient()
    for method, path in (("get", PLANS), ("get", SUBSCRIPTION), ("post", CHECKOUT), ("post", CANCEL)):
        resp = getattr(keyed, method)(path, **{"HTTP_AUTHORIZATION": f"Bearer {raw}"})
        assert resp.status_code == 403, path
    assert s.provider.calls == []


# === GET /billing/subscription ===============================================


def test_subscription_with_no_row(s):
    body = s.client.get(SUBSCRIPTION).json()
    assert body["status"] is None and body["plan"] is None and body["usage"] is None
    assert body["can_send"] is False and body["checkout"] is None
    assert body["next_action"] == {"type": "SUBSCRIBE", "at": None}
    assert s.provider.calls == []


def test_subscription_for_an_active_row_reports_usage_and_renewal(s):
    s.sub("ACTIVE")
    with tenant_context(s.owner.merchant_id), tenant_atomic():
        UsageRecord.objects.update(requests_used=30)
    body = s.client.get(SUBSCRIPTION).json()
    assert body["status"] == "ACTIVE" and body["plan"]["name"] == "Starter"
    assert body["usage"] == {"requests_used": 30, "quota_requests": 100, "requests_remaining": 70}
    assert body["can_send"] is True and body["checkout"] is None
    assert body["next_action"]["type"] == "RENEWAL" and body["next_action"]["at"]
    assert body["past_due_at"] is None and body["grace_ends_at"] is None
    assert "provider_plan_id" not in str(body)
    assert s.provider.calls == []  # reads local state only


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"status": "INCOMPLETE"}, "COMPLETE_CHECKOUT"),
        ({"status": "CANCELLED"}, "SUBSCRIBE"),
        ({"status": "EXPIRED"}, "SUBSCRIBE"),
        ({"status": "ACTIVE", "cancel_at_period_end": True}, "CANCELLATION"),
        ({"status": "ACTIVE", "plan_key": "big", "pending": "small"}, "PLAN_CHANGE"),
        ({"status": "PAST_DUE", "provider_status": "pending"}, "UPDATE_PAYMENT_METHOD"),
        ({"status": "PAST_DUE", "provider_status": "halted"}, "RESUBSCRIBE"),
        ({"status": "PAST_DUE", "provider_status": "active"}, "RESUBSCRIBE"),
    ],
)
def test_subscription_next_action(s, kwargs, expected):
    kwargs = dict(kwargs)
    if kwargs.pop("plan_key", None) == "big":
        kwargs["plan"] = s.big
        kwargs["pending_plan"] = getattr(s, kwargs.pop("pending"))
    s.sub(**kwargs)
    assert s.client.get(SUBSCRIPTION).json()["next_action"]["type"] == expected


def test_subscription_checkout_object_is_owner_only_and_only_where_payment_can_happen(
    s, add_member, session_client
):
    s.sub("INCOMPLETE", ref="sub_incomplete1")
    owner_body = s.client.get(SUBSCRIPTION).json()
    assert owner_body["checkout"] == {
        "provider": "razorpay",
        "key_id": "rzp_test_keyid",
        "subscription_id": "sub_incomplete1",
        "card_change": False,
    }
    admin = session_client(add_member(s.owner.merchant, "ADMIN", "admin@example.com").user.email)
    assert admin.get(SUBSCRIPTION).json()["checkout"] is None  # never for an ADMIN
    assert "secret" not in s.client.get(SUBSCRIPTION).content.decode().lower()


def test_subscription_checkout_object_for_past_due_needs_a_pending_provider(s, add_member, session_client):
    s.sub("PAST_DUE", provider_status="pending")
    body = s.client.get(SUBSCRIPTION).json()
    assert body["checkout"]["card_change"] is True
    assert body["next_action"]["at"] == body["grace_ends_at"]
    assert body["dunning_stage"] == 0 and body["past_due_at"]
    admin = session_client(add_member(s.owner.merchant, "ADMIN", "admin@example.com").user.email)
    assert admin.get(SUBSCRIPTION).json()["checkout"] is None

    with tenant_context(s.owner.merchant_id), tenant_atomic():
        Subscription.objects.update(provider_status="halted")
    body = s.client.get(SUBSCRIPTION).json()
    assert body["checkout"] is None and body["next_action"]["type"] == "RESUBSCRIBE"


def test_tenant_isolation_between_merchants(s, make_merchant, session_client):
    other = make_merchant("B")
    s.sub("ACTIVE")
    s.sub("ACTIVE", plan=s.big, ref="sub_other", merchant=other)
    mine = s.client.get(SUBSCRIPTION).json()
    theirs = session_client(other.user.email).get(SUBSCRIPTION).json()
    assert mine["plan"]["name"] == "Starter" and theirs["plan"]["name"] == "Growth"


# === POST /billing/checkout: validation ======================================


def test_checkout_rejects_a_missing_malformed_unknown_inactive_or_providerless_plan(s, make_plan):
    retired = make_plan("Business", price="10.00", quota=1, provider_plan_id="plan_old", is_active=False)
    providerless = make_plan("Business", price="10.00", quota=1, provider_plan_id=None)
    import uuid

    for body in (
        {},
        {"plan_id": "not-a-uuid"},
        {"plan_id": str(uuid.uuid4())},
        {"plan_id": str(retired.id)},
        {"plan_id": str(providerless.id)},
    ):
        resp = post(s.client, CHECKOUT, body)
        assert resp.status_code == 422, body
        assert resp.json()["error"]["code"] == "validation_error"
    assert s.provider.calls == []
    with tenant_context(s.owner.merchant_id), tenant_atomic():
        assert Subscription.objects.count() == 0


def test_checkout_unset_credentials_is_503_and_nothing_is_called(s, settings):
    settings.RAZORPAY_KEY_SECRET = ""
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "billing_not_configured"
    assert s.provider.calls == []


def test_checkout_ignores_client_supplied_identity_status_and_price(s, make_merchant):
    other = make_merchant("B")
    resp = post(
        s.client,
        CHECKOUT,
        {
            "plan_id": str(s.small.id),
            "merchant_id": str(other.merchant_id),
            "status": "ACTIVE",
            "monthly_price": "0.01",
            "payment_provider_ref": "sub_forged",
        },
    )
    assert resp.status_code == 201
    mine = row(s)
    assert mine.status == "INCOMPLETE" and mine.payment_provider_ref != "sub_forged"
    with tenant_context(other.merchant_id), tenant_atomic():
        assert Subscription.objects.count() == 0


# === POST /billing/checkout: first checkout and idempotency ======================


def test_first_checkout_creates_an_incomplete_row_and_returns_the_checkout(s):
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 201
    body = resp.json()
    ref = body["checkout"]["subscription_id"]
    assert body["checkout"] == {"provider": "razorpay", "key_id": "rzp_test_keyid", "subscription_id": ref}
    assert "secret" not in resp.content.decode().lower()
    after = row(s)
    assert (after.status, after.payment_provider_ref, after.plan_id) == ("INCOMPLETE", ref, s.small.id)
    assert after.provider_status == "created"
    assert s.provider.count("create_subscription") == 1
    assert len(audits(s, "billing.checkout_started")) == 1
    assert body["subscription"]["status"] == "INCOMPLETE"
    assert body["subscription"]["checkout"]["card_change"] is False
    assert body["subscription"]["next_action"]["type"] == "COMPLETE_CHECKOUT"


def test_repeating_the_same_plan_returns_the_same_subscription_without_a_second_create(s):
    first = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)}).json()
    again = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert again.status_code == 200
    assert again.json()["checkout"] == first["checkout"]
    assert s.provider.count("create_subscription") == 1
    assert writes(s.provider) == ["create_subscription"]
    assert len(audits(s, "billing.checkout_started")) == 1


def test_a_failed_first_create_leaves_no_row_behind(s):
    s.provider.create_error = BillingProviderUnavailable()
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "billing_provider_unavailable"
    with tenant_context(s.owner.merchant_id), tenant_atomic():
        assert Subscription.objects.count() == 0
    assert audits(s) == []


def test_a_refused_create_is_a_502_too(s):
    s.provider.create_error = BillingProviderRejected("BAD_REQUEST_ERROR")
    assert post(s.client, CHECKOUT, {"plan_id": str(s.small.id)}).status_code == 502
    with tenant_context(s.owner.merchant_id), tenant_atomic():
        assert Subscription.objects.count() == 0


@pytest.mark.parametrize(
    "bad_id", [None, "", "sub-1", "sub 1", "x" * 65, "sub_1" + chr(10), 12345, ["sub_1"]]
)
def test_an_unusable_id_in_the_create_response_is_never_stored(s, bad_id):
    """F5: nothing is persisted, and no unusable reference is left behind."""
    s.provider.create_entity = {"id": bad_id, "status": "created"}
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "billing_provider_unavailable"
    with tenant_context(s.owner.merchant_id), tenant_atomic():
        assert Subscription.objects.count() == 0
    assert audits(s) == []


def test_an_unusable_id_on_a_replacement_leaves_the_row_untouched(s):
    s.sub("EXPIRED", ref="sub_old", provider_status="expired")
    s.remote(status="expired", ref="sub_old")
    s.provider.create_entity = {"id": "bad id", "status": "created"}
    before = row(s)
    assert post(s.client, CHECKOUT, {"plan_id": str(s.small.id)}).status_code == 502
    unchanged(s, before)
    assert row(s).payment_provider_ref == "sub_old"


# === POST /billing/checkout: INCOMPLETE rows ===================================


def test_incomplete_created_different_plan_cancels_the_old_then_creates_a_new_one(s):
    s.sub("INCOMPLETE", ref="sub_old")
    s.remote(status="created", plan="plan_small", ref="sub_old")
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 201
    new_ref = resp.json()["checkout"]["subscription_id"]
    assert new_ref != "sub_old"
    assert s.provider.names() == ["fetch_subscription", "cancel_subscription", "create_subscription"]
    assert ("cancel_subscription", "sub_old", False) in s.provider.calls
    after = row(s)
    assert (after.payment_provider_ref, after.plan_id, after.status) == (new_ref, s.big.id, "INCOMPLETE")


def test_a_failed_cancel_keeps_the_old_reference_and_creates_nothing(s):
    s.sub("INCOMPLETE", ref="sub_old")
    s.remote(status="created", plan="plan_small", ref="sub_old")
    s.provider.cancel_error = BillingProviderUnavailable()
    before = row(s)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "billing_provider_unavailable"
    assert "create_subscription" not in s.provider.names()
    unchanged(s, before)


def test_a_refused_cancel_is_a_502_with_nothing_created(s):
    s.sub("INCOMPLETE", ref="sub_old")
    s.remote(status="created", plan="plan_small", ref="sub_old")
    s.provider.cancel_error = BillingProviderRejected("BAD_REQUEST_ERROR")
    assert post(s.client, CHECKOUT, {"plan_id": str(s.big.id)}).status_code == 502
    assert "create_subscription" not in s.provider.names()
    assert row(s).payment_provider_ref == "sub_old"


def test_cancel_ok_create_failed_then_the_retry_creates_afresh_never_the_dead_id(s):
    s.sub("INCOMPLETE", ref="sub_old")
    s.remote(status="created", plan="plan_small", ref="sub_old")
    s.provider.create_error = BillingProviderUnavailable()
    assert post(s.client, CHECKOUT, {"plan_id": str(s.big.id)}).status_code == 502
    assert row(s).payment_provider_ref == "sub_old"  # the local row never moved

    s.provider.create_error = None
    retry = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert retry.status_code == 201
    new_ref = retry.json()["checkout"]["subscription_id"]
    assert new_ref != "sub_old" and row(s).payment_provider_ref == new_ref
    # the retry's reconcile saw the old subscription cancelled: it was not cancelled twice
    assert s.provider.count("cancel_subscription") == 1


@pytest.mark.parametrize("pstatus", ["authenticated", "active", "pending", "halted"])
def test_incomplete_authorized_same_plan_returns_the_existing_checkout_with_no_provider_write(s, pstatus):
    s.sub("INCOMPLETE", ref="sub_auth")
    s.remote(status=pstatus, plan="plan_small", ref="sub_auth")
    s.provider.invoices = []  # active, but no qualifying paid invoice
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 200
    assert resp.json()["checkout"]["subscription_id"] == "sub_auth"
    assert writes(s.provider) == []
    assert row(s).status == "INCOMPLETE"


@pytest.mark.parametrize("pstatus", ["authenticated", "active", "pending", "halted"])
def test_incomplete_authorized_different_plan_is_409_activating_and_nothing_is_cancelled(s, pstatus):
    s.sub("INCOMPLETE", ref="sub_auth")
    s.remote(status=pstatus, plan="plan_small", ref="sub_auth")
    s.provider.invoices = []
    before = row(s)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "subscription_activating"
    assert writes(s.provider) == []
    unchanged(s, before)


@pytest.mark.parametrize("pstatus", ["cancelled", "expired", "completed"])
def test_incomplete_with_a_terminal_provider_subscription_creates_a_new_one(s, pstatus):
    s.sub("INCOMPLETE", ref="sub_dead")
    s.remote(status=pstatus, ref="sub_dead")
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 201
    assert "cancel_subscription" not in s.provider.names()  # nothing left to cancel
    assert row(s).payment_provider_ref != "sub_dead"


def test_incomplete_row_with_no_reference_creates_one(s):
    s.sub("INCOMPLETE", ref=None)
    assert post(s.client, CHECKOUT, {"plan_id": str(s.small.id)}).status_code == 201
    assert s.provider.names() == ["create_subscription"]
    assert row(s).payment_provider_ref


def test_incomplete_reuse_compares_the_providers_plan_not_the_local_rows(s):
    """F3: "same plan" means the fetched plan_id equals the requested plan's
    provider_plan_id, even if the local row names another plan."""
    s.sub("INCOMPLETE", plan=s.big, ref="sub_x")  # local row says Growth...
    s.remote(status="created", plan="plan_small", ref="sub_x")  # ...Razorpay holds Starter
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 200 and resp.json()["checkout"]["subscription_id"] == "sub_x"
    assert writes(s.provider) == []


def test_paid_but_not_yet_webhooked_then_plan_switched_never_cancels_the_paid_subscription(s):
    s.sub("INCOMPLETE", ref=REF)
    s.remote(status="active", plan="plan_small")
    s.provider.invoices = [invoice()]
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 200 and resp.json()["checkout"] is None
    assert "cancel_subscription" not in s.provider.names()
    assert "create_subscription" not in s.provider.names()
    assert ("update_subscription", REF, "plan_big", "now") in s.provider.calls
    after = row(s)
    assert (after.status, after.plan_id) == ("ACTIVE", s.big.id)
    assert [a.action for a in audits(s)][:1] == ["billing.subscription_activated"]
    with tenant_context(s.owner.merchant_id), tenant_atomic():
        assert PaymentAttempt.objects.count() == 1 and UsageRecord.objects.count() == 1


# === D3: the reconcile of this very request moved the row into ACTIVE ===========


def test_paid_but_not_yet_webhooked_same_plan_is_a_200_no_op(s):
    s.sub("INCOMPLETE", ref=REF)
    s.remote(status="active", plan="plan_small")
    s.provider.invoices = [invoice()]
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["checkout"] is None
    assert body["subscription"]["status"] == "ACTIVE" and body["subscription"]["can_send"] is True
    assert s.provider.names() == ["fetch_subscription", "fetch_invoices"]  # no further provider call
    assert [a.action for a in audits(s)] == ["billing.subscription_activated"]  # no further audit row


def test_the_no_op_is_only_for_the_request_whose_reconcile_activated_the_row(s):
    """The very next same-plan checkout finds a row that was already ACTIVE
    before the request: the existing 422 rule."""
    s.sub("INCOMPLETE", ref=REF)
    s.remote(status="active", plan="plan_small")
    s.provider.invoices = [invoice()]
    assert post(s.client, CHECKOUT, {"plan_id": str(s.small.id)}).status_code == 200
    again = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert again.status_code == 422 and again.json()["error"]["code"] == "validation_error"
    assert writes(s.provider) == []


def test_the_no_op_needs_the_plan_now_in_force_not_merely_the_same_price(s):
    s.sub("INCOMPLETE", ref=REF)
    s.remote(status="active", plan="plan_small")
    s.provider.invoices = [invoice()]
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.same_price.id)})
    assert resp.status_code == 422  # a different plan at the same price: the existing rule
    assert row(s).status == "ACTIVE" and writes(s.provider) == []


def test_the_no_op_compares_against_the_plan_the_reconcile_put_in_force(s):
    """The local row named Growth, Razorpay holds (and was paid for) Starter:
    after the reconcile the plan in force is Starter."""
    s.sub("INCOMPLETE", plan=s.big, ref=REF)
    s.remote(status="active", plan="plan_small")
    s.provider.invoices = [invoice()]
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 200 and resp.json()["checkout"] is None
    assert row(s).plan_id == s.small.id and writes(s.provider) == []


# === D2: what a replacement subscription resets and what it preserves ============


def _history(x):
    with tenant_context(x.owner.merchant_id), tenant_atomic():
        return (
            sorted((u.period_start, u.requests_used) for u in UsageRecord.objects.all()),
            PaymentAttempt.objects.count(),
            BillingEvent.objects.count(),
            AuditLog.objects.count(),
        )


@pytest.mark.parametrize(
    "local, pstatus, cancels_first",
    [
        ("CANCELLED", "cancelled", False),
        ("EXPIRED", "expired", False),
        ("CANCELLED", "halted", True),
        ("EXPIRED", "active", True),
    ],
)
def test_a_replacement_resets_the_row_and_keeps_every_historical_record(s, local, pstatus, cancels_first):
    old = s.sub(
        local,
        plan=s.big,
        ref="sub_old",
        provider_status=pstatus,
        pending_plan=s.small,
        cancel_at_period_end=True,
        provider_synced_at=NOW - timedelta(days=2),
    )
    with tenant_context(s.owner.merchant_id), tenant_atomic():
        UsageRecord.objects.update(requests_used=77)
        PaymentAttempt.objects.create(
            merchant=s.owner.merchant,
            subscription=old,
            provider="razorpay",
            provider_attempt_id="pay_old",
            attempt_type="RENEWAL",
            status="SUCCEEDED",
            attempted_at=START,
        )
        BillingEvent.objects.create(
            merchant=s.owner.merchant,
            provider="razorpay",
            provider_event_id="evt_old",
            event_type="subscription.charged",
            provider_ref="sub_old",
        )
    usage_before, payments_before, events_before, audits_before = _history(s)
    s.remote(status=pstatus, plan="plan_big", ref="sub_old", start=START)
    s.provider.invoices = []  # an `active` old subscription has no paid invoice

    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 201
    new_ref = resp.json()["checkout"]["subscription_id"]
    assert ("cancel_subscription" in s.provider.names()) is cancels_first

    after = row(s)
    assert after.id == old.id  # the same row, reused
    assert after.plan_id == s.small.id
    assert after.status == "INCOMPLETE"
    assert after.payment_provider_ref == new_ref != "sub_old"
    assert after.provider_status == "created"
    assert after.pending_plan_id is None
    assert after.cancel_at_period_end is False
    assert after.provider_synced_at is None
    assert after.current_period_start is None and after.current_period_end is None
    assert (after.past_due_at, after.dunning_stage) == (None, None)

    usage_after, payments_after, events_after, audits_after = _history(s)
    assert usage_after == usage_before == [(START, 77)]  # not deleted, not reset
    assert (payments_after, events_after) == (payments_before, events_before) == (1, 1)
    assert audits_after == audits_before + 1  # only checkout_started was added

    body = resp.json()["subscription"]
    assert body["status"] == "INCOMPLETE" and body["plan"]["name"] == "Starter"
    assert body["current_period_start"] is None and body["current_period_end"] is None
    assert body["usage"] is None and body["can_send"] is False  # no old usage on the new lifecycle
    assert body["pending_plan"] is None and body["cancel_at_period_end"] is False
    assert body["next_action"]["type"] == "COMPLETE_CHECKOUT"


def test_replacing_an_incomplete_subscription_resets_the_same_fields(s):
    s.sub(
        "INCOMPLETE",
        ref="sub_old",
        provider_status="created",
        provider_synced_at=NOW - timedelta(hours=1),
        cancel_at_period_end=True,
        pending_plan=s.small,
    )
    s.remote(status="created", plan="plan_small", ref="sub_old")
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 201
    after = row(s)
    assert (after.plan_id, after.status, after.provider_status) == (s.big.id, "INCOMPLETE", "created")
    assert after.payment_provider_ref == resp.json()["checkout"]["subscription_id"]
    assert after.pending_plan_id is None and after.cancel_at_period_end is False
    assert after.provider_synced_at is None
    assert after.current_period_start is None and after.current_period_end is None


def test_a_replaced_subscription_activates_on_its_own_period_with_a_fresh_usage_record(s):
    """The new lifecycle starts clean: the paid reconcile of the new
    subscription sets the new period and opens a new UsageRecord, and the old
    one is untouched (no rollover)."""
    s.sub("EXPIRED", ref="sub_old", provider_status="expired")
    with tenant_context(s.owner.merchant_id), tenant_atomic():
        UsageRecord.objects.update(requests_used=40)
    s.remote(status="expired", ref="sub_old")
    new_ref = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)}).json()["checkout"]["subscription_id"]

    new_start = NOW - timedelta(hours=1)
    s.remote(status="active", plan="plan_small", start=new_start, ref=new_ref)
    s.provider.invoices = [invoice(start=new_start, subscription_id=new_ref, payment_id="pay_new")]
    with tenant_context(s.owner.merchant_id):
        services.sync_subscription()

    after = row(s)
    assert after.status == "ACTIVE" and after.current_period_start == new_start
    with tenant_context(s.owner.merchant_id), tenant_atomic():
        usage = sorted((u.period_start, u.requests_used) for u in UsageRecord.objects.all())
    assert usage == [(START, 40), (new_start, 0)]


def test_a_failed_replacement_resets_nothing(s):
    s.sub("CANCELLED", plan=s.big, ref="sub_old", provider_status="cancelled", pending_plan=s.small)
    s.remote(status="cancelled", plan="plan_big", ref="sub_old")
    s.provider.create_error = BillingProviderUnavailable()
    assert post(s.client, CHECKOUT, {"plan_id": str(s.small.id)}).status_code == 502
    after = row(s)
    assert (after.status, after.plan_id, after.payment_provider_ref) == ("CANCELLED", s.big.id, "sub_old")
    assert after.pending_plan_id == s.small.id and after.current_period_start == START


# === POST /billing/checkout: ACTIVE rows (plan changes) ========================


def test_upgrade_updates_the_provider_now_and_changes_the_plan_immediately(s):
    s.sub("ACTIVE")
    s.remote(status="active", plan="plan_small", start=START)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 200 and resp.json()["checkout"] is None
    assert ("update_subscription", REF, "plan_big", "now") in s.provider.calls
    assert "create_subscription" not in s.provider.names() and "cancel_subscription" not in s.provider.names()
    assert row(s).plan_id == s.big.id
    [audit] = audits(s, "billing.plan_changed")
    assert audit.metadata_json["effective"] == "IMMEDIATE" and audit.actor_user_id == s.owner.user_id
    assert resp.json()["subscription"]["plan"]["name"] == "Growth"


def test_an_upgrade_keeps_consumed_usage_when_the_period_is_unchanged(s):
    s.sub("ACTIVE")
    with tenant_context(s.owner.merchant_id), tenant_atomic():
        UsageRecord.objects.update(requests_used=100)
    s.remote(status="active", plan="plan_small", start=START)
    post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    body = s.client.get(SUBSCRIPTION).json()
    assert body["usage"] == {"requests_used": 100, "quota_requests": 500, "requests_remaining": 400}
    assert body["can_send"] is True


def test_an_upgrade_that_moves_the_period_needs_a_paid_invoice_for_it(s):
    """T4 is an external gate; this only checks the paid-entitlement rule
    holds when the provider reports a new cycle after an update."""
    s.sub("ACTIVE")
    s.remote(status="active", plan="plan_small", start=START)
    s.provider.update_extra = {"current_start": ts(END), "current_end": ts(END + timedelta(days=30))}
    s.provider.invoices = []  # no qualifying invoice for the new cycle
    assert post(s.client, CHECKOUT, {"plan_id": str(s.big.id)}).status_code == 200
    after = row(s)
    assert after.plan_id == s.small.id and after.current_period_start == START
    with tenant_context(s.owner.merchant_id), tenant_atomic():
        assert UsageRecord.objects.count() == 1


def test_an_upgrade_that_moves_the_period_with_a_paid_invoice_advances_it(s):
    s.sub("ACTIVE")
    s.remote(status="active", plan="plan_small", start=START)
    s.provider.update_extra = {"current_start": ts(END), "current_end": ts(END + timedelta(days=30))}
    s.provider.invoices = [invoice(start=END, payment_id="pay_upgrade")]
    assert post(s.client, CHECKOUT, {"plan_id": str(s.big.id)}).status_code == 200
    after = row(s)
    assert after.plan_id == s.big.id and after.current_period_start == END
    with tenant_context(s.owner.merchant_id), tenant_atomic():
        assert UsageRecord.objects.count() == 2


def test_downgrade_is_scheduled_for_the_cycle_end_and_changes_nothing_now(s):
    s.sub("ACTIVE", plan=s.big)
    s.remote(status="active", plan="plan_big", start=START)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 200 and resp.json()["checkout"] is None
    assert ("update_subscription", REF, "plan_small", "cycle_end") in s.provider.calls
    after = row(s)
    assert after.plan_id == s.big.id and after.pending_plan_id == s.small.id
    [audit] = audits(s, "billing.plan_changed")
    assert audit.metadata_json["effective"] == "SCHEDULED"
    assert resp.json()["subscription"]["next_action"]["type"] == "PLAN_CHANGE"
    assert resp.json()["subscription"]["pending_plan"]["name"] == "Starter"


def test_a_repeated_downgrade_to_the_pending_plan_changes_nothing(s):
    s.sub("ACTIVE", plan=s.big, pending_plan=s.small)
    s.remote(status="active", plan="plan_big", start=START)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 200
    assert writes(s.provider) == [] and audits(s) == []


def test_active_same_plan_and_same_price_are_422(s):
    s.sub("ACTIVE")
    s.remote(status="active", plan="plan_small", start=START)
    for plan in (s.small, s.same_price):
        resp = post(s.client, CHECKOUT, {"plan_id": str(plan.id)})
        assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"
    assert writes(s.provider) == []


def test_active_cancelling_is_409(s):
    s.sub("ACTIVE", cancel_at_period_end=True)
    s.remote(status="active", plan="plan_small", start=START)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "subscription_cancelling"
    assert writes(s.provider) == []


def test_a_provider_that_refuses_the_update_is_409_plan_change_unsupported_and_nothing_changes(s):
    s.sub("ACTIVE")
    s.remote(status="active", plan="plan_small", start=START)
    s.provider.update_error = BillingProviderRejected("BAD_REQUEST_ERROR")
    before = row(s)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "plan_change_unsupported"
    assert writes(s.provider) == ["update_subscription"]  # no create, no cancel
    unchanged(s, before)
    assert audits(s) == []


def test_a_provider_timeout_on_the_update_is_502_with_no_local_change(s):
    s.sub("ACTIVE")
    s.remote(status="active", plan="plan_small", start=START)
    s.provider.update_error = BillingProviderUnavailable()
    before = row(s)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "billing_provider_unavailable"
    unchanged(s, before)


# === POST /billing/checkout: PAST_DUE (A1) ======================================


@pytest.mark.parametrize("pstatus", ["pending", "halted", "active", "paused", "cancelled"])
def test_past_due_is_409_before_any_provider_call_or_local_write(s, pstatus):
    s.sub("PAST_DUE", provider_status=pstatus)
    s.remote(status=pstatus)
    before = row(s)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "subscription_past_due"
    assert s.provider.calls == []  # not even a fetch
    after = row(s)
    unchanged(s, before)
    assert (after.past_due_at, after.dunning_stage, after.provider_status) == (
        before.past_due_at,
        before.dunning_stage,
        before.provider_status,
    )


def test_an_active_row_the_reconcile_moves_to_past_due_answers_409_past_due(s):
    s.sub("ACTIVE")
    s.remote(status="halted", plan="plan_small", start=START)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "subscription_past_due"
    assert row(s).status == "PAST_DUE"  # the reconcile survives the 409
    assert writes(s.provider) == []


# === POST /billing/checkout: CANCELLED / EXPIRED ================================


@pytest.mark.parametrize("local", ["CANCELLED", "EXPIRED"])
@pytest.mark.parametrize("pstatus", ["cancelled", "completed", "expired"])
def test_ended_row_with_a_terminal_provider_subscription_creates_a_new_one(s, local, pstatus):
    s.sub(local, ref="sub_old", provider_status=pstatus)
    s.remote(status=pstatus, ref="sub_old")
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 201
    assert "cancel_subscription" not in s.provider.names()
    after = row(s)
    assert after.status == "INCOMPLETE" and after.payment_provider_ref != "sub_old"


@pytest.mark.parametrize("local", ["CANCELLED", "EXPIRED"])
def test_ended_row_with_no_reference_creates_one(s, local):
    s.sub(local, ref=None)
    assert post(s.client, CHECKOUT, {"plan_id": str(s.small.id)}).status_code == 201
    assert s.provider.names() == ["create_subscription"]


@pytest.mark.parametrize("local", ["CANCELLED", "EXPIRED"])
@pytest.mark.parametrize("pstatus", ["created", "pending", "halted", "active"])
def test_ended_row_with_an_open_provider_subscription_cancels_it_first(s, local, pstatus):
    s.sub(local, ref="sub_old", provider_status=pstatus)
    s.remote(status=pstatus, ref="sub_old")
    s.provider.invoices = []  # an active one has no paid invoice
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 201
    names = s.provider.names()
    assert names.index("cancel_subscription") < names.index("create_subscription")
    assert ("cancel_subscription", "sub_old", False) in s.provider.calls
    assert row(s).payment_provider_ref != "sub_old" and row(s).status == "INCOMPLETE"


@pytest.mark.parametrize("local", ["CANCELLED", "EXPIRED"])
def test_a_failed_cancel_of_an_open_old_subscription_blocks_the_checkout(s, local):
    s.sub(local, ref="sub_old", provider_status="halted")
    s.remote(status="halted", ref="sub_old")
    s.provider.cancel_error = BillingProviderRejected("BAD_REQUEST_ERROR")
    before = row(s)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "billing_provider_unavailable"
    assert "create_subscription" not in s.provider.names()
    unchanged(s, before)


@pytest.mark.parametrize("local", ["CANCELLED", "EXPIRED"])
def test_a_late_paid_charge_converges_to_active_without_a_second_provider_subscription(s, local):
    s.sub(local, ref=REF, provider_status="active")
    new_start = NOW - timedelta(days=1)
    s.remote(status="active", plan="plan_small", start=new_start)
    s.provider.invoices = [invoice(start=new_start)]
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    # D3: this request's own reconcile made the row ACTIVE on the requested
    # plan, so it is a same-plan no-op, not a 422.
    assert resp.status_code == 200 and resp.json()["checkout"] is None
    assert row(s).status == "ACTIVE"
    assert writes(s.provider) == []
    assert [a.action for a in audits(s)] == ["billing.subscription_activated"]


@pytest.mark.parametrize("local", ["INCOMPLETE", "CANCELLED", "EXPIRED"])
def test_a_stale_cancellation_flag_cannot_cause_409_cancelling_after_a_paid_reactivation(s, local):
    """Decision 2 at the response level: the flag is cleared on the paid
    reactivation, so the same checkout is handled as an upgrade instead of
    answering subscription_cancelling."""
    s.sub(local, ref=REF, provider_status="active", cancel_at_period_end=True)
    new_start = NOW - timedelta(days=1)
    s.remote(status="active", plan="plan_small", start=new_start)
    s.provider.invoices = [invoice(start=new_start)]
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 200, resp.content
    assert ("update_subscription", REF, "plan_big", "now") in s.provider.calls
    after = row(s)
    assert (after.status, after.cancel_at_period_end, after.plan_id) == ("ACTIVE", False, s.big.id)


# === POST /billing/checkout: unsupported provider states (F2, F3, A2, A3, F4) ===


@pytest.mark.parametrize("local", ["ACTIVE", "INCOMPLETE", "CANCELLED", "EXPIRED"])
@pytest.mark.parametrize("pstatus", ["paused", "future_status"])
def test_paused_or_unrecognized_provider_status_is_409_and_nothing_is_touched(s, local, pstatus):
    s.sub(local)
    s.remote(status=pstatus, plan="plan_small", start=START)
    before = row(s)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "subscription_provider_state_unsupported"
    assert writes(s.provider) == []
    unchanged(s, before)
    assert row(s).provider_status == pstatus  # the reconcile snapshot is stored


@pytest.mark.parametrize("local", ["CANCELLED", "EXPIRED"])
def test_authenticated_on_an_ended_row_is_409_provider_state_unsupported(s, local):
    s.sub(local, ref="sub_old", provider_status="authenticated")
    s.remote(status="authenticated", ref="sub_old")
    before = row(s)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "subscription_provider_state_unsupported"
    assert writes(s.provider) == []
    unchanged(s, before)


@pytest.mark.parametrize(
    "local, pstatus",
    [
        ("ACTIVE", "active"),
        ("INCOMPLETE", "created"),
        ("INCOMPLETE", "authenticated"),
        ("CANCELLED", "cancelled"),
        ("EXPIRED", "halted"),
        ("CANCELLED", "active"),
    ],
)
def test_a_known_status_with_an_unmapped_plan_is_409_plan_unsupported(s, local, pstatus):
    s.sub(local)
    s.remote(status=pstatus, plan="plan_nobody_knows", start=START)
    s.provider.invoices = []
    before = row(s)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "subscription_plan_unsupported"
    assert writes(s.provider) == []
    unchanged(s, before)


@pytest.mark.parametrize("local", ["ACTIVE", "INCOMPLETE", "CANCELLED", "EXPIRED"])
def test_a_failed_reconcile_fetch_is_502_with_no_write_and_no_local_change(s, local):
    s.sub(local)
    s.provider.fetch_error = BillingProviderUnavailable()
    before = row(s)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "billing_provider_unavailable"
    assert s.provider.names() == ["fetch_subscription"]
    unchanged(s, before)


@pytest.mark.parametrize("local", ["ACTIVE", "INCOMPLETE", "CANCELLED", "EXPIRED"])
def test_a_permanent_4xx_on_the_fetch_fails_closed_and_replaces_nothing(s, local):
    """F4: a ref Razorpay no longer knows is never replaced automatically."""
    s.sub(local, ref="sub_forgotten")
    s.provider.fetch_error = BillingProviderRejected("BAD_REQUEST_ERROR")
    before = row(s)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "billing_provider_unavailable"
    assert writes(s.provider) == []
    unchanged(s, before)
    assert row(s).payment_provider_ref == "sub_forgotten"


@pytest.mark.parametrize(
    "entity",
    [
        {"status": "active"},  # no id
        {"id": REF},  # no status
        {"id": REF, "status": None},
        {"id": REF, "status": 7},
        {"id": "sub_someone_else", "status": "active"},  # not the subscription we asked for
    ],
)
def test_a_malformed_provider_entity_is_502_with_no_local_or_provider_mutation(s, entity):
    """A3."""
    s.sub("ACTIVE")
    s.provider.entities[REF] = entity
    before = row(s)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "billing_provider_unavailable"
    assert writes(s.provider) == []
    unchanged(s, before)
    assert row(s).provider_synced_at is None  # nothing was applied


def test_a_failed_invoice_fetch_is_502_and_the_row_is_unchanged(s):
    s.sub("INCOMPLETE", ref=REF)
    s.remote(status="active", plan="plan_small")
    s.provider.invoices_error = BillingProviderUnavailable()
    before = row(s)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.small.id)})
    assert resp.status_code == 502
    unchanged(s, before)
    assert writes(s.provider) == []


# === POST /billing/subscription/cancel =========================================


def test_cancel_active_reconciles_first_then_cancels_at_the_cycle_end(s):
    s.sub("ACTIVE")
    s.remote(status="active", plan="plan_small", start=START)
    resp = post(s.client, CANCEL)
    assert resp.status_code == 200
    assert s.provider.names() == ["fetch_subscription", "cancel_subscription"]
    assert s.provider.calls[1] == ("cancel_subscription", REF, True)
    body = resp.json()
    assert body["cancel_at_period_end"] is True and body["status"] == "ACTIVE" and body["can_send"] is True
    assert body["next_action"]["type"] == "CANCELLATION"
    [audit] = audits(s, "billing.cancellation_requested")
    assert audit.actor_user_id == s.owner.user_id


def test_a_second_cancel_is_a_200_no_op_with_no_fetch_no_provider_call_and_no_audit_row(s):
    s.sub("ACTIVE")
    s.remote(status="active", plan="plan_small", start=START)
    assert post(s.client, CANCEL).status_code == 200
    calls_after_first = list(s.provider.calls)
    again = post(s.client, CANCEL)
    assert again.status_code == 200 and again.json()["cancel_at_period_end"] is True
    assert s.provider.calls == calls_after_first
    assert len(audits(s, "billing.cancellation_requested")) == 1


def test_an_already_cancelling_row_stays_a_200_even_if_the_provider_is_now_paused(s):
    s.sub("ACTIVE", cancel_at_period_end=True)
    s.remote(status="paused", plan="plan_small", start=START)
    resp = post(s.client, CANCEL)
    assert resp.status_code == 200
    assert s.provider.calls == []  # the no-op is decided before any reconcile
    assert audits(s) == []


def test_a_failed_reconcile_fetch_on_cancel_is_502_and_nothing_is_cancelled(s):
    s.sub("ACTIVE")
    s.provider.fetch_error = BillingProviderUnavailable()
    resp = post(s.client, CANCEL)
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "billing_provider_unavailable"
    assert s.provider.names() == ["fetch_subscription"]
    after = row(s)
    assert after.cancel_at_period_end is False and audits(s) == []


def test_a_permanent_4xx_on_the_cancel_fetch_is_502(s):
    s.sub("ACTIVE")
    s.provider.fetch_error = BillingProviderRejected("BAD_REQUEST_ERROR")
    assert post(s.client, CANCEL).status_code == 502
    assert s.provider.names() == ["fetch_subscription"] and row(s).cancel_at_period_end is False


@pytest.mark.parametrize("error", [BillingProviderUnavailable(), BillingProviderRejected("X")])
def test_a_failed_provider_cancel_on_active_is_502_and_changes_nothing(s, error):
    s.sub("ACTIVE")
    s.remote(status="active", plan="plan_small", start=START)
    s.provider.cancel_error = error
    resp = post(s.client, CANCEL)
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "billing_provider_unavailable"
    assert row(s).cancel_at_period_end is False and audits(s) == []


def test_active_with_a_paused_provider_is_409_nothing_is_cancelled_and_repeats_are_identical(s):
    s.sub("ACTIVE")
    s.remote(status="paused", plan="plan_small", start=START)
    for _ in range(2):
        resp = post(s.client, CANCEL)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "subscription_provider_state_unsupported"
    assert "cancel_subscription" not in s.provider.names()
    after = row(s)
    assert (after.status, after.cancel_at_period_end) == ("ACTIVE", False)
    assert after.provider_status == "paused"  # the reconcile snapshot only
    assert audits(s) == []


@pytest.mark.parametrize("pstatus", ["authenticated", "future_status"])
def test_the_paused_rule_is_not_extended_to_other_statuses(s, pstatus):
    s.sub("ACTIVE")
    s.remote(status=pstatus, plan="plan_small", start=START)
    resp = post(s.client, CANCEL)
    assert resp.status_code == 200
    assert ("cancel_subscription", REF, True) in s.provider.calls
    assert row(s).cancel_at_period_end is True


def test_a_reconcile_that_moves_the_row_to_past_due_follows_the_past_due_rule(s):
    s.sub("ACTIVE")
    s.remote(status="halted", plan="plan_small", start=START)
    resp = post(s.client, CANCEL)
    assert resp.status_code == 200 and resp.json()["status"] == "CANCELLED"
    assert ("cancel_subscription", REF, False) in s.provider.calls


def test_a_reconcile_that_finds_the_provider_cancelled_is_409_not_cancellable_and_persists(s):
    s.sub("ACTIVE")
    s.remote(status="cancelled", plan="plan_small", start=START)
    resp = post(s.client, CANCEL)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "subscription_not_cancellable"
    assert row(s).status == "CANCELLED"  # the snapshot survives the 409
    assert "cancel_subscription" not in s.provider.names()


def test_cancel_past_due_is_local_first_with_no_reconcile(s):
    s.sub("PAST_DUE", provider_status="halted")
    s.remote(status="halted")
    resp = post(s.client, CANCEL)
    assert resp.status_code == 200 and resp.json()["status"] == "CANCELLED"
    assert "fetch_subscription" not in s.provider.names()
    assert ("cancel_subscription", REF, False) in s.provider.calls
    after = row(s)
    assert (after.past_due_at, after.dunning_stage) == (None, None)
    assert after.provider_status == "cancelled"
    assert len(audits(s, "billing.cancellation_requested")) == 1


@pytest.mark.parametrize(
    "error", [BillingProviderUnavailable(), BillingProviderRejected("X")]
)
def test_cancel_past_due_survives_a_failing_provider(s, error):
    s.sub("PAST_DUE", provider_status="halted")
    s.provider.cancel_error = error
    resp = post(s.client, CANCEL)
    assert resp.status_code == 200 and resp.json()["status"] == "CANCELLED"
    assert row(s).provider_status == "halted"  # the cancel is owed to the sweep


@pytest.mark.parametrize("pstatus", ["paused", "authenticated", "future_status", "cancelled"])
def test_cancel_past_due_never_touches_a_provider_the_shared_rule_forbids(s, pstatus):
    s.sub("PAST_DUE", provider_status=pstatus)
    resp = post(s.client, CANCEL)
    assert resp.status_code == 200 and resp.json()["status"] == "CANCELLED"
    assert s.provider.calls == []


def test_cancel_past_due_with_no_reference_makes_no_provider_call(s):
    s.sub("PAST_DUE", ref=None)
    assert post(s.client, CANCEL).status_code == 200
    assert s.provider.calls == []


@pytest.mark.parametrize("local", [None, "INCOMPLETE", "CANCELLED", "EXPIRED"])
def test_cancel_in_a_state_that_cannot_be_cancelled_is_409_with_no_provider_call(s, local):
    if local:
        s.sub(local)
    resp = post(s.client, CANCEL)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "subscription_not_cancellable"
    assert s.provider.calls == []


def test_cancel_ignores_anything_in_the_body(s):
    s.sub("ACTIVE")
    s.remote(status="active", plan="plan_small", start=START)
    resp = post(s.client, CANCEL, {"status": "EXPIRED", "merchant_id": "x", "at_cycle_end": False})
    assert resp.status_code == 200
    assert s.provider.calls[-1] == ("cancel_subscription", REF, True)


def test_cancel_only_touches_the_callers_merchant(s, make_merchant, session_client):
    other = make_merchant("B")
    s.sub("ACTIVE")
    s.remote(status="active", plan="plan_small", start=START)
    s.sub("ACTIVE", ref="sub_b", merchant=other)
    s.remote(status="active", plan="plan_small", start=START, ref="sub_b")
    assert post(s.client, CANCEL).status_code == 200
    assert row(s).cancel_at_period_end is True
    assert row(s, other).cancel_at_period_end is False
    assert all(c[1] != "sub_b" for c in s.provider.calls)


def test_billing_writes_are_throttled_per_user(s):
    codes = [post(s.client, CANCEL).status_code for _ in range(11)]
    assert codes[:10] == [409] * 10 and codes[10] == 429


# === the two explicit open cases: the code stops, it does not decide =============


def _as_owner(s, fn, **kwargs):
    with tenant_context(s.owner.merchant_id), tenant_atomic():
        return fn(**kwargs)


@pytest.mark.parametrize("plan_key", ["big", "small"])
def test_checkout_on_an_active_row_with_no_provider_reference_is_409_and_nothing_happens(s, plan_key):
    """Decided 2026-10-01: no provider subscription exists to change."""
    s.sub("ACTIVE", ref=None)
    before = row(s)
    resp = post(s.client, CHECKOUT, {"plan_id": str(getattr(s, plan_key).id)})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "subscription_provider_state_unsupported"
    assert s.provider.calls == []
    unchanged(s, before)
    assert audits(s) == []
    again = post(s.client, CHECKOUT, {"plan_id": str(getattr(s, plan_key).id)})
    assert again.status_code == 409 and s.provider.calls == []


def test_cancel_on_an_active_row_with_no_provider_reference_is_409_and_nothing_happens(s):
    s.sub("ACTIVE", ref=None)
    before = row(s)
    for _ in range(2):
        resp = post(s.client, CANCEL)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "subscription_provider_state_unsupported"
    assert s.provider.calls == []
    unchanged(s, before)
    assert row(s).cancel_at_period_end is False and audits(s) == []


def test_an_already_cancelling_active_row_with_no_reference_keeps_the_200_no_op(s):
    """The existing idempotent no-op is checked before the no-reference rule."""
    s.sub("ACTIVE", ref=None, cancel_at_period_end=True)
    resp = post(s.client, CANCEL)
    assert resp.status_code == 200 and resp.json()["cancel_at_period_end"] is True
    assert s.provider.calls == [] and audits(s) == []


def test_a_past_due_row_with_no_reference_still_answers_past_due_on_checkout(s):
    s.sub("PAST_DUE", ref=None)
    resp = post(s.client, CHECKOUT, {"plan_id": str(s.big.id)})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "subscription_past_due"
    assert s.provider.calls == []


def test_open_case_the_reconcile_stops_at_the_unimplemented_invoice_fetch(s, monkeypatch):
    """An INCOMPLETE row whose provider is `active` needs the invoice list, and
    fetch_invoices is an explicit fail-closed stop (pagination not
    established). Nothing is written before it raises."""
    monkeypatch.setattr(razorpay, "fetch_invoices", ORIGINAL_FETCH_INVOICES)
    s.sub("INCOMPLETE", ref=REF)
    s.remote(status="active", plan="plan_small")
    before = row(s)
    with pytest.raises(NotImplementedError):
        _as_owner(s, services.start_checkout, actor=s.owner, plan_id=s.small.id)
    unchanged(s, before)
    assert writes(s.provider) == []


# === service guards ================================================================


@pytest.mark.django_db(transaction=True)
def test_checkout_and_cancel_refuse_to_run_outside_the_requests_transaction(s):
    """With no ambient transaction the savepoint phases would each commit, and
    the row lock would be released between them."""
    with tenant_context(s.owner.merchant_id):
        with pytest.raises(RuntimeError):
            services.start_checkout(actor=s.owner, plan_id=s.small.id)
        with pytest.raises(RuntimeError):
            services.cancel_subscription(actor=s.owner)
    assert s.provider.calls == []


def test_team_member_role_constants_are_what_the_overview_compares_against():
    assert TeamMember.Role.OWNER == "OWNER"
