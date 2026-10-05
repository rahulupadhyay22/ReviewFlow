"""RLS write isolation on all four tenant billing tables (spec 07 DoD "Models,
migrations, RLS"; Multi-Tenancy.md Layer 1). test_rls.py proves the WITH CHECK
backstop through the ORM for two tables; this file proves it with raw SQL on
every table, and proves cross-tenant raw UPDATE/DELETE touch nothing.

Every refusal is asserted by SQLSTATE 42501 (insufficient_privilege, which is
what a failed RLS WITH CHECK raises), never a bare Error: a raw statement that
missed a NOT NULL column or broke a CHECK would also raise and must not pass.
A positive control inserts the same row for the caller's own merchant.

Also pins the two partial/secondary indexes the spec lists. These tests verify
ReviewFlow's database schema only."""
import uuid

import pytest
from django.db import connection, transaction
from django.db.utils import Error

from billing.models import BillingEvent, PaymentAttempt, Subscription, UsageRecord
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db(transaction=True)

TABLES = ("billing_subscription", "billing_usagerecord", "billing_paymentattempt", "billing_billingevent")
RLS_REFUSED = "42501"


def _sqlstate(exc: BaseException) -> str | None:
    cause = exc.__cause__ or exc
    return getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)


def _insert(table, merchant_id, *, plan_id, subscription_id):
    """One complete raw INSERT (every NOT NULL column supplied) for `merchant_id`."""
    token = uuid.uuid4().hex[:12]
    base = "id, created_at, updated_at, merchant_id"
    head = "gen_random_uuid(), now(), now(), %s"
    if table == "billing_subscription":
        sql = (
            f"INSERT INTO billing_subscription ({base}, plan_id, status, cancel_at_period_end, payment_provider_ref) "
            f"VALUES ({head}, %s, 'INCOMPLETE', false, %s)"
        )
        params = [merchant_id, plan_id, f"sub_raw{token}"]
    elif table == "billing_usagerecord":
        sql = (
            f"INSERT INTO billing_usagerecord ({base}, period_start, period_end, requests_used) "
            f"VALUES ({head}, now(), now() + interval '30 days', 0)"
        )
        params = [merchant_id]
    elif table == "billing_paymentattempt":
        sql = (
            f"INSERT INTO billing_paymentattempt ({base}, subscription_id, provider, provider_attempt_id, "
            f"attempt_type, status, attempted_at) VALUES ({head}, %s, 'razorpay', %s, 'RENEWAL', 'SUCCEEDED', now())"
        )
        params = [merchant_id, subscription_id, f"pay_raw{token}"]
    else:
        sql = (
            f"INSERT INTO billing_billingevent ({base}, provider, provider_event_id, event_type, provider_ref) "
            f"VALUES ({head}, 'razorpay', %s, 'subscription.charged', %s)"
        )
        params = [merchant_id, f"evt_raw{token}", f"sub_raw{token}"]
    return sql, params


def _seed(merchant, plan, make_subscription):
    """A subscription (with its usage record), a payment and an event."""
    sub = make_subscription(merchant, plan)
    with tenant_context(merchant.id), tenant_atomic():
        PaymentAttempt.objects.create(
            merchant=merchant,
            subscription=sub,
            provider="razorpay",
            provider_attempt_id=f"pay_{uuid.uuid4().hex[:12]}",
            attempt_type="RENEWAL",
            status="SUCCEEDED",
            attempted_at=sub.current_period_start,
        )
        BillingEvent.objects.create(
            merchant=merchant,
            provider="razorpay",
            provider_event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type="subscription.charged",
            provider_ref=sub.payment_provider_ref,
        )
    return sub


def _counts(merchant):
    with tenant_context(merchant.id), tenant_atomic():
        return {
            "billing_subscription": Subscription.objects.count(),
            "billing_usagerecord": UsageRecord.objects.count(),
            "billing_paymentattempt": PaymentAttempt.objects.count(),
            "billing_billingevent": BillingEvent.objects.count(),
        }


@pytest.fixture
def two_merchants(make_merchant, make_plan, make_subscription):
    class S:
        pass

    s = S()
    s.a, s.b = make_merchant("A").merchant, make_merchant("B").merchant
    s.plan = make_plan()
    return s


