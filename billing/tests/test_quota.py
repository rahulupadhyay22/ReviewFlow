"""Quota primitives (spec 07 Definition of done "Quota primitive";
Testing-Strategy.md "Billing / quota reservation"). Uses transaction=True:
reservations need real transaction boundaries and row locks.

Developer checks for W3; the formal feature-test pass is /test-feature."""
import threading
from datetime import timedelta

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone as dj_timezone

from billing import services
from billing.models import Subscription, UsageRecord
from billing.services import ReservationResult
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db(transaction=True)

R = ReservationResult


def _reserve(merchant):
    with tenant_context(merchant.id), tenant_atomic():
        return services.reserve_quota_unit()


def _usage(merchant):
    with tenant_context(merchant.id), tenant_atomic():
        return UsageRecord.objects.get().requests_used


def _set_used(merchant, n):
    with tenant_context(merchant.id), tenant_atomic():
        UsageRecord.objects.update(requests_used=n)


def test_reserve_increments_by_exactly_one_for_an_active_subscription(
    make_merchant, make_plan, make_subscription
):
    owner = make_merchant("A")
    make_subscription(owner.merchant, make_plan(quota=5))
    assert _reserve(owner.merchant) is R.RESERVED
    assert _usage(owner.merchant) == 1


def test_reserve_returns_quota_exhausted_and_increments_nothing_at_the_quota(
    make_merchant, make_plan, make_subscription
):
    owner = make_merchant("A")
    make_subscription(owner.merchant, make_plan(quota=3))
    _set_used(owner.merchant, 3)
    assert _reserve(owner.merchant) is R.QUOTA_EXHAUSTED
    assert _usage(owner.merchant) == 3


def test_reserve_outside_a_caller_transaction_is_refused(make_merchant, make_plan, make_subscription):
    owner = make_merchant("A")
    make_subscription(owner.merchant, make_plan())
    with tenant_context(owner.merchant.id):
        with pytest.raises(RuntimeError):
            services.reserve_quota_unit()


def test_reserve_with_no_subscription_row_is_not_entitled(make_merchant):
    owner = make_merchant("A")
    assert _reserve(owner.merchant) is R.NOT_ENTITLED


@pytest.mark.parametrize("status", ["INCOMPLETE", "PAST_DUE", "CANCELLED", "EXPIRED"])
def test_reserve_is_not_entitled_outside_active(make_merchant, make_plan, make_subscription, status):
    owner = make_merchant("A")
    make_subscription(owner.merchant, make_plan(), status=status)
    assert _reserve(owner.merchant) is R.NOT_ENTITLED
    with tenant_context(owner.merchant.id), tenant_atomic():
        assert not UsageRecord.objects.exclude(requests_used=0).exists()


def test_reserve_is_not_entitled_for_a_lapsed_period(make_merchant, make_plan, make_subscription):
    owner = make_merchant("A")
    now = dj_timezone.now()
    make_subscription(
        owner.merchant,
        make_plan(),
        current_period_start=now - timedelta(days=31),
        current_period_end=now - timedelta(seconds=1),
    )
    assert _reserve(owner.merchant) is R.NOT_ENTITLED
    assert _usage(owner.merchant) == 0


def test_reserve_never_creates_a_missing_usage_record(make_merchant, make_plan, make_subscription):
    owner = make_merchant("A")
    make_subscription(owner.merchant, make_plan(), usage=False)
    assert _reserve(owner.merchant) is R.NOT_ENTITLED
    with tenant_context(owner.merchant.id), tenant_atomic():
        assert UsageRecord.objects.count() == 0


def test_a_reservation_rolled_back_by_its_caller_leaves_usage_unchanged(
    make_merchant, make_plan, make_subscription
):
    owner = make_merchant("A")
    make_subscription(owner.merchant, make_plan(quota=5))

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with tenant_context(owner.merchant.id), tenant_atomic():
            assert services.reserve_quota_unit() is R.RESERVED
            raise Boom
    assert _usage(owner.merchant) == 0


def test_an_upgrade_is_effective_for_the_next_reservation_with_usage_carried_over(
    make_merchant, make_plan, make_subscription
):
    owner = make_merchant("A")
    small, big = make_plan("Starter", quota=2), make_plan("Growth", quota=10, price="1999.00")
    make_subscription(owner.merchant, small)
    _set_used(owner.merchant, 2)
    assert _reserve(owner.merchant) is R.QUOTA_EXHAUSTED

    with tenant_context(owner.merchant.id), tenant_atomic():
        Subscription.objects.update(plan=big)
    assert _reserve(owner.merchant) is R.RESERVED
    assert _usage(owner.merchant) == 3  # carried over, not reset


def test_n_threads_racing_for_one_remaining_unit_yield_exactly_one_reservation(
    make_merchant, make_plan, make_subscription
):
    owner = make_merchant("A")
    make_subscription(owner.merchant, make_plan(quota=5))
    _set_used(owner.merchant, 4)

    n = 8
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker():
        try:
            barrier.wait(timeout=10)
            results.append(_reserve(owner.merchant))
        except Exception as exc:  # captured for the assertion below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert sorted(results, key=str) == sorted([R.RESERVED] + [R.QUOTA_EXHAUSTED] * (n - 1), key=str)
    assert _usage(owner.merchant) == 5


