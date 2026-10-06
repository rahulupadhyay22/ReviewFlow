"""W4 of 07-plan-change-replacement: the replacement switch and abandon against
concurrent work, with real threads and real transactions (thread_helpers.race:
the first thread holds the real Subscription row lock, the second is observed
waiting on it in pg_stat_activity before the first is released; no sleeps).

Each case ends in one consistent state: one switch, one abandon, one new
UsageRecord. Switch versus a merchant cancel and the commit versus a merchant
cancel belong to W6 and are not here."""
import pytest

from billing import services, tasks
from billing.tests.replacement_helpers import REPL_REF, provide_replacement, set_replacement
from billing.tests.snapshot_helpers import START, entity
from billing.tests.state_helpers import read_state
from billing.tests.thread_helpers import race
from core.tenancy import tenant_context

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def w(lifecycle_setup):
    return lifecycle_setup


def maintain(w):
    return lambda: tasks.maintain_subscription(w.a.merchant.id)


def replacement_step(w):
    def run():
        with tenant_context(w.a.merchant.id):
            services._sync_replacement()

    return run


def main_step(w):
    def run():
        with tenant_context(w.a.merchant.id):
            services._sync_main()

    return run


def paid_upgrade(w, *, status="ACTIVE"):
    w.sub(w.a, status, plan=w.small)
    set_replacement(w.a.merchant)
    provide_replacement(w.provider, w.big)
    return read_state(w.a.merchant)


def test_two_sweeps_race_one_switch_and_one_new_usage_period(w, monkeypatch):
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START)
    initial = paid_upgrade(w)
    race(monkeypatch, first=maintain(w), second=maintain(w))
    state = read_state(w.a.merchant)
    assert state.status == "ACTIVE"
    assert state.audits == ["billing.plan_changed"]  # one switch
    assert len(state.payments) == 1  # one ledger entry
    assert len(state.usages) == len(initial.usages) + 1  # one fresh UsageRecord
    assert (state.replacement_provider_ref, state.retired_kind) == (None, None)  # old cancelled, slot cleared


@pytest.mark.parametrize("order", ["switch_first", "past_due_first"])
def test_switch_versus_the_past_due_transition_ends_active_on_the_new_plan(w, monkeypatch, order):
    """The old ref goes `halted` while the replacement is paid. Switch first: the
    old-ref snapshot is stale and dropped. PAST_DUE first: the transition keeps
    the `active` replacement and the switch then recovers the row."""
    initial = paid_upgrade(w)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="halted")
    switch, transition = replacement_step(w), main_step(w)
    first, second = (switch, transition) if order == "switch_first" else (transition, switch)
    race(monkeypatch, first=first, second=second)

    state = read_state(w.a.merchant)
    assert state.status == "ACTIVE"
    assert (state.past_due_at, state.dunning_stage) == (None, None)
    assert state.retired_kind == "SWITCHED_OLD" and state.replacement_provider_ref is None
    assert len(state.usages) == len(initial.usages) + 1
    assert len(state.payments) == 1
    if order == "switch_first":
        assert state.audits == ["billing.plan_changed"]
    else:
        assert state.audits == [
            "billing.subscription_past_due",
            "billing.subscription_recovered",
            "billing.plan_changed",
        ]


def test_two_sweeps_race_one_abandon(w, monkeypatch):
    w.sub(w.a, "ACTIVE", plan=w.small)
    set_replacement(w.a.merchant)
    provide_replacement(w.provider, w.big, status="halted", paid=False)
    w.provider.entities[w.ref(w.a)] = entity(w.small, status="active", start=START)
    race(monkeypatch, first=maintain(w), second=maintain(w))
    state = read_state(w.a.merchant)
    assert state.audits == ["billing.replacement_abandoned"]
    assert (state.retired_provider_ref, state.retired_kind) == (REPL_REF, "ABANDONED_REPLACEMENT")
    assert state.replacement_provider_ref is None and state.status == "ACTIVE"
