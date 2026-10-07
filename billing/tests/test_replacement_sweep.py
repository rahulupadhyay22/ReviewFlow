"""07-plan-change-replacement, Definition of done "Webhook and sweep": a row with a
replacement or retired ref is due, and the sweep task processes the replacement, the
main and the retired subscription in that order. The no-eta/countdown/retry rule for
billing/tasks.py is pinned for the whole module by test_tasks.py
(test_no_task_has_retry_configuration_and_nothing_uses_eta_or_countdown). Razorpay is
the FakeProvider."""
import pytest

from billing import tasks
from billing.models import Subscription
from billing.tests.replacement_helpers import (
    REPL_REF,
    RETIRED_REF,
    replacement_entity,
    set_replacement,
    set_retired,
)
from billing.tests.snapshot_helpers import START, entity
from billing.tests.state_helpers import read_state
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(lifecycle_setup):
    return lifecycle_setup


@pytest.fixture
def delays(monkeypatch):
    calls = []

    def recorder(name):
        return lambda *args, **kwargs: calls.append((name, args))

    monkeypatch.setattr(tasks.maintain_subscription, "delay", recorder("maintain"))
    monkeypatch.setattr(tasks.advance_subscription_dunning, "delay", recorder("dunning"))
    return calls


def test_the_beat_sweep_enqueues_a_healthy_row_only_when_it_has_a_replacement_or_retired_ref(
    w, make_merchant, delays, django_capture_on_commit_callbacks
):
    third = make_merchant("C")
    for owner in (w.a, w.b, third):
        w.sub(owner, "ACTIVE")  # period in the future: nothing else makes these due
    set_replacement(w.a.merchant)
    set_retired(w.b.merchant)

    with django_capture_on_commit_callbacks(execute=True):
        tasks.run_billing_maintenance()

    def tasks_for(owner):
        return [name for name, args in delays if args == (owner.merchant.id,)]

    assert tasks_for(w.a) == ["maintain", "dunning"]  # only a replacement ref
    assert tasks_for(w.b) == ["maintain", "dunning"]  # only a retired ref
    assert tasks_for(third) == []  # neither: not due


def test_the_maintenance_task_processes_the_replacement_then_the_main_then_the_retired_ref(w):
    w.sub(w.a, "ACTIVE", plan=w.small)
    old = w.ref(w.a)
    w.provider.entities[old] = entity(w.small, status="active", start=START)
    set_replacement(w.a.merchant, plan=w.big)
    w.provider.entities[REPL_REF] = replacement_entity(w.big, status="created")  # awaiting authorization
    set_retired(w.a.merchant)  # SWITCHED_OLD
    w.provider.entities[RETIRED_REF] = entity(w.small, status="active", ref=RETIRED_REF, start=START)

    tasks.maintain_subscription(w.a.merchant.id)

    fetched = [c[1] for c in w.provider.calls if c[0] == "fetch_subscription"]
    assert fetched == [REPL_REF, old, RETIRED_REF]
    cancels = [(i, c) for i, c in enumerate(w.provider.calls) if c[0] == "cancel_subscription"]
    assert [c for _, c in cancels] == [("cancel_subscription", RETIRED_REF, False)]  # an active SWITCHED_OLD
    assert cancels[0][0] > w.provider.calls.index(("fetch_subscription", old))  # after the main step
    state = read_state(w.a.merchant)
    assert (state.retired_provider_ref, state.retired_kind) == (None, None)  # confirmed terminal: slot cleared
    assert state.replacement_provider_ref == REPL_REF  # `created` inside its window: left pending
    assert state.status == "ACTIVE"


def test_the_maintenance_task_never_touches_another_merchants_replacement_or_retired_ref(w):
    for owner in (w.a, w.b):
        w.sub(owner, "ACTIVE", plan=w.small)
        w.provider.entities[w.ref(owner)] = entity(w.small, status="active", start=START, ref=w.ref(owner))
    set_replacement(w.b.merchant, ref="sub_b_repl", plan=w.big)
    set_retired(w.b.merchant, ref="sub_b_retired")

    tasks.maintain_subscription(w.a.merchant.id)

    assert all(c[1] not in ("sub_b_repl", "sub_b_retired", w.ref(w.b)) for c in w.provider.calls)
    with tenant_context(w.b.merchant.id), tenant_atomic():
        row = Subscription.objects.get()
        assert (row.replacement_provider_ref, row.retired_provider_ref) == ("sub_b_repl", "sub_b_retired")


# --- sync_subscription: replacement -> main -> retired, each later step runs even if an earlier one raises ------------


@pytest.mark.parametrize("failing", [None, "replacement", "main", "retired"])
def test_every_later_step_runs_even_when_an_earlier_one_raises(monkeypatch, failing):
    """The nested try/finally in services.sync_subscription: the order is fixed, no step
    is skipped by an earlier failure, and a failure still propagates. (When two steps
    raise, the later exception is the one that propagates; that is a known, deferred
    follow-up and is deliberately not pinned here.)"""
    from billing import services

    ran = []

    def step(name):
        def run():
            ran.append(name)
            if name == failing:
                raise NotImplementedError(name)

        return run

    for name in ("replacement", "main", "retired"):
        monkeypatch.setattr(services, f"_sync_{name}", step(name))
    if failing is None:
        services.sync_subscription()
    else:
        with pytest.raises(NotImplementedError) as caught:
            services.sync_subscription()
        assert str(caught.value) == failing
    assert ran == ["replacement", "main", "retired"]