def test_a_reservation_racing_a_transition_to_past_due_never_reserves_after_it_commits(
    make_merchant, make_plan, make_subscription
):
    """The holder takes the Subscription lock, then the UsageRecord lock (the
    transition lock order), and only then flips to PAST_DUE. A reservation
    that started while it held the locks must see the committed transition."""
    owner = make_merchant("A")
    sub = make_subscription(owner.merchant, make_plan(quota=5))
    locked, reserver_started = threading.Event(), threading.Event()
    outcome, errors = [], []

    def holder():
        try:
            with tenant_context(owner.merchant.id), tenant_atomic():
                locked_sub = services._lock_subscription(sub.pk)
                locked.set()
                reserver_started.wait(timeout=10)
                threading.Event().wait(0.5)  # let the reserver reach the usage lock
                locked_sub.status = Subscription.Status.PAST_DUE
                locked_sub.past_due_at = dj_timezone.now()
                locked_sub.dunning_stage = 0
                locked_sub.save()
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    def reserver():
        try:
            locked.wait(timeout=10)
            reserver_started.set()
            outcome.append(_reserve(owner.merchant))
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=holder), threading.Thread(target=reserver)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert outcome == [R.NOT_ENTITLED]
    assert _usage(owner.merchant) == 0


# --- get_entitlement ------------------------------------------------------


def _entitlement(merchant):
    with tenant_context(merchant.id):
        return services.get_entitlement()


def test_entitlement_reports_quota_usage_and_can_send_for_an_active_row(
    make_merchant, make_plan, make_subscription
):
    owner = make_merchant("A")
    plan = make_plan(quota=10)
    make_subscription(owner.merchant, plan)
    _set_used(owner.merchant, 4)
    e = _entitlement(owner.merchant)
    assert (e.status, e.quota_requests, e.requests_used, e.requests_remaining, e.can_send) == (
        "ACTIVE",
        10,
        4,
        6,
        True,
    )
    assert e.plan.pk == plan.pk


def test_entitlement_cannot_send_at_the_quota(make_merchant, make_plan, make_subscription):
    owner = make_merchant("A")
    make_subscription(owner.merchant, make_plan(quota=2))
    _set_used(owner.merchant, 5)  # a record may sit above a later, lower quota
    e = _entitlement(owner.merchant)
    assert e.requests_remaining == 0 and e.can_send is False


def test_entitlement_with_no_row(make_merchant):
    e = _entitlement(make_merchant("A").merchant)
    assert e.status is None and e.can_send is False and e.plan is None


@pytest.mark.parametrize("status", ["INCOMPLETE", "PAST_DUE", "CANCELLED", "EXPIRED"])
def test_entitlement_cannot_send_outside_active(make_merchant, make_plan, make_subscription, status):
    owner = make_merchant("A")
    make_subscription(owner.merchant, make_plan(), status=status)
    e = _entitlement(owner.merchant)
    assert e.status == status and e.can_send is False


def test_entitlement_cannot_send_for_a_lapsed_period_or_missing_record(
    make_merchant, make_plan, make_subscription
):
    now = dj_timezone.now()
    lapsed = make_merchant("A")
    make_subscription(
        lapsed.merchant,
        make_plan("Starter"),
        current_period_start=now - timedelta(days=31),
        current_period_end=now - timedelta(seconds=1),
    )
    assert _entitlement(lapsed.merchant).can_send is False

    no_record = make_merchant("B")
    make_subscription(no_record.merchant, make_plan("Growth"), usage=False)
    e = _entitlement(no_record.merchant)
    assert e.can_send is False and e.requests_used is None


def test_entitlement_takes_no_lock(make_merchant, make_plan, make_subscription):
    owner = make_merchant("A")
    make_subscription(owner.merchant, make_plan())
    with CaptureQueriesContext(connection) as ctx:
        _entitlement(owner.merchant)
    assert ctx.captured_queries
    assert not any("FOR UPDATE" in q["sql"].upper() for q in ctx.captured_queries)


# --- reset_usage_period ---------------------------------------------------


def test_the_usage_helper_is_idempotent_and_a_new_period_starts_at_zero_without_rollover(
    make_merchant, make_plan, make_subscription
):
    owner = make_merchant("A")
    sub = make_subscription(owner.merchant, make_plan(), usage=False)
    with tenant_context(owner.merchant.id):
        first = services.reset_usage_period(sub)
        again = services.reset_usage_period(sub)
    assert first.pk == again.pk
    _set_used(owner.merchant, 7)

    new_start = sub.current_period_end
    with tenant_context(owner.merchant.id), tenant_atomic():
        Subscription.objects.update(
            current_period_start=new_start, current_period_end=new_start + timedelta(days=30)
        )
        sub.refresh_from_db()
        second = services.reset_usage_period(sub)
        records = list(UsageRecord.objects.order_by("period_start"))
    assert second.requests_used == 0
    assert [r.requests_used for r in records] == [7, 0]  # the old record is untouched


def test_the_usage_helper_refuses_another_merchants_subscription(
    make_merchant, make_plan, make_subscription
):
    from core.exceptions import TenantContextError

    a, b = make_merchant("A"), make_merchant("B")
    sub_b = make_subscription(b.merchant, make_plan())
    with tenant_context(a.merchant.id):
        with pytest.raises(TenantContextError):
            services.reset_usage_period(sub_b)


def test_no_usage_is_written_outside_a_tenant_context(make_merchant, make_plan, make_subscription):
    from core.exceptions import TenantContextError

    sub = make_subscription(make_merchant("A").merchant, make_plan())
    with pytest.raises(TenantContextError):
        services.reset_usage_period(sub)
    with pytest.raises(TenantContextError):
        services.get_entitlement()
    with transaction.atomic(), pytest.raises(TenantContextError):
        services.reserve_quota_unit()
