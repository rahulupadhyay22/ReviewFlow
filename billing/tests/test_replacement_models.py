"""W1 of 07-plan-change-replacement: the six new nullable Subscription columns
and the seven named constraints ("Models & database changes"). Each constraint
has a test that violates it and one that satisfies it. Nothing writes these
columns yet outside tests; they are all NULL by default.

Not in the spec's "Files to create" list (it names only the RLS file for W1);
mirrors the existing test_models.py, which holds the merged constraint tests."""
from datetime import timedelta

import pytest
from django.db import IntegrityError, connection
from django.utils import timezone as dj_timezone

from billing.models import Subscription
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db(transaction=True)

NEW_COLUMNS = (
    "replacement_provider_ref",
    "replacement_plan",
    "replacement_expires_at",
    "replacement_committed_at",
    "replacement_cancel_confirmed_at",
    "retired_provider_ref",
    "retired_kind",
)
NEW_CONSTRAINTS = {
    "billing_subscription_replacement_ref_uniq",
    "billing_subscription_retired_ref_uniq",
    "billing_subscription_replacement_ref_differs",
    "billing_subscription_replacement_expiry_set",
    "billing_subscription_replacement_plan_set",
    "billing_subscription_replacement_committed_needs_ref",
    "billing_subscription_retired_kind_set",
    "billing_subscription_replacement_confirmed_needs_committed",
}


def _later():
    return dj_timezone.now() + timedelta(days=1)


def _replacement(plan, **extra):
    """A valid replacement: a ref, its target plan and its required deadline."""
    return {
        "replacement_provider_ref": "sub_repl1",
        "replacement_plan": plan,
        "replacement_expires_at": _later(),
        **extra,
    }


def test_the_six_columns_exist_and_are_null_by_default(make_merchant, make_plan, make_subscription):
    sub = make_subscription(make_merchant("A").merchant, make_plan(), ref="sub_A")
    with tenant_context(sub.merchant_id), tenant_atomic():
        row = Subscription.objects.get(pk=sub.pk)
    for column in NEW_COLUMNS:
        assert getattr(row, column) is None, column


def test_exactly_the_new_constraints_exist_on_the_table():
    """Five CHECK constraints are pg_constraint rows; the two conditional (partial)
    unique constraints are unique indexes in PostgreSQL."""
    unique_indexes = {
        "billing_subscription_replacement_ref_uniq",
        "billing_subscription_retired_ref_uniq",
    }
    with connection.cursor() as cur:
        cur.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'billing_subscription'::regclass "
            "AND contype = 'c' AND (conname LIKE 'billing_subscription_replacement_%%' "
            "OR conname LIKE 'billing_subscription_retired_%%')"
        )
        checks = {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'billing_subscription' "
            "AND indexdef LIKE 'CREATE UNIQUE INDEX%%' "
            "AND (indexname LIKE 'billing_subscription_replacement_%%' "
            "OR indexname LIKE 'billing_subscription_retired_%%')"
        )
        uniques = {r[0] for r in cur.fetchall()}
    assert checks == NEW_CONSTRAINTS - unique_indexes
    assert uniques == unique_indexes
    assert checks | uniques == NEW_CONSTRAINTS


# --- partial uniqueness ------------------------------------------------------------


def test_replacement_ref_is_unique_when_set_and_null_repeats(make_merchant, make_plan, make_subscription):
    plan = make_plan()
    make_subscription(make_merchant("A").merchant, plan, ref="sub_A", **_replacement(plan))
    make_subscription(make_merchant("B").merchant, plan, ref="sub_B")  # NULL beside a set value
    make_subscription(make_merchant("C").merchant, plan, ref="sub_C")  # NULL repeats
    with pytest.raises(IntegrityError):
        make_subscription(make_merchant("D").merchant, plan, ref="sub_D", **_replacement(plan))


def test_retired_ref_is_unique_when_set_and_null_repeats(make_merchant, make_plan, make_subscription):
    plan = make_plan()
    retired = {"retired_provider_ref": "sub_ret1", "retired_kind": "SWITCHED_OLD"}
    make_subscription(make_merchant("A").merchant, plan, ref="sub_A", **retired)
    make_subscription(make_merchant("B").merchant, plan, ref="sub_B")
    make_subscription(make_merchant("C").merchant, plan, ref="sub_C")
    with pytest.raises(IntegrityError):
        make_subscription(make_merchant("D").merchant, plan, ref="sub_D", **retired)


