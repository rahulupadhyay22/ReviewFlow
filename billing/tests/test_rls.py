"""RLS backstop for the four tenant billing tables (spec 07 Definition of
done "Models, migrations, RLS"; Multi-Tenancy.md §Layer 1/2). Mirrors
integrations/tests/test_rls.py."""
import uuid
from datetime import timedelta

import pytest
from django.db import Error, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone as dj_timezone

from billing.models import BillingEvent, PaymentAttempt, Subscription, UsageRecord
from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db(transaction=True)

TENANT_TABLES = [
    "billing_subscription",
    "billing_usagerecord",
    "billing_paymentattempt",
    "billing_billingevent",
]


def _ids(sql, params=()):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return {str(r[0]) for r in cur.fetchall()}


def _seed(merchant, plan, make_subscription):
    """A subscription, its usage record, a payment and an event for one merchant."""
    sub = make_subscription(merchant, plan)
    now = dj_timezone.now()
    with tenant_context(merchant.id), tenant_atomic():
        PaymentAttempt.objects.create(
            merchant=merchant,
            subscription=sub,
            provider="razorpay",
            provider_attempt_id=f"pay_{uuid.uuid4().hex[:12]}",
            attempt_type="RENEWAL",
            status="SUCCEEDED",
            attempted_at=now,
        )
        BillingEvent.objects.create(
            merchant=merchant,
            provider="razorpay",
            provider_event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type="subscription.charged",
            provider_ref=sub.payment_provider_ref,
        )
    return sub


def test_rls_enabled_forced_and_policies_match_the_spec():
    with connection.cursor() as cur:
        for table in TENANT_TABLES:
            cur.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s", [table]
            )
            assert cur.fetchone() == (True, True), table
            cur.execute("SELECT policyname, cmd FROM pg_policies WHERE tablename = %s", [table])
            policies = dict(cur.fetchall())
            if table == "billing_subscription":
                assert policies == {"tenant_isolation": "ALL", "billing_ref_lookup": "SELECT"}
            else:
                assert policies == {"tenant_isolation": "ALL"}, table
        # Plan is GLOBAL reference data: no policy, RLS not enabled.
        cur.execute("SELECT count(*) FROM pg_policies WHERE tablename = 'billing_plan'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'billing_plan'")
        assert cur.fetchone()[0] is False


def test_raw_select_in_a_merchant_context_sees_only_that_merchants_rows(
    make_merchant, make_plan, make_subscription
):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    plan = make_plan()
    _seed(owner_a.merchant, plan, make_subscription)
    _seed(owner_b.merchant, plan, make_subscription)

    for table in TENANT_TABLES:
        with tenant_context(owner_a.merchant_id), tenant_atomic():
            with connection.cursor() as cur:
                cur.execute(f"SELECT DISTINCT merchant_id FROM {table}")
                merchants = {str(r[0]) for r in cur.fetchall()}
        assert merchants == {str(owner_a.merchant_id)}, table


def test_raw_select_without_a_context_returns_no_rows(make_merchant, make_plan, make_subscription):
    owner = make_merchant("A")
    _seed(owner.merchant, make_plan(), make_subscription)
    for table in TENANT_TABLES:
        assert _ids(f"SELECT id FROM {table}") == set(), table


def test_raw_insert_for_another_merchant_fails_with_check(make_merchant, make_plan):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    plan = make_plan()
    now = dj_timezone.now()
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            UsageRecord.objects.create(
                merchant=owner_b.merchant, period_start=now, period_end=now + timedelta(days=30)
            )
        with pytest.raises(Error), transaction.atomic():
            Subscription.objects.create(
                merchant=owner_b.merchant,
                plan=plan,
                status="INCOMPLETE",
                payment_provider_ref="sub_other",
            )


@pytest.mark.parametrize("model", [Subscription, UsageRecord, PaymentAttempt, BillingEvent])
def test_manager_without_a_tenant_context_raises(model):
    with pytest.raises(TenantContextError):
        list(model.objects.all())


def test_manager_in_a_merchant_context_returns_only_that_merchants_rows(
    make_merchant, make_plan, make_subscription
):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    plan = make_plan()
    sub_a = _seed(owner_a.merchant, plan, make_subscription)
    _seed(owner_b.merchant, plan, make_subscription)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        assert {s.id for s in Subscription.objects.all()} == {sub_a.id}
        assert UsageRecord.objects.count() == 1


def test_billing_migrations_reverse_and_reapply_cleanly():
    """`migrate billing zero` reverses 0001 + 0002 (spec DoD)."""
    executor = MigrationExecutor(connection)
    leaf = executor.loader.graph.leaf_nodes("billing")
    try:
        executor.migrate([("billing", None)])
        with connection.cursor() as cur:
            cur.execute("SELECT to_regclass('billing_subscription')")
            assert cur.fetchone()[0] is None
        executor = MigrationExecutor(connection)
        executor.migrate(leaf)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT policyname FROM pg_policies WHERE tablename = 'billing_subscription'"
            )
            assert {r[0] for r in cur.fetchall()} == {"tenant_isolation", "billing_ref_lookup"}
    finally:
        MigrationExecutor(connection).migrate(leaf)
