"""billing.services.get_merchant_plan_summary (spec 07 Decision 5): `{id, name}`
of the subscription's plan for ACTIVE and PAST_DUE, else None, and a hard
TenantContextError unless the merchant being described is the active tenant.
None never means "could not tell"."""
import uuid

import pytest

from billing import services
from core.exceptions import TenantContextError
from core.tenancy import tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(lifecycle_setup):
    return lifecycle_setup


def summary(owner, merchant_id=None):
    with tenant_context(owner.merchant.id):
        return services.get_merchant_plan_summary(merchant_id or owner.merchant.id)


def test_no_subscription_is_none(w):
    assert summary(w.a) is None


@pytest.mark.parametrize("status", ["ACTIVE", "PAST_DUE"])
def test_active_and_past_due_return_the_plan(w, status):
    w.sub(w.a, status, plan=w.big)
    assert summary(w.a) == {"id": w.big.id, "name": "Growth"}


@pytest.mark.parametrize("status", ["INCOMPLETE", "CANCELLED", "EXPIRED"])
def test_every_other_status_is_none(w, status):
    w.sub(w.a, status)
    assert summary(w.a) is None


def test_the_pending_plan_is_never_reported_only_the_plan_in_force(w):
    w.sub(w.a, "ACTIVE", plan=w.big, pending_plan=w.small)
    assert summary(w.a)["name"] == "Growth"


def test_a_string_merchant_id_is_accepted_for_the_active_tenant(w):
    w.sub(w.a, "ACTIVE")
    assert summary(w.a, str(w.a.merchant.id))["name"] == "Starter"


def test_no_tenant_context_raises(w):
    with pytest.raises(TenantContextError):
        services.get_merchant_plan_summary(w.a.merchant.id)


def test_a_different_merchants_context_raises_and_never_reads_the_other_row(w):
    w.sub(w.a, "ACTIVE")
    w.sub(w.b, "ACTIVE", plan=w.big)
    with pytest.raises(TenantContextError):
        summary(w.a, w.b.merchant.id)  # context A, asked about B
    with pytest.raises(TenantContextError):
        summary(w.b, w.a.merchant.id)  # context B, asked about A
    with pytest.raises(TenantContextError):
        summary(w.a, uuid.uuid4())  # context A, asked about nobody


def test_it_makes_no_provider_call(w):
    w.sub(w.a, "ACTIVE")
    summary(w.a)
    assert w.provider.calls == []
