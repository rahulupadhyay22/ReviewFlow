"""billing_ref_lookup RLS policy and core.tenancy.billing_ref_lookup_atomic
(spec 07 Change 1 -- LOCKED DECISION CHANGE, signed off). Mirrors
integrations/tests/test_lookup_rls.py."""
import pytest
from django.db import Error, connection, transaction

from billing.models import Subscription
from core.exceptions import TenantContextError
from core.tenancy import billing_ref_lookup_atomic, tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db(transaction=True)


def _ids(sql, params=()):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return {str(r[0]) for r in cur.fetchall()}


def _setting(name):
    with connection.cursor() as cur:
        cur.execute("SELECT current_setting(%s, true)", [name])
        return cur.fetchone()[0]


def test_lookup_select_returns_only_the_row_with_that_ref(make_merchant, make_plan, make_subscription):
    plan = make_plan()
    sub_a = make_subscription(make_merchant("A").merchant, plan, ref="sub_AAA")
    sub_b = make_subscription(make_merchant("B").merchant, plan, ref="sub_BBB")
    with billing_ref_lookup_atomic("sub_AAA"):
        ids = _ids("SELECT id FROM billing_subscription")
    assert ids == {str(sub_a.id)}
    assert str(sub_b.id) not in ids


def test_lookup_update_and_delete_affect_zero_rows(make_merchant, make_plan, make_subscription):
    owner = make_merchant("A")
    sub = make_subscription(owner.merchant, make_plan(), ref="sub_AAA")
    with billing_ref_lookup_atomic("sub_AAA"):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE billing_subscription SET provider_status = 'x' WHERE id = %s", [str(sub.id)]
            )
            assert cur.rowcount == 0
            cur.execute("DELETE FROM billing_subscription WHERE id = %s", [str(sub.id)])
            assert cur.rowcount == 0
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Subscription.objects.get(id=sub.id).provider_status is None


def test_lookup_insert_is_rejected(make_merchant, make_plan):
    """No write policy is keyed on app.current_billing_ref."""
    owner = make_merchant("A")
    plan = make_plan()
    with billing_ref_lookup_atomic("sub_NEW"):
        with pytest.raises(Error), transaction.atomic():
            Subscription(
                merchant=owner.merchant, plan=plan, status="INCOMPLETE", payment_provider_ref="sub_NEW"
            ).save(force_insert=True)


def test_lookup_other_billing_tables_are_invisible(make_merchant, make_plan, make_subscription):
    sub = make_subscription(make_merchant("A").merchant, make_plan(), ref="sub_AAA")
    assert sub
    with billing_ref_lookup_atomic("sub_AAA"):
        for table in ("billing_usagerecord", "billing_paymentattempt", "billing_billingevent"):
            assert _ids(f"SELECT id FROM {table}") == set(), table


def test_lookup_setting_is_reset_after_the_transaction(make_merchant, make_plan, make_subscription):
    make_subscription(make_merchant("A").merchant, make_plan(), ref="sub_AAA")
    with billing_ref_lookup_atomic("sub_AAA"):
        assert _setting("app.current_billing_ref") == "sub_AAA"
    assert _setting("app.current_billing_ref") in (None, "")


def test_lookup_setting_is_reset_after_a_failed_transaction():
    with pytest.raises(RuntimeError):
        with billing_ref_lookup_atomic("sub_AAA"):
            raise RuntimeError("boom")
    assert _setting("app.current_billing_ref") in (None, "")


def test_lookup_with_an_unknown_ref_sees_no_rows(make_merchant, make_plan, make_subscription):
    make_subscription(make_merchant("A").merchant, make_plan(), ref="sub_AAA")
    with billing_ref_lookup_atomic("sub_ZZZ"):
        assert _ids("SELECT id FROM billing_subscription") == set()


def test_lookup_raises_tenant_context_error_inside_a_merchant_context(make_merchant):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id):
        with pytest.raises(TenantContextError):
            with billing_ref_lookup_atomic("sub_AAA"):
                pass


def test_lookup_inside_tenant_atomic_raises_and_sets_nothing(make_merchant):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(TenantContextError):
            with billing_ref_lookup_atomic("sub_AAA"):
                pass
        assert _setting("app.current_billing_ref") in (None, "")


@pytest.mark.parametrize(
    "bad_ref",
    ["", None, "sub AAA", "sub-AAA", "sub_AAA" + chr(10), "x'; DROP TABLE billing_subscription; --", "s" * 65],
)
def test_lookup_raises_valueerror_for_a_ref_outside_the_pattern(bad_ref):
    with pytest.raises(ValueError):
        with billing_ref_lookup_atomic(bad_ref):
            pass


def test_lookup_accepts_a_64_character_ref():
    with billing_ref_lookup_atomic("s" * 64):
        pass


def test_lookup_raises_runtimeerror_when_nested_in_another_atomic():
    with transaction.atomic():
        with pytest.raises(RuntimeError):
            with billing_ref_lookup_atomic("sub_AAA"):
                pass
        assert _setting("app.current_billing_ref") in (None, "")


def test_for_lookup_ref_raises_outside_the_lookup_context():
    with pytest.raises(TenantContextError):
        Subscription.objects.for_lookup_ref("sub_AAA")


def test_for_lookup_ref_raises_for_a_different_ref():
    with billing_ref_lookup_atomic("sub_BBB"):
        with pytest.raises(TenantContextError):
            Subscription.objects.for_lookup_ref("sub_AAA")


def test_for_lookup_ref_returns_only_that_refs_row(make_merchant, make_plan, make_subscription):
    plan = make_plan()
    sub_a = make_subscription(make_merchant("A").merchant, plan, ref="sub_AAA")
    make_subscription(make_merchant("B").merchant, plan, ref="sub_BBB")
    with billing_ref_lookup_atomic("sub_AAA"):
        rows = list(Subscription.objects.for_lookup_ref("sub_AAA"))
    assert [r.id for r in rows] == [sub_a.id]
