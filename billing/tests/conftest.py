"""Feature-local fixtures for billing tests (Fixture Rules: pytest fixtures do
not cross app test-package boundaries). Razorpay is never called: tests patch
billing.razorpay at its module boundary."""
import itertools
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone as dj_timezone
from rest_framework.test import APIClient

from accounts import services as account_services
from accounts.models import TeamMember, User
from billing import services, tasks
from billing.models import Plan, Subscription, UsageRecord
from billing.tests import snapshot_helpers, webhook_helpers
from core.tenancy import tenant_atomic, tenant_context

PASSWORD = "Tr1cky-Horse-Battery-Staple!"


@pytest.fixture(autouse=True)
def local_cache(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def make_merchant():
    counter = itertools.count(1)

    def _make(name="Merchant", email=None):
        n = next(counter)
        return account_services.create_merchant_with_owner(
            name=f"{name} {n}",
            timezone="Asia/Kolkata",
            owner_email=email or f"owner{n}@example.com",
            owner_password=PASSWORD,
        )

    return _make


@pytest.fixture
def add_member():
    def _add(merchant, role, email, *, accepted=True):
        user = User.objects.create_user(email, PASSWORD)
        with tenant_context(merchant.id), tenant_atomic():
            return TeamMember.objects.create(
                merchant=merchant,
                user=user,
                role=role,
                accepted_at=dj_timezone.now() if accepted else None,
            )

    return _add


@pytest.fixture
def csrf_client():
    return APIClient(enforce_csrf_checks=True)


@pytest.fixture
def make_plan():
    """Plan is GLOBAL: no tenant context needed."""
    counter = itertools.count(1)

    def _make(name="Starter", *, price="999.00", quota=100, provider_plan_id="__auto__", **kwargs):
        n = next(counter)
        if provider_plan_id == "__auto__":
            provider_plan_id = f"plan_test{n}"
        return Plan.objects.create(
            name=name,
            monthly_price=price,
            quota_requests=quota,
            provider_plan_id=provider_plan_id,
            **kwargs,
        )

    return _make


@pytest.fixture
def make_subscription():
    """Creates a Subscription (and, for ACTIVE/PAST_DUE, its UsageRecord)
    directly in the database, as test setup only -- production code only ever
    reaches ACTIVE through apply_provider_snapshot()."""
    counter = itertools.count(1)

    def _make(merchant, plan, *, status="ACTIVE", ref="__auto__", usage=True, **kwargs):
        n = next(counter)
        if ref == "__auto__":
            ref = f"sub_test{n}"
        now = dj_timezone.now()
        if status == "INCOMPLETE":
            kwargs.setdefault("current_period_start", None)
            kwargs.setdefault("current_period_end", None)
        else:
            kwargs.setdefault("current_period_start", now - timedelta(days=10))
            kwargs.setdefault("current_period_end", now + timedelta(days=20))
        if status == "PAST_DUE":
            kwargs.setdefault("past_due_at", now)
            kwargs.setdefault("dunning_stage", 0)
        with tenant_context(merchant.id), tenant_atomic():
            sub = Subscription.objects.create(
                merchant=merchant, plan=plan, status=status, payment_provider_ref=ref, **kwargs
            )
            if usage and sub.current_period_start is not None:
                UsageRecord.objects.create(
                    merchant=merchant,
                    period_start=sub.current_period_start,
                    period_end=sub.current_period_end,
                )
        return sub

    return _make


@pytest.fixture
def login():
    def _login(email, password=PASSWORD, client=None):
        client = client or APIClient(enforce_csrf_checks=True)
        client.get("/api/v1/auth/login")
        token = client.cookies["csrftoken"].value
        resp = client.post(
            "/api/v1/auth/login",
            {"email": email, "password": password},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        return client, resp

    return _login


@pytest.fixture
def session_client(login):
    def _make(email):
        client, resp = login(email)
        assert resp.status_code == 200, resp.content
        client.csrf = client.cookies["csrftoken"].value
        return client

    return _make


@pytest.fixture
def make_key():
    from apikeys import services as apikey_services

    def _make(owner, scopes=("sales:write",)):
        with tenant_context(owner.merchant_id):
            return apikey_services.create_api_key(actor=owner, scopes=list(scopes))

    return _make


class FakeProvider:
    """billing.razorpay replaced at its module boundary: records every call and
    answers from `entities`. No network, ever. Queue errors on the *_error
    attributes."""

    def __init__(self):
        self.calls = []
        self.entities = {}  # ref -> entity dict
        self.invoices = []
        self.fetch_error = None
        self.invoices_error = None
        self.create_error = None
        self.create_entity = None  # override the id/status Razorpay "returns"
        self.update_error = None
        self.update_extra = {}  # merged into the entity an update returns (e.g. a new period)
        self.cancel_error = None
        self._created = 0

    # -- helpers for tests
    def names(self):
        return [c[0] for c in self.calls]

    def count(self, name):
        return self.names().count(name)

    def add(self, ref, **fields):
        self.entities[ref] = {"id": ref, "status": "created", **fields}
        return self.entities[ref]

    # -- the razorpay surface
    def fetch_subscription(self, ref):
        self.calls.append(("fetch_subscription", ref))
        if self.fetch_error:
            raise self.fetch_error
        return self.entities[ref]

    def fetch_invoices(self, ref):
        self.calls.append(("fetch_invoices", ref))
        if self.invoices_error:
            raise self.invoices_error
        return self.invoices

    def create_subscription(self, provider_plan_id):
        self.calls.append(("create_subscription", provider_plan_id))
        if self.create_error:
            raise self.create_error
        if self.create_entity is not None:
            return self.create_entity
        self._created += 1
        ref = f"sub_created{self._created:04d}"
        return self.add(ref, plan_id=provider_plan_id)

    def update_subscription(self, ref, provider_plan_id, schedule_change_at):
        self.calls.append(("update_subscription", ref, provider_plan_id, schedule_change_at))
        if self.update_error:
            raise self.update_error
        self.entities[ref] = {
            **self.entities[ref],
            "plan_id": provider_plan_id,
            **self.update_extra,
        }
        return self.entities[ref]

    def cancel_subscription(self, ref, at_cycle_end):
        self.calls.append(("cancel_subscription", ref, at_cycle_end))
        if self.cancel_error:
            raise self.cancel_error
        if ref in self.entities and not at_cycle_end:
            self.entities[ref] = {**self.entities[ref], "status": "cancelled"}
        return self.entities.get(ref, {"id": ref, "status": "cancelled"})


@pytest.fixture
def provider(monkeypatch, settings):
    from billing import razorpay

    settings.RAZORPAY_KEY_ID = "rzp_test_keyid"
    settings.RAZORPAY_KEY_SECRET = "key_secret_value"
    fake = FakeProvider()
    for name in (
        "fetch_subscription",
        "fetch_invoices",
        "create_subscription",
        "update_subscription",
        "cancel_subscription",
    ):
        monkeypatch.setattr(razorpay, name, getattr(fake, name))
    return fake


# --- Razorpay webhook tests (W6): opt in with usefixtures("webhook_secret") ---


@pytest.fixture
def webhook_secret(settings):
    settings.RAZORPAY_WEBHOOK_SECRET = webhook_helpers.SECRET


@pytest.fixture
def webhook_setup(make_merchant, make_plan, make_subscription, provider):
    """Merchant A holds the webhook REF (INCOMPLETE, as after checkout);
    merchant B holds another subscription. Razorpay (fake) reports REF active
    and paid."""

    class S:
        pass

    s = S()
    s.a, s.b = make_merchant("A"), make_merchant("B")
    s.plan = make_plan("Starter", provider_plan_id=webhook_helpers.PLAN_ID)
    s.sub_a = make_subscription(s.a.merchant, s.plan, status="INCOMPLETE", ref=webhook_helpers.REF)
    s.sub_b = make_subscription(s.b.merchant, make_plan("Growth", price="1999.00"), status="ACTIVE", ref="sub_B")
    s.provider = provider
    provider.add(
        webhook_helpers.REF,
        status="active",
        plan_id=webhook_helpers.PLAN_ID,
        current_start=webhook_helpers.START,
        current_end=webhook_helpers.END,
    )
    provider.invoices = [
        {
            "id": "inv_Test1",
            "subscription_id": webhook_helpers.REF,
            "status": "paid",
            "amount_due": 0,
            "payment_id": "pay_TestPay0000001",
            "billing_start": webhook_helpers.START,
            "billing_end": webhook_helpers.END,
            "paid_at": webhook_helpers.START + 60,
        }
    ]
    return s


@pytest.fixture
def queued(monkeypatch):
    """The sync task's .delay replaced by a recorder (merchant ids): tasks are
    queued, then run by the test, so every ordering is chosen, not raced."""
    calls = []
    monkeypatch.setattr(tasks.sync_subscription, "delay", lambda merchant_id: calls.append(merchant_id))
    return calls


@pytest.fixture
def real_clock_skew():
    """Request this to keep services.EVENT_CLOCK_SKEW at its production value.
    Every other billing test runs with the margin at zero (below)."""
    return services.EVENT_CLOCK_SKEW


@pytest.fixture(autouse=True)
def no_clock_skew_margin(monkeypatch, request):
    """The production margin (5 s) keeps an event received moments before a
    fetch from being marked processed. Tests that deliver and sync inline
    would never see a marked event, so they run with the margin at zero; the
    margin itself is tested explicitly with the real_clock_skew fixture."""
    if "real_clock_skew" not in request.fixturenames:
        monkeypatch.setattr(services, "EVENT_CLOCK_SKEW", timedelta(0))


@pytest.fixture
def lifecycle_setup(make_merchant, make_plan, make_subscription, provider):
    """Two merchants (A, B), a small and a big plan, the FakeProvider, and a
    subscription builder. A's provider ref is snapshot_helpers.REF, B's is
    "sub_test_b"; `ref(owner)` returns the right one."""

    class S:
        pass

    s = S()
    s.a, s.b = make_merchant("A"), make_merchant("B")
    s.small = make_plan("Starter", quota=100, provider_plan_id="plan_small")
    s.big = make_plan("Growth", quota=500, price="1999.00", provider_plan_id="plan_big")
    s.provider = provider
    refs = {s.a.merchant.id: snapshot_helpers.REF, s.b.merchant.id: "sub_test_b"}

    def ref(owner):
        return refs.get(owner.merchant.id) or f"sub_test_{owner.merchant.id.hex[:12]}"

    def sub(owner, status="ACTIVE", plan=None, **kwargs):
        kwargs.setdefault("ref", ref(owner))
        if status != "INCOMPLETE":
            kwargs.setdefault("current_period_start", snapshot_helpers.START)
            kwargs.setdefault("current_period_end", snapshot_helpers.END)
        if status == "PAST_DUE":
            kwargs.setdefault("dunning_stage", 0)
            kwargs.setdefault("provider_status", "halted")
        return make_subscription(owner.merchant, plan or s.small, status=status, **kwargs)

    s.ref, s.sub = ref, sub
    return s