# --- replacement_ref_differs -------------------------------------------------------


def test_replacement_ref_must_differ_from_the_payment_ref(make_merchant, make_plan, make_subscription):
    plan = make_plan()
    with pytest.raises(IntegrityError):
        make_subscription(
            make_merchant("A").merchant, plan, ref="sub_same", **_replacement(plan, replacement_provider_ref="sub_same")
        )
    make_subscription(make_merchant("B").merchant, plan, ref="sub_B", **_replacement(plan))  # satisfied


# --- replacement_expiry_set --------------------------------------------------------


def test_replacement_expiry_is_set_exactly_when_the_ref_is(make_merchant, make_plan, make_subscription):
    plan = make_plan()
    with pytest.raises(IntegrityError):  # ref without a deadline
        make_subscription(make_merchant("A").merchant, plan, ref="sub_A", replacement_provider_ref="sub_repl1", replacement_plan=plan)
    with pytest.raises(IntegrityError):  # a deadline without a ref
        make_subscription(make_merchant("B").merchant, plan, ref="sub_B", replacement_expires_at=_later())
    make_subscription(make_merchant("C").merchant, plan, ref="sub_C", **_replacement(plan))  # both set
    make_subscription(make_merchant("D").merchant, plan, ref="sub_D")  # both NULL


# --- replacement_plan_set (W5 amendment) ---------------------------------------------


def test_the_replacement_plan_is_set_exactly_when_the_ref_is(make_merchant, make_plan, make_subscription):
    plan = make_plan()
    with pytest.raises(IntegrityError):  # a ref and a deadline, no target plan
        make_subscription(
            make_merchant("A").merchant,
            plan,
            ref="sub_A",
            replacement_provider_ref="sub_repl1",
            replacement_expires_at=_later(),
        )
    with pytest.raises(IntegrityError):  # a target plan, no ref
        make_subscription(make_merchant("B").merchant, plan, ref="sub_B", replacement_plan=plan)
    make_subscription(make_merchant("C").merchant, plan, ref="sub_C", **_replacement(plan))  # all set


# --- replacement_committed_needs_ref -----------------------------------------------


def test_a_commit_marker_needs_a_replacement_ref(make_merchant, make_plan, make_subscription):
    plan = make_plan()
    with pytest.raises(IntegrityError):
        make_subscription(
            make_merchant("A").merchant, plan, ref="sub_A", replacement_committed_at=dj_timezone.now()
        )
    make_subscription(
        make_merchant("B").merchant,
        plan,
        ref="sub_B",
        **_replacement(plan, replacement_committed_at=dj_timezone.now()),
    )


# --- retired_kind_set --------------------------------------------------------------


def test_retired_kind_is_set_exactly_when_the_retired_ref_is(make_merchant, make_plan, make_subscription):
    plan = make_plan()
    with pytest.raises(IntegrityError):  # ref without a kind
        make_subscription(make_merchant("A").merchant, plan, ref="sub_A", retired_provider_ref="sub_ret1")
    with pytest.raises(IntegrityError):  # a kind without a ref
        make_subscription(make_merchant("B").merchant, plan, ref="sub_B", retired_kind="SWITCHED_OLD")
    make_subscription(
        make_merchant("C").merchant,
        plan,
        ref="sub_C",
        retired_provider_ref="sub_ret1",
        retired_kind="SWITCHED_OLD",
    )
    make_subscription(
        make_merchant("D").merchant,
        plan,
        ref="sub_D",
        retired_provider_ref="sub_ret2",
        retired_kind="ABANDONED_REPLACEMENT",
    )


# --- replacement_confirmed_needs_committed -----------------------------------------


def test_a_confirmed_cancel_needs_a_commit_marker(make_merchant, make_plan, make_subscription):
    plan = make_plan()
    with pytest.raises(IntegrityError):  # confirmed, never committed
        make_subscription(
            make_merchant("A").merchant,
            plan,
            ref="sub_A",
            **_replacement(plan, replacement_cancel_confirmed_at=dj_timezone.now()),
        )
    now = dj_timezone.now()
    make_subscription(
        make_merchant("B").merchant,
        plan,
        ref="sub_B",
        **_replacement(plan, replacement_committed_at=now, replacement_cancel_confirmed_at=now),
    )


def test_retired_kind_choices_are_the_two_documented_values():
    assert {c.value for c in Subscription.RetiredKind} == {"SWITCHED_OLD", "ABANDONED_REPLACEMENT"}
