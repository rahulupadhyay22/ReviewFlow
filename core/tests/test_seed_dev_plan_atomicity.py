"""seed_dev's four development plans are created all-or-nothing: a failure while
creating one must not leave a partial set (code-review item, spec 07 Decision 10)."""
import pytest

from accounts import services
from billing.models import Plan, Subscription
from core.management.commands.seed_dev import DEV_PLANS, seed_billing
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def make_dev_merchant():
    return services.create_merchant_with_owner(
        name="Atomic Cafe", timezone="Asia/Kolkata", owner_email="o@atomic.example.com", owner_password="Tr1cky-Horse-Battery-Staple!"
    ).merchant


def test_a_failed_plan_creation_leaves_no_partial_set(make_dev_merchant, monkeypatch):
    real_create = Plan.objects.create
    calls = []

    def flaky(**kwargs):
        calls.append(kwargs["name"])
        if len(calls) == 3:
            raise RuntimeError("boom")
        return real_create(**kwargs)

    monkeypatch.setattr(Plan.objects, "create", flaky)
    with pytest.raises(RuntimeError):
        seed_billing(make_dev_merchant)
    assert Plan.objects.count() == 0  # the first two were rolled back
    with tenant_context(make_dev_merchant.id), tenant_atomic():
        assert Subscription.objects.count() == 0

    monkeypatch.undo()
    assert seed_billing(make_dev_merchant) is True  # a re-run completes the set
    assert Plan.objects.count() == len(DEV_PLANS)