@pytest.mark.parametrize("table", TABLES)
def test_a_raw_insert_naming_another_merchant_is_refused_by_with_check(
    table, two_merchants, make_subscription
):
    s = two_merchants
    sub_a = None
    if table != "billing_subscription":
        sub_a = _seed(s.a, s.plan, make_subscription)  # A needs a parent row for the payment
    before_b = _counts(s.b)
    sub_id = sub_a.id if sub_a else None

    with tenant_context(s.a.id), tenant_atomic():
        sql, params = _insert(table, s.b.id, plan_id=s.plan.id, subscription_id=sub_id)
        with connection.cursor() as cur, pytest.raises(Error) as refused, transaction.atomic():
            cur.execute(sql, params)
        assert _sqlstate(refused.value) == RLS_REFUSED, refused.value

        # Positive control: the very same statement for A's own merchant works,
        # so the refusal above was RLS and nothing else.
        sql, params = _insert(table, s.a.id, plan_id=s.plan.id, subscription_id=sub_id)
        with connection.cursor() as cur:
            cur.execute(sql, params)
            assert cur.rowcount == 1

    assert _counts(s.b) == before_b  # nothing was written for B


@pytest.mark.parametrize("table", TABLES)
def test_a_raw_update_cannot_move_a_row_to_another_merchant(table, two_merchants, make_subscription):
    s = two_merchants
    _seed(s.a, s.plan, make_subscription)
    before_b = _counts(s.b)
    with tenant_context(s.a.id), tenant_atomic():
        with connection.cursor() as cur, pytest.raises(Error) as refused, transaction.atomic():
            cur.execute(f"UPDATE {table} SET merchant_id = %s WHERE merchant_id = %s", [s.b.id, s.a.id])
        assert _sqlstate(refused.value) == RLS_REFUSED, refused.value
    assert _counts(s.b) == before_b


@pytest.mark.parametrize("table", TABLES)
def test_a_raw_update_or_delete_aimed_at_another_merchants_rows_affects_nothing(
    table, two_merchants, make_subscription
):
    s = two_merchants
    _seed(s.a, s.plan, make_subscription)
    _seed(s.b, s.plan, make_subscription)
    before_b = _counts(s.b)
    assert before_b[table] == 1

    with tenant_context(s.a.id), tenant_atomic():
        with connection.cursor() as cur:
            cur.execute(f"UPDATE {table} SET updated_at = now() WHERE merchant_id = %s", [s.b.id])
            assert cur.rowcount == 0
            cur.execute(f"DELETE FROM {table} WHERE merchant_id = %s", [s.b.id])
            assert cur.rowcount == 0
            cur.execute(f"SELECT count(*) FROM {table} WHERE merchant_id = %s", [s.b.id])
            assert cur.fetchone()[0] == 0

    assert _counts(s.b) == before_b  # B's rows are intact


@pytest.mark.parametrize("table", TABLES)
def test_a_raw_write_with_no_tenant_context_matches_nothing_and_inserts_are_refused(
    table, two_merchants, make_subscription
):
    s = two_merchants
    sub = _seed(s.a, s.plan, make_subscription)
    before = _counts(s.a)
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(f"UPDATE {table} SET updated_at = now()")
        assert cur.rowcount == 0
        cur.execute(f"DELETE FROM {table}")
        assert cur.rowcount == 0
        sql, params = _insert(table, s.a.id, plan_id=s.plan.id, subscription_id=sub.id)
        with pytest.raises(Error) as refused, transaction.atomic():
            cur.execute(sql, params)
        assert _sqlstate(refused.value) == RLS_REFUSED, refused.value
    assert _counts(s.a) == before


def test_the_secondary_and_partial_indexes_the_spec_lists_exist():
    with connection.cursor() as cur:
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN (%s, %s)",
            ["billing_be_unprocessed_idx", "billing_pa_merchant_at_idx"],
        )
        defs = dict(cur.fetchall())
    assert set(defs) == {"billing_be_unprocessed_idx", "billing_pa_merchant_at_idx"}
    assert "processed_at IS NULL" in defs["billing_be_unprocessed_idx"]
    assert "merchant_id" in defs["billing_pa_merchant_at_idx"] and "attempted_at" in defs["billing_pa_merchant_at_idx"]
