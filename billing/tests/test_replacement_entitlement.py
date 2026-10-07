"""07-plan-change-replacement, "Quota and usage" and "Downgrade boundary": the old
plan, quota and UsageRecord are untouched until the switch; after the old period end
a row without proof is not entitled (no grace); a replacement switch opens a fresh
UsageRecord at 0 and leaves the old period's record alone (a provider-update upgrade
that keeps consumed usage is pinned in test_api.py
test_an_upgrade_keeps_consumed_usage_when_the_period_is_unchanged). Razorpay is the
FakeProvider; D1 is not worked around (no invoice means no proof)."""
from datetime import datetime, timedelta, timezone

import pytest
from django.utils import timezone as dj_timezone

from billing import services
from billing.models import UsageRecord
from billing.tests.replacement_helpers import NEW_START, provide_replacement, set_replacement
from billing.tests.snapshot_helpers import END, START, entity, ts
from billing.tests.state_helpers import read_state
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

R = services.ReservationResult
NEW_END = NEW_START + timedelta(days=30)


@pytest.fixture
def w(lifecycle_setup):
    return lifecycle_setup


def unix(value) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def consume(owner, used):
    with tenant_context(owner.merchant.id), tenant_atomic():
        UsageRecord.objects.update(requests_used=used)


def entitlement(owner):
    with tenant_context(owner.merchant.id):
        return services.get_entitlement()


def reserve(owner):
    with tenant_context(owner.merchant.id), tenant_atomic():
        return services.reserve_quota_unit()


def test_a_pending_replacement_leaves_the_old_plan_quota_and_usage_untouched(w):
    w.sub(w.a, "ACTIVE", plan=w.small)  # quota 100
    consume(w.a, 30)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START)
    set_replacement(w.a.merchant, plan=w.big)
    provide_replacement(w.provider, w.big, status="authenticated", paid=False)
    before = read_state(w.a.merchant)

    with tenant_context(w.a.merchant.id):
        services.sync_subscription()  # the sweep reads the replacement and the old subscription

    ent = entitlement(w.a)
    assert (ent.plan.pk, ent.quota_requests, ent.requests_used, ent.can_send) == (w.small.pk, 100, 30, True)
    assert (ent.period_start, ent.period_end) == (unix(ts(START)), unix(ts(END)))
    after = read_state(w.a.merchant)
    assert (after.usages, after.payments, after.audits, after.period) == (
        before.usages,
        before.payments,
        before.audits,
        before.period,
    )
    assert reserve(w.a) is R.RESERVED  # a send reserves against the OLD record and quota
    assert read_state(w.a.merchant).usages == [(unix(ts(START)), unix(ts(END)), 31)]


@pytest.mark.parametrize("committed", [False, True])
def test_after_the_old_period_end_a_row_without_proof_is_not_entitled(w, committed):
    """Q1: no grace is added. The replacement is `active` at the provider but there is
    no qualifying paid invoice, so nothing switches and nothing is granted."""
    now = dj_timezone.now()
    w.sub(
        w.a,
        "ACTIVE",
        plan=w.big,
        current_period_start=now - timedelta(days=31),
        current_period_end=now - timedelta(hours=1),
    )
    set_replacement(w.a.merchant, committed=committed, downgrade_to=w.small)
    provide_replacement(w.provider, w.small, status="active", paid=False)
    before = read_state(w.a.merchant)

    with tenant_context(w.a.merchant.id):
        services._sync_replacement()

    after = read_state(w.a.merchant)
    assert (after.status, after.period, after.usages, after.audits, after.payments) == (
        before.status,
        before.period,
        before.usages,
        before.audits,
        before.payments,
    )
    assert after.replacement_provider_ref is not None  # still pending: the row was not switched
    ent = entitlement(w.a)
    assert ent.plan.pk == w.big.pk and ent.can_send is False
    assert reserve(w.a) is R.NOT_ENTITLED


def test_a_replacement_upgrade_switch_opens_a_fresh_usage_period_at_zero_and_keeps_the_old_record(w):
    w.sub(w.a, "ACTIVE", plan=w.small)
    consume(w.a, 30)
    old_usage = (unix(ts(START)), unix(ts(END)), 30)
    set_replacement(w.a.merchant, plan=w.big)
    provide_replacement(w.provider, w.big)  # active and paid, for a new period

    with tenant_context(w.a.merchant.id):
        services._sync_replacement()

    state = read_state(w.a.merchant)
    assert state.usages == [old_usage, (unix(ts(NEW_START)), unix(ts(NEW_END)), 0)]  # no rollover
    ent = entitlement(w.a)
    assert (ent.plan.pk, ent.quota_requests, ent.requests_used, ent.requests_remaining, ent.can_send) == (
        w.big.pk,
        500,
        0,
        500,
        True,
    )
    assert reserve(w.a) is R.RESERVED
    assert read_state(w.a.merchant).usages[0] == old_usage  # the old period's record is never touched again
