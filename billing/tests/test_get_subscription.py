"""billing.services.get_subscription (spec 07 "billing/services.py": the
tenant's Subscription or None, with its plan and pending plan loaded). It is part
of the approved service contract for later phases, so its behavior is pinned."""
import pytest

from billing import services
from core.exceptions import TenantContextError
from core.tenancy import tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(lifecycle_setup):
    return lifecycle_setup


def test_none_without_a_subscription(w):
    with tenant_context(w.a.merchant.id):
        assert services.get_subscription() is None


def test_returns_only_the_active_tenants_row_with_plans_loaded(w, django_assert_num_queries):
    sub = w.sub(w.a, "ACTIVE", plan=w.big, pending_plan=w.small)
    other = w.sub(w.b, "ACTIVE")
    with tenant_context(w.a.merchant.id):
        got = services.get_subscription()
        # plan and pending_plan arrive with the row (select_related): reading them
        # costs no further query. The call's own total is not asserted, because
        # tenant_atomic() adds its savepoint and SET LOCAL statements.
        with django_assert_num_queries(0):
            assert (got.plan.name, got.pending_plan.name) == ("Growth", "Starter")
    assert got.id == sub.id and got.merchant_id == w.a.merchant.id
    assert got.id != other.id  # the other tenant's subscription is never returned

    with tenant_context(w.b.merchant.id):
        assert services.get_subscription().id == other.id  # and the reverse


def test_requires_a_tenant_context(w):
    with pytest.raises(TenantContextError):
        services.get_subscription()
