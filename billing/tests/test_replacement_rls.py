"""W1 of 07-plan-change-replacement: the extended SELECT-only billing_ref_lookup
policy (migration 0004), Subscription.objects.for_lookup_ref matching either
ref, tenant isolation of the new columns, and the 0003/0004 reverse round
trip. LOCKED DECISION CHANGE (approved in principle 2026-10-05; formal sign-off
at PR time): the policy matches payment_provider_ref OR replacement_provider_ref
and stays SELECT-only."""
from datetime import timedelta

import pytest
from django.db import Error, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone as dj_timezone

from billing.models import Subscription
from core.exceptions import TenantContextError
from core.tenancy import billing_ref_lookup_atomic, tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db(transaction=True)


def _later():
    return dj_timezone.now() + timedelta(days=1)


def _ids(sql, params=()):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return {str(r[0]) for r in cur.fetchall()}


def _policy_qual():
    with connection.cursor() as cur:
        cur.execute(
            "SELECT qual FROM pg_policies WHERE tablename = 'billing_subscription' "
            "AND policyname = 'billing_ref_lookup'"
        )
        return cur.fetchone()[0]


@pytest.fixture
def pair(make_merchant, make_plan, make_subscription):
    """A has a replacement and a retired subscription; B is unrelated."""
    plan = make_plan()
    a = make_subscription(
        make_merchant("A").merchant,
        plan,
        ref="sub_AAA",
        replacement_provider_ref="sub_RRR",
        replacement_plan=plan,
        replacement_expires_at=_later(),
        retired_provider_ref="sub_OLD",
        retired_kind="SWITCHED_OLD",
    )
    b = make_subscription(make_merchant("B").merchant, plan, ref="sub_BBB")
    return a, b


def test_the_table_keeps_exactly_two_policies_and_the_lookup_is_select_only(pair):
    with connection.cursor() as cur:
        cur.execute("SELECT policyname, cmd FROM pg_policies WHERE tablename = 'billing_subscription'")
        assert dict(cur.fetchall()) == {"tenant_isolation": "ALL", "billing_ref_lookup": "SELECT"}


def test_the_lookup_policy_matches_the_payment_ref_or_the_replacement_ref_and_nothing_else(pair):
    qual = _policy_qual()
    assert "payment_provider_ref" in qual and "replacement_provider_ref" in qual
    assert "retired_provider_ref" not in qual


def test_lookup_by_the_payment_ref_still_returns_only_that_row(pair):
    a, b = pair
    with billing_ref_lookup_atomic("sub_AAA"):
        assert _ids("SELECT id FROM billing_subscription") == {str(a.id)}


def test_lookup_by_the_replacement_ref_returns_that_row_and_no_other(pair):
    a, b = pair
    with billing_ref_lookup_atomic("sub_RRR"):
        assert _ids("SELECT id FROM billing_subscription") == {str(a.id)}


def test_lookup_by_the_retired_ref_or_an_unknown_ref_returns_nothing(pair):
    for ref in ("sub_OLD", "sub_NOPE"):
        with billing_ref_lookup_atomic(ref):
            assert _ids("SELECT id FROM billing_subscription") == set(), ref


def test_without_a_lookup_context_nothing_is_visible(pair):
    assert _ids("SELECT id FROM billing_subscription") == set()


def test_lookup_by_the_replacement_ref_cannot_update_or_delete(pair):
    a, b = pair
    with billing_ref_lookup_atomic("sub_RRR"):
        with connection.cursor() as cur:
            cur.execute("UPDATE billing_subscription SET provider_status = 'x' WHERE id = %s", [str(a.id)])
            assert cur.rowcount == 0
            cur.execute("DELETE FROM billing_subscription WHERE id = %s", [str(a.id)])
            assert cur.rowcount == 0
    with tenant_context(a.merchant_id), tenant_atomic():
        row = Subscription.objects.get(id=a.id)
    assert row.provider_status is None and row.replacement_provider_ref == "sub_RRR"


def test_lookup_by_the_replacement_ref_cannot_insert(make_merchant, make_plan):
    owner = make_merchant("A")
    plan = make_plan()
    with billing_ref_lookup_atomic("sub_NEW"):
        with pytest.raises(Error), transaction.atomic():
            Subscription(
                merchant=owner.merchant,
                plan=plan,
                status="INCOMPLETE",
                payment_provider_ref="sub_X",
                replacement_provider_ref="sub_NEW",
                replacement_plan=plan,
                replacement_expires_at=_later(),
            ).save(force_insert=True)


# --- the manager mirrors the policy ---------------------------------------------------


