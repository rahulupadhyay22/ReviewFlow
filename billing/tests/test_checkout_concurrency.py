"""Concurrent first checkouts (spec 07 Definition of done: "Two concurrent
first checkouts leave one row and one usable ref"). Real threads, real
transactions: the INCOMPLETE row is inserted first, so UNIQUE(merchant) makes
the loser wait, then find the row, and no second provider subscription is
created."""
import threading

import pytest
from django.db import connection

from billing import services
from billing.models import Subscription
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db(transaction=True)


def test_two_concurrent_first_checkouts_leave_one_row_one_ref_and_one_provider_create(
    make_merchant, make_plan, provider
):
    owner = make_merchant("A")
    plan = make_plan("Starter", price="999.00", quota=100, provider_plan_id="plan_small")
    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker():
        try:
            barrier.wait(timeout=10)
            with tenant_context(owner.merchant_id), tenant_atomic():
                results.append(services.start_checkout(actor=owner, plan_id=plan.id))
        except Exception as exc:  # captured for the assertion below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert sorted(r.created for r in results) == [False, True]
    refs = {r.checkout["subscription_id"] for r in results}
    assert len(refs) == 1
    assert provider.count("create_subscription") == 1
    with tenant_context(owner.merchant_id), tenant_atomic():
        [row] = list(Subscription.objects.all())
    assert row.payment_provider_ref == refs.pop() and row.status == "INCOMPLETE"


def test_a_failed_first_create_releases_the_row_so_the_next_attempt_can_proceed(
    make_merchant, make_plan, provider
):
    from billing.exceptions import BillingProviderUnavailable

    owner = make_merchant("A")
    plan = make_plan("Starter", price="999.00", quota=100, provider_plan_id="plan_small")
    provider.create_error = BillingProviderUnavailable()
    with pytest.raises(BillingProviderUnavailable):
        with tenant_context(owner.merchant_id), tenant_atomic():
            services.start_checkout(actor=owner, plan_id=plan.id)
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Subscription.objects.count() == 0  # the whole attempt rolled back

    provider.create_error = None
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert services.start_checkout(actor=owner, plan_id=plan.id).created is True
