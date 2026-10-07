"""W3 of 07-plan-change-replacement: the FakeProvider's per-ref machinery and the
replacement helpers. This tests test infrastructure on purpose: later phases lean
on these semantics, and a fake that quietly differs from its documentation would
make their tests lie. No provider behavior is claimed; the fake only does what
each test sets it up to do.

Semantics under test: a per-ref map wins over the global switch for that ref, the
global switch keeps working for every other ref, and the call tuples keep the
shape the merged tests assert."""
import pytest

from billing.exceptions import BillingProviderRejected, BillingProviderUnavailable
from billing.tests.replacement_helpers import (
    REPL_REF,
    RETIRED_REF,
    clear_replacement,
    replacement_entity,
    set_replacement,
    set_retired,
)
from billing.tests.snapshot_helpers import REF, entity, invoice
from billing.tests.state_helpers import read_state

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(lifecycle_setup):
    return lifecycle_setup


# --- per-ref entities -----------------------------------------------------------------


def test_each_ref_returns_its_own_entity(w):
    fake = w.provider
    fake.entities[REF] = entity(w.small, status="active")
    fake.entities[REPL_REF] = replacement_entity(w.big, status="authenticated")
    assert fake.fetch_subscription(REF)["status"] == "active"
    repl = fake.fetch_subscription(REPL_REF)
    assert (repl["id"], repl["status"], repl["plan_id"]) == (REPL_REF, "authenticated", w.big.provider_plan_id)


# --- per-ref fetch errors -------------------------------------------------------------


def test_a_per_ref_fetch_error_affects_only_that_ref(w):
    fake = w.provider
    fake.entities[REF] = entity(w.small)
    fake.entities[REPL_REF] = replacement_entity(w.big)
    fake.fetch_errors[REPL_REF] = BillingProviderUnavailable()
    with pytest.raises(BillingProviderUnavailable):
        fake.fetch_subscription(REPL_REF)
    assert fake.fetch_subscription(REF)["id"] == REF


def test_a_per_ref_fetch_error_wins_over_the_global_one_for_that_ref(w):
    fake = w.provider
    fake.entities[REF] = entity(w.small)
    fake.fetch_error = BillingProviderUnavailable()
    fake.fetch_errors[REF] = BillingProviderRejected("PER_REF")
    with pytest.raises(BillingProviderRejected):
        fake.fetch_subscription(REF)


def test_the_global_fetch_error_still_applies_to_every_other_ref(w):
    fake = w.provider
    fake.entities[REPL_REF] = replacement_entity(w.big)
    fake.fetch_error = BillingProviderUnavailable()
    with pytest.raises(BillingProviderUnavailable):
        fake.fetch_subscription(REPL_REF)


# --- per-ref invoices -----------------------------------------------------------------


def test_invoices_are_per_ref_with_the_shared_list_as_the_fallback(w):
    fake = w.provider
    fake.invoices = [invoice()]
    repl_invoice = invoice(subscription_id=REPL_REF, id="inv_repl")
    fake.invoices_by_ref[REPL_REF] = [repl_invoice]
    assert fake.fetch_invoices(REPL_REF) == [repl_invoice]
    assert fake.fetch_invoices(REF) == fake.invoices  # the merged shared behavior


def test_a_per_ref_invoice_error_affects_only_that_ref(w):
    fake = w.provider
    fake.invoices = [invoice()]
    fake.invoices_errors[REPL_REF] = BillingProviderUnavailable()
    with pytest.raises(BillingProviderUnavailable):
        fake.fetch_invoices(REPL_REF)
    assert fake.fetch_invoices(REF) == fake.invoices


def test_the_global_invoice_error_still_applies_to_every_ref(w):
    fake = w.provider
    fake.invoices_error = BillingProviderUnavailable()
    for ref in (REF, REPL_REF):
        with pytest.raises(BillingProviderUnavailable):
            fake.fetch_invoices(ref)


# --- per-ref cancel errors --------------------------------------------------------------


def test_a_per_ref_cancel_error_affects_only_that_ref(w):
    fake = w.provider
    fake.entities[REF] = entity(w.small)
    fake.entities[RETIRED_REF] = replacement_entity(w.small, ref=RETIRED_REF, status="active")
    fake.cancel_errors[RETIRED_REF] = BillingProviderUnavailable()
    with pytest.raises(BillingProviderUnavailable):
        fake.cancel_subscription(RETIRED_REF, False)
    assert fake.cancel_subscription(REF, False)["status"] == "cancelled"


def test_the_global_cancel_error_still_applies_to_every_ref(w):
    fake = w.provider
    fake.entities[REF] = entity(w.small)
    fake.cancel_error = BillingProviderUnavailable()
    with pytest.raises(BillingProviderUnavailable):
        fake.cancel_subscription(REF, True)


# --- the call log keeps its merged shape ----------------------------------------------------


def test_call_tuples_keep_their_merged_shape(w):
    fake = w.provider
    fake.entities[REF] = entity(w.small)
    fake.fetch_subscription(REF)
    fake.fetch_invoices(REF)
    fake.cancel_subscription(REF, True)
    fake.create_subscription("plan_x", start_at=1, expire_by=2)
    assert fake.calls == [
        ("fetch_subscription", REF),
        ("fetch_invoices", REF),
        ("cancel_subscription", REF, True),
        ("create_subscription", "plan_x"),
    ]
    assert fake.created_kwargs == [{"start_at": 1, "expire_by": 2}]


# --- the helpers write the documented columns ---------------------------------------------


def test_set_replacement_writes_the_upgrade_columns(w):
    w.sub(w.a, "ACTIVE")
    set_replacement(w.a.merchant)
    state = read_state(w.a.merchant)
    assert state.replacement_provider_ref == REPL_REF
    assert state.replacement_expires_at is not None
    assert state.replacement_committed_at is None and state.retired_provider_ref is None


def test_set_replacement_for_a_downgrade_sets_pending_plan_and_a_commit_marker(w):
    from billing.models import Subscription
    from core.tenancy import tenant_atomic, tenant_context

    w.sub(w.a, "ACTIVE", plan=w.big)
    set_replacement(w.a.merchant, committed=True, downgrade_to=w.small)
    assert read_state(w.a.merchant).replacement_committed_at is not None
    with tenant_context(w.a.merchant.id), tenant_atomic():
        assert Subscription.objects.get().pending_plan_id == w.small.id


def test_set_retired_and_clear_replacement(w):
    w.sub(w.a, "ACTIVE")
    set_replacement(w.a.merchant)
    set_retired(w.a.merchant, kind="ABANDONED_REPLACEMENT")
    clear_replacement(w.a.merchant)
    state = read_state(w.a.merchant)
    assert state.replacement_provider_ref is None and state.replacement_expires_at is None
    assert (state.retired_provider_ref, state.retired_kind) == (RETIRED_REF, "ABANDONED_REPLACEMENT")