def test_for_lookup_ref_matches_either_ref_and_nothing_else(pair):
    a, b = pair
    for ref in ("sub_AAA", "sub_RRR"):
        with billing_ref_lookup_atomic(ref):
            assert {s.id for s in Subscription.objects.for_lookup_ref(ref)} == {a.id}, ref
    for ref in ("sub_OLD", "sub_BBB_not"):
        with billing_ref_lookup_atomic(ref):
            assert list(Subscription.objects.for_lookup_ref(ref)) == [], ref


def test_for_lookup_ref_still_requires_the_lookup_context_for_that_same_ref(pair):
    with pytest.raises(TenantContextError):
        Subscription.objects.for_lookup_ref("sub_RRR")
    with billing_ref_lookup_atomic("sub_AAA"):
        with pytest.raises(TenantContextError):
            Subscription.objects.for_lookup_ref("sub_RRR")  # a different ref


# --- tenant isolation of the new columns ----------------------------------------------


def test_a_tenant_can_write_its_own_replacement_columns_and_never_anothers(pair):
    a, b = pair
    with tenant_context(a.merchant_id), tenant_atomic(), connection.cursor() as cur:
        cur.execute(
            "UPDATE billing_subscription SET replacement_committed_at = now() WHERE id = %s", [str(a.id)]
        )
        assert cur.rowcount == 1  # its own row
        cur.execute(
            "UPDATE billing_subscription SET replacement_provider_ref = 'sub_STOLEN', "
            "replacement_expires_at = now() WHERE id = %s",
            [str(b.id)],
        )
        assert cur.rowcount == 0  # B's row is invisible to A
    with tenant_context(b.merchant_id), tenant_atomic():
        assert Subscription.objects.get(id=b.id).replacement_provider_ref is None


def test_a_tenant_cannot_insert_a_row_for_another_merchant_with_replacement_columns(
    make_merchant, make_plan
):
    a, b = make_merchant("A"), make_merchant("B")
    plan = make_plan()
    with tenant_context(a.merchant_id), tenant_atomic():
        with pytest.raises(Error), transaction.atomic():
            Subscription(
                merchant=b.merchant,
                plan=plan,
                status="INCOMPLETE",
                payment_provider_ref="sub_X",
                replacement_provider_ref="sub_Y",
                replacement_plan=plan,
                replacement_expires_at=_later(),
            ).save(force_insert=True)


# --- migrations 0003 and 0004 reverse and reapply -------------------------------------


def _columns():
    with connection.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'billing_subscription'"
        )
        return {r[0] for r in cur.fetchall()}


def test_0003_to_0005_reverse_and_reapply_with_the_new_columns_empty(pair):
    """`migrate billing 0002` reverses 0004 then 0003 (the new columns are dropped,
    so their data is discarded; this test runs with them empty, per the spec).
    Applying them again restores the columns and the two-column policy."""
    a, b = pair
    # The fixture's rows use the new columns; empty them first (the stated precondition).
    with tenant_context(a.merchant_id), tenant_atomic(), connection.cursor() as cur:
        cur.execute(
            "UPDATE billing_subscription SET replacement_provider_ref = NULL, replacement_plan_id = NULL, "
            "replacement_expires_at = NULL, retired_provider_ref = NULL, retired_kind = NULL "
            "WHERE id = %s",
            [str(a.id)],
        )
    new = {
        "replacement_provider_ref",
        "replacement_plan_id",
        "replacement_expires_at",
        "replacement_committed_at",
        "replacement_cancel_confirmed_at",
        "retired_provider_ref",
        "retired_kind",
    }
    leaf = MigrationExecutor(connection).loader.graph.leaf_nodes("billing")
    try:
        MigrationExecutor(connection).migrate([("billing", "0002_rls")])
        assert not (new & _columns())
        qual = _policy_qual()
        assert "replacement_provider_ref" not in qual and "payment_provider_ref" in qual
        with connection.cursor() as cur:
            cur.execute("SELECT policyname FROM pg_policies WHERE tablename = 'billing_subscription'")
            assert {r[0] for r in cur.fetchall()} == {"tenant_isolation", "billing_ref_lookup"}

        MigrationExecutor(connection).migrate(leaf)
        assert new <= _columns()
        assert "replacement_provider_ref" in _policy_qual()
        with connection.cursor() as cur:
            cur.execute("SELECT policyname FROM pg_policies WHERE tablename = 'billing_subscription'")
            assert {r[0] for r in cur.fetchall()} == {"tenant_isolation", "billing_ref_lookup"}
    finally:
        MigrationExecutor(connection).migrate(leaf)
