"""Billing services (spec 07; Coding-Standards.md §1: business logic lives
here, never in views/serializers/tasks). Every function requires a tenant
context.

Implemented so far: usage and quota (W3), reconciliation/lifecycle (W4) and
checkout, cancel and the dashboard view (W5), and the Razorpay webhook
receiver (W6).

Razorpay is reached only through billing.razorpay (Decision 15).

Lock order, everywhere: Subscription row, then the current UsageRecord row.
Nothing takes them the other way round. reserve_quota_unit() takes only the
UsageRecord lock.
"""
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q, QuerySet
from django.utils import timezone as dj_timezone

from accounts.models import TeamMember
from auditlog.services import record
from billing import razorpay
from billing.exceptions import (
    BillingNotConfigured,
    BillingProviderRejected,
    BillingProviderUnavailable,
    InvalidWebhookPayload,
    PlanChangeUnsupported,
    PlanNotAvailable,
    PlanUnchanged,
    ProviderStateUnsupported,
    SubscriptionActivating,
    SubscriptionCancelling,
    SubscriptionNotCancellable,
    SubscriptionPastDue,
    SubscriptionPlanUnsupported,
    WebhookRejected,
)
from billing.models import BillingEvent, PaymentAttempt, Plan, Subscription, UsageRecord
from core.exceptions import TenantContextError
from core.tenancy import (
    billing_ref_lookup_atomic,
    get_current_merchant_id,
    is_valid_billing_ref,
    tenant_atomic,
    tenant_context,
)

logger = logging.getLogger(__name__)

GRACE = timedelta(days=7)
DUNNING_STAGES = ((6, timedelta(days=6)), (3, timedelta(days=3)))
EVENT_SETTLE_AFTER = timedelta(minutes=2)
# A BillingEvent's created_at is stamped by the web host and a sync's
# fetch_started_at by the worker host. A sync marks only events received at
# least this long before its fetch began, which absorbs ordinary clock skew
# between the two (NTP-synced hosts differ by milliseconds). It is a margin,
# not a guarantee: a skew larger than this can still mark an event the fetch
# did not observe.
EVENT_CLOCK_SKEW = timedelta(seconds=5)
# The unique constraint that makes a repeated provider event id a duplicate
# delivery (BillingEvent.Meta). Only a violation of this one is a duplicate.
EVENT_UNIQUE_CONSTRAINT = "billing_billingevent_provider_uniq"
INCOMPLETE_SWEEP_WINDOW = timedelta(days=7)
TERMINAL_PROVIDER_STATUSES = frozenset({"cancelled", "completed", "expired"})
# The one "provider may be touched" rule (spec "Which provider states may be
# touched", approved 2026-09-30), shared by checkout and the sweep. authenticated,
# paused and any unrecognized or missing status are never cancelled or replaced.
CANCELLABLE_PROVIDER_STATUSES = frozenset({"created", "pending", "halted", "active"})


def provider_may_be_cancelled(provider_status: str | None) -> bool:
    return provider_status in CANCELLABLE_PROVIDER_STATUSES


AUDIT_CHECKOUT_STARTED = "billing.checkout_started"
AUDIT_PLAN_CHANGED = "billing.plan_changed"
AUDIT_CANCELLATION_REQUESTED = "billing.cancellation_requested"
AUDIT_ACTIVATED = "billing.subscription_activated"
AUDIT_PAST_DUE = "billing.subscription_past_due"
AUDIT_DUNNING_CHECKPOINT = "billing.dunning_checkpoint"
AUDIT_RECOVERED = "billing.subscription_recovered"
AUDIT_CANCELLED = "billing.subscription_cancelled"
AUDIT_EXPIRED = "billing.subscription_expired"


def _merchant_id() -> uuid.UUID:
    merchant_id = get_current_merchant_id()
    if merchant_id is None:
        raise TenantContextError("Billing services require a tenant context.")
    return merchant_id


# --- Plans and reads ------------------------------------------------------


def list_plans() -> QuerySet[Plan]:
    """Offered plans only. Plan is global reference data: no tenant filter."""
    return Plan.objects.filter(is_active=True).order_by("monthly_price", "id")


def get_subscription() -> Subscription | None:
    _merchant_id()
    with tenant_atomic():
        return Subscription.objects.select_related("plan", "pending_plan").first()


def get_merchant_plan_summary(merchant_id) -> dict | None:
    """`{id, name}` of the subscription's plan while the status is ACTIVE or
    PAST_DUE, else None: the value GET /merchant shows as `plan` (spec 07
    Decision 5; the plan has one home, Subscription.plan). Read-only, local
    state only, no provider call.

    The merchant being described must be the active tenant. A missing context
    or a different merchant raises TenantContextError: None is reserved for
    "a correctly scoped merchant with no entitled plan", never for "could not
    tell"."""
    current = _merchant_id()
    if current != uuid.UUID(str(merchant_id)):
        raise TenantContextError("The merchant is not the active tenant.")
    with tenant_atomic():
        subscription = Subscription.objects.select_related("plan").first()
    S = Subscription.Status
    if subscription is None or subscription.status not in (S.ACTIVE, S.PAST_DUE):
        return None
    return {"id": subscription.plan_id, "name": subscription.plan.name}


# --- Entitlement and quota (W3) -------------------------------------------


@dataclass(frozen=True)
class Entitlement:
    status: str | None
    plan: Plan | None
    period_start: datetime | None
    period_end: datetime | None
    quota_requests: int | None
    requests_used: int | None
    requests_remaining: int | None
    can_send: bool


class ReservationResult(str, Enum):
    RESERVED = "RESERVED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    NOT_ENTITLED = "NOT_ENTITLED"


def _usage_for(subscription: Subscription) -> UsageRecord | None:
    if subscription.current_period_start is None or subscription.current_period_end is None:
        return None
    return UsageRecord.objects.filter(
        period_start=subscription.current_period_start,
        period_end=subscription.current_period_end,
    ).first()


def get_entitlement() -> Entitlement:
    """Read-only, takes no lock. can_send is true only for an ACTIVE
    subscription inside its period with quota left (and a UsageRecord, which
    reserve_quota_unit() never creates)."""
    _merchant_id()
    with tenant_atomic():
        subscription = Subscription.objects.select_related("plan").first()
        if subscription is None:
            return Entitlement(None, None, None, None, None, None, None, False)
        usage = _usage_for(subscription)
        quota = subscription.plan.quota_requests
        used = usage.requests_used if usage is not None else None
        remaining = max(quota - used, 0) if used is not None else None
        can_send = (
            subscription.status == Subscription.Status.ACTIVE
            and subscription.current_period_end is not None
            and dj_timezone.now() < subscription.current_period_end
            and used is not None
            and used < quota
        )
        return Entitlement(
            status=subscription.status,
            plan=subscription.plan,
            period_start=subscription.current_period_start,
            period_end=subscription.current_period_end,
            quota_requests=quota,
            requests_used=used,
            requests_remaining=remaining,
            can_send=can_send,
        )


def _entitled_now(subscription: Subscription | None) -> bool:
    return (
        subscription is not None
        and subscription.status == Subscription.Status.ACTIVE
        and subscription.current_period_end is not None
        and dj_timezone.now() < subscription.current_period_end
    )


def reserve_quota_unit() -> ReservationResult:
    """Atomically reserves one unit of the current period's quota.

    Must run inside the caller's own tenant_atomic(), so the increment commits
    with the caller's own write (Phase 11: the SENDING transition). It takes
    the UsageRecord row lock and holds it until the caller commits: after
    calling it the caller must never lock the Subscription row in that
    transaction (Decision 4). It never creates a UsageRecord.
    """
    _merchant_id()
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("reserve_quota_unit() must run inside the caller's transaction.")
    with tenant_atomic():
        subscription = Subscription.objects.select_related("plan").first()
        if not _entitled_now(subscription):
            return ReservationResult.NOT_ENTITLED

        usage = (
            UsageRecord.objects.select_for_update()
            .filter(
                period_start=subscription.current_period_start,
                period_end=subscription.current_period_end,
            )
            .first()
        )
        if usage is None:
            return ReservationResult.NOT_ENTITLED

        # READ COMMITTED: this statement sees what a transition committed while
        # we waited for the lock. Re-check status, period and (upgraded) plan.
        subscription = Subscription.objects.select_related("plan").get(pk=subscription.pk)
        if not _entitled_now(subscription) or (
            subscription.current_period_start,
            subscription.current_period_end,
        ) != (usage.period_start, usage.period_end):
            return ReservationResult.NOT_ENTITLED

        if usage.requests_used >= subscription.plan.quota_requests:
            return ReservationResult.QUOTA_EXHAUSTED

        UsageRecord.objects.filter(pk=usage.pk).update(
            requests_used=F("requests_used") + 1, updated_at=dj_timezone.now()
        )
        return ReservationResult.RESERVED


def reset_usage_period(subscription: Subscription) -> UsageRecord:
    """Insert-or-ignore the UsageRecord for the subscription's current
    period, then return it. An internal helper of the reconcile flow (and the
    seed): it is NOT the old monthly reset, and nothing schedules it. Callers
    must already hold a paid-period proof (current_period_paid()); this
    function does not re-check it. Safe to call any number of times."""
    merchant_id = _merchant_id()
    if subscription.merchant_id != merchant_id:
        raise TenantContextError("reset_usage_period() subscription is not the current merchant's.")
    if subscription.current_period_start is None or subscription.current_period_end is None:
        raise ValueError("reset_usage_period() needs a subscription with a current period.")
    with tenant_atomic():
        try:
            with transaction.atomic():
                return UsageRecord.objects.create(
                    merchant_id=merchant_id,
                    period_start=subscription.current_period_start,
                    period_end=subscription.current_period_end,
                )
        except IntegrityError:
            return UsageRecord.objects.get(
                period_start=subscription.current_period_start,
                period_end=subscription.current_period_end,
            )


# --- Paid-entitlement rule (W4) -------------------------------------------


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def current_period_paid(entity: dict, invoices: list[dict] | None) -> dict | None:
    """The invoice that proves the subscription's current period is paid, or
    None. Razorpay's `active` alone proves nothing (V4): a halted subscription
    returns to active without re-charging the missed invoice.

    All of: subscription_id equals the subscription's id; status == "paid";
    amount_due == 0; payment_id present; billing_start <= current_start <
    billing_end. Pure, no I/O. The one place the window test lives (T7)."""
    if not isinstance(entity, dict) or not invoices:
        return None
    ref, current_start = entity.get("id"), entity.get("current_start")
    if not isinstance(ref, str) or not _is_int(current_start):
        return None
    for invoice in invoices:
        if not isinstance(invoice, dict):
            continue
        billing_start, billing_end = invoice.get("billing_start"), invoice.get("billing_end")
        payment_id = invoice.get("payment_id")
        if (
            invoice.get("subscription_id") == ref
            and invoice.get("status") == "paid"
            and _is_int(invoice.get("amount_due"))
            and invoice["amount_due"] == 0
            and isinstance(payment_id, str)
            and payment_id
            and _is_int(billing_start)
            and _is_int(billing_end)
            and billing_start <= current_start < billing_end
        ):
            return invoice
    return None


# --- Reconciliation (W4) --------------------------------------------------


@dataclass(frozen=True)
class SnapshotResult:
    """changed: a status, plan or period changed.

    settled: the provider snapshot was successfully reconciled and persisted,
    so the BillingEvent rows that triggered the sync are marked processed
    (decided 2026-09-30). It does NOT mean ACTIVE, entitled or paid: a
    paused or unknown provider status, or any other "no change" cell, is
    settled. It is False only where the spec says events stay unprocessed (no
    qualifying invoice or a failed fetch, a stale or dropped fetch, an unknown
    plan, a provider API failure) and for a malformed snapshot (no status, or
    an unusable period), which is not a valid snapshot and which the spec does
    not address."""

    changed: bool
    settled: bool


def _from_unix(value) -> datetime | None:
    if not _is_int(value):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _lock_subscription(subscription_id) -> Subscription:
    """Subscription row lock, then the current UsageRecord row lock, even when
    the caller will not write usage: that is what orders every
    entitlement-changing transition against reserve_quota_unit()."""
    subscription = (
        Subscription.objects.select_for_update(of=("self",))
        .select_related("plan")
        .get(pk=subscription_id)
    )
    if subscription.current_period_start is not None and subscription.current_period_end is not None:
        list(
            UsageRecord.objects.select_for_update().filter(
                period_start=subscription.current_period_start,
                period_end=subscription.current_period_end,
            )
        )
    return subscription


def _audit(action, subscription, actor, **metadata):
    record(
        action,
        actor=actor.user if actor is not None else None,
        target=subscription,
        metadata=metadata,
    )


def _record_payment(subscription: Subscription, invoice: dict, was_past_due: bool) -> None:
    """Insert-or-ignore the real Razorpay payment that proved the period.

    attempted_at is the qualifying invoice's `paid_at` (signed off
    2026-09-30): Razorpay documents it as the time the payment was made, and
    the invoice is already fetched, so no payment API call is made. It is not
    a condition of current_period_paid(): an invoice without a usable
    `paid_at` still qualifies and the subscription still reconciles. The
    ledger row alone fails closed: nothing is written and no other time is
    substituted (not ReviewFlow's clock, not invoice.created_at, not
    payment.created_at). The condition is logged with the merchant id only."""
    merchant_id = _merchant_id()
    if subscription.merchant_id != merchant_id:
        raise TenantContextError("PaymentAttempt merchant must equal the subscription's merchant.")
    paid_at = _from_unix(invoice.get("paid_at"))
    if paid_at is None:
        logger.warning(
            "Paid invoice for merchant %s has no usable paid_at; PaymentAttempt not recorded",
            merchant_id,
        )
        return
    try:
        with transaction.atomic():
            PaymentAttempt.objects.create(
                merchant_id=merchant_id,
                subscription=subscription,
                provider=PaymentAttempt.Provider.RAZORPAY,
                provider_attempt_id=invoice["payment_id"],
                attempt_type=(
                    PaymentAttempt.AttemptType.RETRY
                    if was_past_due
                    else PaymentAttempt.AttemptType.RENEWAL
                ),
                status=PaymentAttempt.Status.SUCCEEDED,
                attempted_at=paid_at,
            )
    except IntegrityError:
        pass  # an earlier sync already recorded this paid invoice's payment


def apply_provider_snapshot(
    subscription: Subscription,
    entity: dict,
    invoices: list[dict] | None,
    fetch_started_at: datetime,
    *,
    actor: TeamMember | None = None,
) -> bool:
    """The single place a status, plan or period changes. Returns whether
    anything changed. `actor` only names the OWNER on a plan-change audit row."""
    return _apply_snapshot(subscription, entity, invoices, fetch_started_at, actor=actor).changed


def _apply_snapshot(
    subscription: Subscription,
    entity: dict,
    invoices: list[dict] | None,
    fetch_started_at: datetime,
    *,
    actor: TeamMember | None = None,
) -> SnapshotResult:
    merchant_id = _merchant_id()
    if subscription.merchant_id != merchant_id:
        raise TenantContextError("Subscription is not the current merchant's.")

    with tenant_atomic():
        sub = _lock_subscription(subscription.pk)
        now = dj_timezone.now()

        # Stale-apply guard: the ref may have been replaced, or a newer fetch
        # already applied.
        ref = entity.get("id") if isinstance(entity, dict) else None
        if not ref or sub.payment_provider_ref != ref:
            return SnapshotResult(False, False)
        if sub.provider_synced_at is not None and fetch_started_at <= sub.provider_synced_at:
            return SnapshotResult(False, False)

        plan_id = entity.get("plan_id")
        plan = Plan.objects.filter(provider_plan_id=plan_id).first() if isinstance(plan_id, str) else None
        if plan is None:
            # Operator error in plan setup: never guess a plan.
            logger.warning("Billing snapshot for merchant %s names an unknown plan", merchant_id)
            return SnapshotResult(False, False)

        provider_status = entity.get("status")
        if not isinstance(provider_status, str):
            logger.warning("Billing snapshot for merchant %s has no status", merchant_id)
            return SnapshotResult(False, False)

        changed, settled, advanced = False, True, False
        fields = {"provider_status", "provider_synced_at"}
        if provider_status == "active":
            changed, settled, advanced = _apply_active(sub, entity, invoices, plan, fields, actor)
        elif provider_status in ("pending", "halted"):
            changed = _apply_payment_failed(sub, now, fields)
        elif provider_status in TERMINAL_PROVIDER_STATUSES:
            changed = _apply_terminal(sub, now, fields)
        elif provider_status not in ("created", "authenticated"):
            # paused or unknown: no change, logged.
            logger.warning("Billing snapshot for merchant %s has status %r", merchant_id, provider_status[:16])

        sub.provider_status = provider_status[:16]
        sub.provider_synced_at = fetch_started_at
        update_fields = set(fields)
        if changed:
            update_fields.add("updated_at")
        sub.save(update_fields=sorted(update_fields))
        if advanced:
            # The new period is proven paid: open its UsageRecord, in the same
            # transaction. The old record is left as it is (no rollover).
            reset_usage_period(sub)
        return SnapshotResult(changed, settled)


# The helpers below run under _apply_snapshot's row locks. Each mutates `sub`
# in memory and adds every field it touches to `fields`; _apply_snapshot saves.


def _apply_active(
    sub: Subscription,
    entity: dict,
    invoices: list[dict] | None,
    plan: Plan,
    fields: set[str],
    actor: TeamMember | None,
) -> tuple[bool, bool, bool]:
    """Provider `active`. Returns (changed, settled, advanced)."""
    Status = Subscription.Status
    current_start = _from_unix(entity.get("current_start"))
    current_end = _from_unix(entity.get("current_end"))
    if current_start is None or current_end is None or current_end <= current_start:
        logger.warning("Billing snapshot for merchant %s has an invalid period", sub.merchant_id)
        return False, False, False

    paid = current_period_paid(entity, invoices)
    old_status = sub.status
    was_past_due = old_status == Status.PAST_DUE
    old_plan, old_pending = sub.plan, sub.pending_plan_id
    same_period = (
        old_status == Status.ACTIVE
        and sub.current_period_start is not None
        and sub.current_period_start == current_start
    )

    if same_period:
        # A plan refresh inside an unchanged period needs no paid proof.
        changed = False
        if old_plan.pk != plan.pk:
            _take_plan(sub, plan, old_pending, fields)
            changed = True
            _audit_plan_change(sub, actor, old_plan, plan, old_pending)
        elif old_pending is not None and old_pending == plan.pk:
            sub.pending_plan = None
            fields.add("pending_plan")
            changed = True
        if paid is not None:
            _record_payment(sub, paid, was_past_due)
        return changed, True, False

    if paid is None:
        # Fail closed: no qualifying invoice (or the fetch was not made) means
        # no transition, no new period, no UsageRecord. The events stay
        # unprocessed for the next sweep.
        return False, False, False

    _take_plan(sub, plan, old_pending, fields, period=(current_start, current_end))
    if old_status != Status.ACTIVE:
        sub.status = Status.ACTIVE
        fields.add("status")
        if was_past_due:
            _clear_grace(sub, fields)
        else:
            # Decided 2026-09-30: a stale cancellation flag must not survive
            # INCOMPLETE/CANCELLED/EXPIRED -> ACTIVE. ACTIVE -> CANCELLED and
            # PAST_DUE -> ACTIVE leave it as it is: the spec defines neither.
            sub.cancel_at_period_end = False
            fields.add("cancel_at_period_end")
        _audit(
            AUDIT_RECOVERED if was_past_due else AUDIT_ACTIVATED,
            sub,
            None,
            from_status=old_status,
            to_status=Status.ACTIVE,
            plan_id=str(plan.pk),
            plan_name=plan.name,
        )
    elif old_plan.pk != plan.pk:
        _audit_plan_change(sub, actor, old_plan, plan, old_pending)
    _record_payment(sub, paid, was_past_due)
    return True, True, True


def _apply_payment_failed(sub: Subscription, now: datetime, fields: set[str]) -> bool:
    """Provider `pending`/`halted`: an ACTIVE row enters the grace period."""
    Status = Subscription.Status
    if sub.status != Status.ACTIVE:
        return False
    old_status = sub.status
    sub.status = Status.PAST_DUE
    sub.past_due_at = now
    sub.dunning_stage = 0
    fields.update({"status", "past_due_at", "dunning_stage"})
    _audit(
        AUDIT_PAST_DUE,
        sub,
        None,
        from_status=old_status,
        to_status=Status.PAST_DUE,
        plan_id=str(sub.plan_id),
    )
    return True


def _apply_terminal(sub: Subscription, now: datetime, fields: set[str]) -> bool:
    """Provider `cancelled`/`completed`/`expired`: the local row ends."""
    Status = Subscription.Status
    old_status = sub.status
    if old_status == Status.ACTIVE:
        sub.status = Status.CANCELLED
        fields.add("status")
        _audit(AUDIT_CANCELLED, sub, None, from_status=old_status, to_status=Status.CANCELLED)
        return True
    if old_status == Status.PAST_DUE:
        grace_over = now >= sub.past_due_at + GRACE
        sub.status = Status.EXPIRED if grace_over else Status.CANCELLED
        fields.add("status")
        _clear_grace(sub, fields)
        _audit(
            AUDIT_EXPIRED if grace_over else AUDIT_CANCELLED,
            sub,
            None,
            from_status=old_status,
            to_status=sub.status,
        )
        return True
    return False


def _take_plan(
    sub: Subscription,
    plan: Plan,
    old_pending: uuid.UUID | None,
    fields: set[str],
    period: tuple[datetime, datetime] | None = None,
) -> None:
    """Take the snapshot's plan, clear a pending plan it fulfils, and take the
    period when one is given."""
    sub.plan = plan
    fields.add("plan")
    if old_pending is not None and old_pending == plan.pk:
        sub.pending_plan = None
        fields.add("pending_plan")
    if period is not None:
        sub.current_period_start, sub.current_period_end = period
        fields.update({"current_period_start", "current_period_end"})


def _clear_grace(sub: Subscription, fields: set[str]) -> None:
    sub.past_due_at = None
    sub.dunning_stage = None
    fields.update({"past_due_at", "dunning_stage"})


def _audit_plan_change(
    sub: Subscription,
    actor: TeamMember | None,
    old_plan: Plan,
    plan: Plan,
    old_pending: uuid.UUID | None,
) -> None:
    # `effective` uses the pending plan as it was before _take_plan cleared it.
    _audit(
        AUDIT_PLAN_CHANGED,
        sub,
        actor,
        effective="APPLIED" if old_pending == plan.pk else "IMMEDIATE",
        from_plan=old_plan.name,
        to_plan=plan.name,
        plan_id=str(plan.pk),
    )


def _fetch_invoices_if_needed(ref, old_status, old_period_start, entity) -> list[dict] | None:
    """The invoices the paid-entitlement rule needs, or None when the rule is
    not required: only an `active` snapshot for a row that is not ACTIVE, or
    for a different period, needs a paid proof. Shared by sync and by the
    checkout/cancel reconcile."""
    if entity.get("status") != "active":
        return None
    same_period = (
        old_status == Subscription.Status.ACTIVE
        and old_period_start is not None
        and _from_unix(entity.get("current_start")) == old_period_start
    )
    return None if same_period else razorpay.fetch_invoices(ref)


def sync_subscription() -> None:
    """Fetch the subscription's current state from Razorpay and converge to
    it. The webhook is only a signal; transitions come from this fetched state.

    Every provider call happens outside any transaction of its own: read the
    ref, fetch, then lock and apply. A failed fetch applies nothing and leaves
    the events unprocessed for the next sweep."""
    merchant_id = _merchant_id()
    with tenant_atomic():
        subscription = Subscription.objects.first()
        if subscription is None or not subscription.payment_provider_ref:
            return
        ref = subscription.payment_provider_ref
        old_status = subscription.status
        old_period_start = subscription.current_period_start

    fetch_started_at = dj_timezone.now()
    try:
        entity = razorpay.fetch_subscription(ref)
        invoices = _fetch_invoices_if_needed(ref, old_status, old_period_start, entity)
    except (BillingProviderUnavailable, BillingProviderRejected, BillingNotConfigured) as exc:
        logger.warning("Billing sync for merchant %s failed with %s", merchant_id, type(exc).__name__)
        return

    with tenant_atomic():
        result = _apply_snapshot(subscription, entity, invoices, fetch_started_at)
        if result.settled:
            # Only events received more than EVENT_CLOCK_SKEW before the fetch
            # began are marked (strictly: an event exactly on the cutoff is
            # not). One timestamp for both columns.
            stamp = dj_timezone.now()
            BillingEvent.objects.filter(
                processed_at__isnull=True, created_at__lt=fetch_started_at - EVENT_CLOCK_SKEW
            ).update(processed_at=stamp, updated_at=stamp)


def sync_pending_events() -> None:
    """The sync a webhook enqueues (receive order step 7). Every committed
    BillingEvent enqueues one; this skips the provider fetch when the merchant
    has no unprocessed BillingEvent.

    Why the skip is safe: processed_at is set only by a sync that settled and
    whose fetch started more than EVENT_CLOCK_SKEW after the event was
    received (sync_subscription). The task for an event is enqueued after
    that event commits, so when it starts the event is either still
    unprocessed (and this fetches) or was already observed by such a sync.
    Both timestamps come from different hosts, so the margin absorbs ordinary
    clock skew but cannot guarantee correctness under arbitrary drift: a skew
    beyond EVENT_CLOCK_SKEW can mark an event the fetch did not observe.
    An event received within the margin of a fetch stays unprocessed; the
    next task fetches again (or, once it exists, the sweep). A replayed body
    under fresh event ids still enqueues one task per event; those tasks
    collapse into one fetch only once the events are older than the margin.
    While the subscription is not settled (no qualifying invoice, a failed
    fetch), events stay unprocessed and every task fetches."""
    _merchant_id()
    with tenant_atomic():
        pending = BillingEvent.objects.filter(processed_at__isnull=True).exists()
    if pending:
        sync_subscription()


# --- Dunning checkpoints, grace expiry, owed provider cancel (W4) ----------


def advance_dunning(now: datetime | None = None) -> None:
    """Advance dunning_stage to the highest checkpoint that is due, expire the
    grace period at past_due_at + 7 days, then retry an owed provider cancel.
    Safe to run any number of times. It writes no PaymentAttempt, retries no
    payment and sends no reminder: Razorpay owns both (Change 3)."""
    _merchant_id()
    now = now or dj_timezone.now()
    with tenant_atomic():
        subscription = Subscription.objects.first()
        if subscription is not None:
            sub = _lock_subscription(subscription.pk)
            if sub.status == Subscription.Status.PAST_DUE:
                _advance_past_due(sub, now)
    _retry_owed_provider_cancel()


def _advance_past_due(sub: Subscription, now: datetime) -> None:
    S = Subscription.Status
    if now >= sub.past_due_at + GRACE:
        # Unconditional: the locked day-7 rule never waits on a provider call.
        sub.status = S.EXPIRED
        sub.past_due_at = None
        sub.dunning_stage = None
        sub.save(update_fields=["status", "past_due_at", "dunning_stage", "updated_at"])
        _audit(AUDIT_EXPIRED, sub, None, from_status=S.PAST_DUE, to_status=S.EXPIRED)
        return
    for stage, offset in DUNNING_STAGES:
        if now >= sub.past_due_at + offset:
            # One conditional update: zero rows means it was already done.
            advanced = Subscription.objects.filter(
                pk=sub.pk, past_due_at=sub.past_due_at, dunning_stage__lt=stage
            ).update(dunning_stage=stage, updated_at=now)
            if advanced:
                _audit(AUDIT_DUNNING_CHECKPOINT, sub, None, stage=stage)
            return


def _retry_owed_provider_cancel() -> None:
    """A CANCELLED/EXPIRED row whose provider subscription may be cancelled
    (provider_may_be_cancelled) is still owed a provider cancel. An
    authenticated, paused, unrecognized or missing provider status is never
    touched here, exactly as checkout never touches it. The call is made with
    no transaction of ours open; a failure or refusal is logged and retried on
    a later tick."""
    with tenant_atomic():
        subscription = Subscription.objects.filter(
            status__in=[Subscription.Status.CANCELLED, Subscription.Status.EXPIRED],
            payment_provider_ref__isnull=False,
            provider_status__in=CANCELLABLE_PROVIDER_STATUSES,
        ).first()
        if subscription is None:
            return
        ref = subscription.payment_provider_ref
        pk = subscription.pk

    try:
        entity = razorpay.cancel_subscription(ref, at_cycle_end=False)
    except (BillingProviderUnavailable, BillingProviderRejected, BillingNotConfigured) as exc:
        logger.warning("Owed provider cancel failed with %s", type(exc).__name__)
        return

    status = entity.get("status")
    if isinstance(status, str):
        with tenant_atomic():
            # Only while the row still points at the subscription we cancelled.
            Subscription.objects.filter(pk=pk, payment_provider_ref=ref).update(
                provider_status=status[:16]
            )


def maintenance_due(now: datetime | None = None) -> bool:
    """Whether the current merchant needs a maintenance run."""
    _merchant_id()
    now = now or dj_timezone.now()
    with tenant_atomic():
        if BillingEvent.objects.filter(
            processed_at__isnull=True, created_at__lte=now - EVENT_SETTLE_AFTER
        ).exists():
            return True
        S = Subscription.Status
        return (
            Subscription.objects.filter(
                Q(
                    status=S.INCOMPLETE,
                    payment_provider_ref__isnull=False,
                    updated_at__gte=now - INCOMPLETE_SWEEP_WINDOW,
                )
                # A lapsed ACTIVE row with no provider reference has nothing to
                # sync (decided 2026-10-01), so it is not swept every tick.
                | Q(status=S.ACTIVE, current_period_end__lte=now, payment_provider_ref__isnull=False)
                | Q(status=S.PAST_DUE)
                | (
                    Q(status__in=[S.CANCELLED, S.EXPIRED], payment_provider_ref__isnull=False)
                    & (
                        Q(provider_status__isnull=True)
                        | ~Q(provider_status__in=TERMINAL_PROVIDER_STATUSES)
                    )
                )
            )
        ).exists()


# --- Checkout, cancel and the dashboard view (W5) -----------------------------

RECOGNIZED_PROVIDER_STATUSES = frozenset(
    {
        "created",
        "authenticated",
        "active",
        "pending",
        "halted",
        "cancelled",
        "completed",
        "expired",
        "paused",
    }
)

# One explicit open case (D1): billing.razorpay.fetch_invoices() is an
# unimplemented fail-closed stop (invoice-list pagination is not
# established). Every path that needs the paid-entitlement proof reaches it
# through _fetch_invoices_if_needed, and nothing catches its error.
#
# An ACTIVE subscription with no provider reference (decided 2026-10-01): only
# the dev seed creates one, since every production ACTIVE comes from a
# provider snapshot. There is no provider subscription to change or cancel,
# so checkout and cancel answer 409 subscription_provider_state_unsupported
# with no provider call, no local change and no audit row.


@dataclass(frozen=True)
class SubscriptionOverview:
    subscription: Subscription | None
    entitlement: Entitlement
    grace_ends_at: datetime | None
    checkout: dict | None
    next_action: dict


@dataclass(frozen=True)
class CheckoutResult:
    created: bool  # a provider subscription was created by this request (201)
    checkout: dict | None  # what the browser needs to open Razorpay Checkout


def _require_request_transaction() -> None:
    """Checkout and cancel run inside the request's tenant transaction (the
    spec's stated exception: a provider call under the row lock). Each phase
    below is its own savepoint, so a domain error rolls back only its own
    writes, never a reconcile that already succeeded."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Billing checkout and cancel must run inside the request's transaction.")


def _require_configured() -> None:
    if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
        raise BillingNotConfigured()


def _checkout_payload(ref: str) -> dict:
    return {"provider": "razorpay", "key_id": settings.RAZORPAY_KEY_ID, "subscription_id": ref}


def _lock_current_subscription() -> Subscription | None:
    """The merchant's Subscription, locked (then its current UsageRecord, the
    lock order), or None."""
    with tenant_atomic():
        subscription = Subscription.objects.first()
        return _lock_subscription(subscription.pk) if subscription is not None else None


def _offered_plan(plan_id) -> Plan:
    try:
        plan = Plan.objects.filter(pk=plan_id, is_active=True).first()
    except (ValueError, TypeError, DjangoValidationError):
        plan = None
    if plan is None or not plan.provider_plan_id:
        raise PlanNotAvailable()
    return plan


# --- the dashboard view ------------------------------------------------------


def _next_action(subscription: Subscription | None, grace_ends_at) -> dict:
    S = Subscription.Status
    if subscription is None or subscription.status in (S.CANCELLED, S.EXPIRED):
        return {"type": "SUBSCRIBE", "at": None}
    if subscription.status == S.INCOMPLETE:
        return {"type": "COMPLETE_CHECKOUT", "at": None}
    if subscription.status == S.PAST_DUE:
        kind = "UPDATE_PAYMENT_METHOD" if subscription.provider_status == "pending" else "RESUBSCRIBE"
        return {"type": kind, "at": grace_ends_at}
    if subscription.cancel_at_period_end:
        return {"type": "CANCELLATION", "at": subscription.current_period_end}
    if subscription.pending_plan_id is not None:
        return {"type": "PLAN_CHANGE", "at": subscription.current_period_end}
    return {"type": "RENEWAL", "at": subscription.current_period_end}


def get_subscription_overview(*, role: str) -> SubscriptionOverview:
    """Reads local state only; it never calls the provider. The `checkout`
    object is decided here, by role: only an OWNER gets it, because those
    values are enough to open Razorpay Checkout and change the payment method,
    and changing billing is OWNER-only."""
    _merchant_id()
    with tenant_atomic():
        subscription = Subscription.objects.select_related("plan", "pending_plan").first()
        entitlement = get_entitlement()
    S = Subscription.Status
    grace_ends_at = (
        subscription.past_due_at + GRACE
        if subscription is not None and subscription.status == S.PAST_DUE
        else None
    )
    checkout = None
    if role == TeamMember.Role.OWNER and subscription is not None and subscription.payment_provider_ref:
        if subscription.status == S.INCOMPLETE:
            checkout = {**_checkout_payload(subscription.payment_provider_ref), "card_change": False}
        elif subscription.status == S.PAST_DUE and subscription.provider_status == "pending":
            checkout = {**_checkout_payload(subscription.payment_provider_ref), "card_change": True}
    return SubscriptionOverview(
        subscription=subscription,
        entitlement=entitlement,
        grace_ends_at=grace_ends_at,
        checkout=checkout,
        next_action=_next_action(subscription, grace_ends_at),
    )


# --- reconcile -------------------------------------------------------------------


def _fetch_provider_entity(ref: str) -> dict:
    """The provider subscription, or a 502. A permanent 4xx (a ref Razorpay no
    longer knows) is a failed fetch, fail closed, never a reason to replace
    anything (F4). An entity with no id, a different id, or no string status
    is malformed and treated the same way (A3)."""
    try:
        entity = razorpay.fetch_subscription(ref)
    except BillingProviderRejected:
        raise BillingProviderUnavailable() from None
    if (
        not isinstance(entity, dict)
        or entity.get("id") != ref
        or not isinstance(entity.get("status"), str)
    ):
        raise BillingProviderUnavailable()
    return entity


def _reconcile(subscription: Subscription, *, actor=None) -> tuple[Subscription, dict]:
    """Fetch the provider subscription and apply it through the one place a
    status, plan or period changes. The snapshot is committed in its own
    savepoint, so it survives a later 409/502 of the same request: it is the
    approved reconcile, not part of what the request then refuses."""
    ref = subscription.payment_provider_ref
    started = dj_timezone.now()
    entity = _fetch_provider_entity(ref)
    try:
        invoices = _fetch_invoices_if_needed(
            ref, subscription.status, subscription.current_period_start, entity
        )
    except BillingProviderRejected:
        raise BillingProviderUnavailable() from None
    with tenant_atomic():
        _apply_snapshot(subscription, entity, invoices, started, actor=actor)
    with tenant_atomic():
        return _lock_subscription(subscription.pk), entity


def _check_provider_state(entity: dict) -> None:
    status = entity["status"]
    if status == "paused" or status not in RECOGNIZED_PROVIDER_STATUSES:
        raise ProviderStateUnsupported()
    plan_id = entity.get("plan_id")
    if not isinstance(plan_id, str) or not Plan.objects.filter(provider_plan_id=plan_id).exists():
        raise SubscriptionPlanUnsupported()


# --- creating and replacing the provider subscription ---------------------------------


def _create_provider_subscription(plan: Plan) -> dict:
    try:
        entity = razorpay.create_subscription(plan.provider_plan_id)
    except BillingProviderRejected:
        raise BillingProviderUnavailable() from None
    # F5: an unusable id is never stored, because no webhook could be resolved
    # through it. The subscription Razorpay created is never paid (the browser
    # never receives its id) and lapses on the provider side.
    if not isinstance(entity, dict) or not is_valid_billing_ref(entity.get("id")):
        raise BillingProviderUnavailable()
    return entity


def _cancel_old_provider_subscription(ref: str) -> None:
    """Immediate cancel (V3: created/authenticated must be cancelled at once).
    A failed or refused cancel is a 502 and nothing is created, so two live
    provider subscriptions never exist for one merchant."""
    try:
        razorpay.cancel_subscription(ref, at_cycle_end=False)
    except BillingProviderRejected:
        raise BillingProviderUnavailable() from None


def _store_new_provider_subscription(subscription: Subscription, plan: Plan, entity: dict, actor) -> None:
    status = entity.get("status")
    subscription.plan = plan
    subscription.status = Subscription.Status.INCOMPLETE
    subscription.payment_provider_ref = entity["id"]
    subscription.provider_status = status[:16] if isinstance(status, str) else None
    # Approved 2026-09-30 (D2): INCOMPLETE means the first payment of the NEW
    # subscription is not confirmed, so nothing of the previous lifecycle is
    # carried onto the row. The old period and usage stay only in the
    # historical records (UsageRecord, PaymentAttempt, BillingEvent, AuditLog),
    # which are never deleted.
    subscription.pending_plan = None
    subscription.cancel_at_period_end = False
    subscription.provider_synced_at = None
    subscription.current_period_start = None
    subscription.current_period_end = None
    subscription.save(
        update_fields=[
            "plan",
            "status",
            "payment_provider_ref",
            "provider_status",
            "pending_plan",
            "cancel_at_period_end",
            "provider_synced_at",
            "current_period_start",
            "current_period_end",
            "updated_at",
        ]
    )
    _audit(AUDIT_CHECKOUT_STARTED, subscription, actor, plan_id=str(plan.pk), plan_name=plan.name)


def _replace_provider_subscription(subscription, plan, actor, *, cancel_ref=None) -> CheckoutResult:
    with tenant_atomic():
        if cancel_ref is not None:
            _cancel_old_provider_subscription(cancel_ref)
        entity = _create_provider_subscription(plan)
        _store_new_provider_subscription(subscription, plan, entity, actor)
    return CheckoutResult(created=True, checkout=_checkout_payload(entity["id"]))


def _first_checkout(plan: Plan, actor) -> CheckoutResult | None:
    """No row yet. The INCOMPLETE row is inserted first, so UNIQUE(merchant)
    serializes two concurrent first checkouts (the loser waits, then finds the
    row) and no second provider subscription is ever created for a race. If
    the provider call fails the whole block rolls back, row included."""
    merchant_id = _merchant_id()
    with tenant_atomic():
        try:
            with transaction.atomic():
                subscription = Subscription.objects.create(
                    merchant_id=merchant_id, plan=plan, status=Subscription.Status.INCOMPLETE
                )
        except IntegrityError:
            return None
        entity = _create_provider_subscription(plan)
        _store_new_provider_subscription(subscription, plan, entity, actor)
    return CheckoutResult(created=True, checkout=_checkout_payload(entity["id"]))


# --- checkout ------------------------------------------------------------------------


def start_checkout(*, actor: TeamMember, plan_id) -> CheckoutResult:
    """The state table under POST /billing/checkout. Must run inside the
    request's transaction (see _require_request_transaction)."""
    _merchant_id()
    _require_request_transaction()
    plan = _offered_plan(plan_id)
    _require_configured()

    subscription = _lock_current_subscription()
    if subscription is None:
        result = _first_checkout(plan, actor)
        if result is not None:
            return result
        subscription = _lock_current_subscription()  # a concurrent first checkout won

    S = Subscription.Status
    if subscription.status == S.PAST_DUE:
        # A1: before any provider-state answer, with no provider call and no
        # local write.
        raise SubscriptionPastDue()

    entity = None
    status_before = subscription.status
    if subscription.payment_provider_ref:
        subscription, entity = _reconcile(subscription)
        if subscription.status == S.PAST_DUE:
            raise SubscriptionPastDue()
        _check_provider_state(entity)
    elif subscription.status == S.ACTIVE:
        raise ProviderStateUnsupported()  # no provider subscription to change

    if subscription.status == S.ACTIVE:
        if status_before != S.ACTIVE and subscription.plan_id == plan.pk:
            # Approved 2026-09-30 (D3): this request's own reconcile moved the
            # row into ACTIVE on the requested plan. A same-plan no-op: no
            # further provider call, no further audit row. A row that was
            # already ACTIVE before the request keeps the 422 below.
            return CheckoutResult(created=False, checkout=None)
        return _checkout_active(subscription, plan, actor)
    if subscription.status == S.INCOMPLETE:
        return _checkout_incomplete(subscription, entity, plan, actor)
    return _checkout_ended(subscription, entity, plan, actor)  # CANCELLED / EXPIRED


def _checkout_active(subscription: Subscription, plan: Plan, actor) -> CheckoutResult:
    if subscription.cancel_at_period_end:
        raise SubscriptionCancelling()
    current = subscription.plan
    if plan.pk == current.pk or plan.monthly_price == current.monthly_price:
        raise PlanUnchanged()
    if subscription.pending_plan_id == plan.pk:
        return CheckoutResult(created=False, checkout=None)  # already pending: nothing changes

    upgrade = plan.monthly_price > current.monthly_price
    ref = subscription.payment_provider_ref
    started = dj_timezone.now()
    try:
        entity = razorpay.update_subscription(
            ref, plan.provider_plan_id, "now" if upgrade else "cycle_end"
        )
    except BillingProviderRejected:
        # UPI, eMandate or a domestic card: Razorpay refuses the update (S4).
        raise PlanChangeUnsupported() from None

    with tenant_atomic():
        if upgrade:
            if (
                not isinstance(entity, dict)
                or entity.get("id") != ref
                or not isinstance(entity.get("status"), str)
            ):
                raise BillingProviderUnavailable()
            try:
                invoices = _fetch_invoices_if_needed(
                    ref, subscription.status, subscription.current_period_start, entity
                )
            except BillingProviderRejected:
                raise BillingProviderUnavailable() from None
            # Audited as plan_changed (IMMEDIATE) by the snapshot, naming the OWNER.
            _apply_snapshot(subscription, entity, invoices, started, actor=actor)
        else:
            subscription.pending_plan = plan
            subscription.save(update_fields=["pending_plan", "updated_at"])
            _audit(
                AUDIT_PLAN_CHANGED,
                subscription,
                actor,
                effective="SCHEDULED",
                from_plan=current.name,
                to_plan=plan.name,
                plan_id=str(plan.pk),
            )
    return CheckoutResult(created=False, checkout=None)


def _checkout_incomplete(subscription, entity, plan: Plan, actor) -> CheckoutResult:
    if entity is None:  # no provider reference yet
        return _replace_provider_subscription(subscription, plan, actor)
    ref = subscription.payment_provider_ref
    status = entity["status"]
    # "Same plan" means the provider's own plan, not the local row's.
    same_plan = entity.get("plan_id") == plan.provider_plan_id
    if status in TERMINAL_PROVIDER_STATUSES:
        return _replace_provider_subscription(subscription, plan, actor)
    if same_plan:
        return CheckoutResult(created=False, checkout=_checkout_payload(ref))
    if status == "created":
        return _replace_provider_subscription(subscription, plan, actor, cancel_ref=ref)
    # authenticated, or active/pending/halted not proven paid: the payer has
    # authorized and may have paid, so it is never cancelled or replaced.
    raise SubscriptionActivating()


def _checkout_ended(subscription, entity, plan: Plan, actor) -> CheckoutResult:
    """A CANCELLED or EXPIRED row."""
    if entity is None or entity["status"] in TERMINAL_PROVIDER_STATUSES:
        return _replace_provider_subscription(subscription, plan, actor)
    if provider_may_be_cancelled(entity["status"]):
        return _replace_provider_subscription(
            subscription, plan, actor, cancel_ref=subscription.payment_provider_ref
        )
    raise ProviderStateUnsupported()  # authenticated (F2)


# --- cancel ----------------------------------------------------------------------------


def cancel_subscription(*, actor: TeamMember) -> Subscription:
    """Order of checks (spec, approved 2026-09-30), under the row lock: a
    state that cannot be cancelled; an ACTIVE row already cancelling (a 200
    no-op, before any reconcile); an ACTIVE row reconciles first; PAST_DUE has
    no provider dependency."""
    _merchant_id()
    _require_request_transaction()

    subscription = _lock_current_subscription()
    S = Subscription.Status
    if subscription is None or subscription.status in (S.INCOMPLETE, S.CANCELLED, S.EXPIRED):
        raise SubscriptionNotCancellable()

    if subscription.status == S.ACTIVE:
        if subscription.cancel_at_period_end:
            return subscription  # idempotent: no fetch, no provider call, no audit row
        if not subscription.payment_provider_ref:
            raise ProviderStateUnsupported()  # no provider subscription to cancel
        subscription, entity = _reconcile(subscription)
        if subscription.status == S.ACTIVE:
            if entity["status"] == "paused":
                raise ProviderStateUnsupported()  # paused only; nothing is touched
            return _cancel_at_period_end(subscription, actor)
        if subscription.status != S.PAST_DUE:
            raise SubscriptionNotCancellable()  # the reconcile moved it past cancellable
    return _cancel_past_due(subscription, actor)


def _cancel_at_period_end(subscription: Subscription, actor) -> Subscription:
    try:
        razorpay.cancel_subscription(subscription.payment_provider_ref, at_cycle_end=True)
    except BillingProviderRejected:
        raise BillingProviderUnavailable() from None
    with tenant_atomic():
        subscription.cancel_at_period_end = True
        subscription.save(update_fields=["cancel_at_period_end", "updated_at"])
        _audit(AUDIT_CANCELLATION_REQUESTED, subscription, actor, effective="PERIOD_END")
    return subscription


def _cancel_past_due(subscription: Subscription, actor) -> Subscription:
    """Local-first: the row is CANCELLED in this request whatever the provider
    then allows. The provider cancel is attempted (only where the shared rule
    lets the provider be touched) and, if it fails, is owed to the sweep."""
    S = Subscription.Status
    ref, provider_status = subscription.payment_provider_ref, subscription.provider_status
    with tenant_atomic():
        subscription.status = S.CANCELLED
        subscription.past_due_at = None
        subscription.dunning_stage = None
        subscription.save(update_fields=["status", "past_due_at", "dunning_stage", "updated_at"])
        _audit(
            AUDIT_CANCELLATION_REQUESTED,
            subscription,
            actor,
            from_status=S.PAST_DUE,
            to_status=S.CANCELLED,
            effective="IMMEDIATE",
        )
    if ref and provider_may_be_cancelled(provider_status):
        try:
            entity = razorpay.cancel_subscription(ref, at_cycle_end=False)
        except (BillingProviderUnavailable, BillingProviderRejected, BillingNotConfigured) as exc:
            logger.warning("Provider cancel after a PAST_DUE cancel failed with %s", type(exc).__name__)
        else:
            status = entity.get("status") if isinstance(entity, dict) else None
            if isinstance(status, str):
                # Deliberately not guarded by provider_synced_at and not
                # stamping it: this runs under the request's row lock, records
                # only what the cancel call itself answered, and a local cancel
                # is not a snapshot (spec "Expiry versus sync: race rules"). A
                # snapshot fetched before the cancel may still overwrite it, and
                # the next sync restamps it from Razorpay's then-current state.
                with tenant_atomic():
                    Subscription.objects.filter(pk=subscription.pk, payment_provider_ref=ref).update(
                        provider_status=status[:16]
                    )
    with tenant_atomic():
        return Subscription.objects.select_related("plan", "pending_plan").get(pk=subscription.pk)


# --- Razorpay webhook receiver (W6) ----------------------------------------------

_EVENT_ID = re.compile(r"[\x21-\x7E]{1,64}")


def event_id_failure(value) -> str | None:
    """None for a valid x-razorpay-event-id, else the failure category (A5).
    The category is the only thing ever logged about an invalid id."""
    if value is None:
        return "missing"
    if not isinstance(value, str):
        return "not_string"  # cannot arrive over HTTP; checked defensively
    if value == "":
        return "empty"
    if len(value) > 64:
        return "too_long"
    if _EVENT_ID.fullmatch(value) is None:
        return "invalid_characters"
    return None


def _parse_event(raw_body: bytes) -> tuple[str, bool, object]:
    """Receive-order steps 4 and 5, up to the F1 check, on an already
    verified body: (event_type, has_subscription_entity, ref). Raises
    InvalidWebhookPayload for a body that is not an event object, or whose
    `event` is not a 1..64-character string. The ref is returned as found,
    unvalidated: the caller applies F1."""
    try:
        payload = json.loads(raw_body)
    except (ValueError, TypeError):
        raise InvalidWebhookPayload() from None
    if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
        raise InvalidWebhookPayload()
    event_type = payload["event"]
    if not 1 <= len(event_type) <= BillingEvent._meta.get_field("event_type").max_length:
        # Decided 2026-10-01: an event type the inbox cannot hold is an invalid
        # payload, before any lookup. Never truncated, never logged.
        raise InvalidWebhookPayload()

    entity = payload.get("payload")
    for key in ("subscription", "entity"):
        entity = entity.get(key) if isinstance(entity, dict) else None
    if not isinstance(entity, dict) or "id" not in entity:
        return event_type, False, None
    return event_type, True, entity["id"]


def receive_webhook(*, request) -> None:
    """The normative receive order (spec 07 "Webhook design"). Must be called
    with NO active tenant context. The throttle (step 1) is the view's DRF
    throttle, which runs before this.

    Nothing is read from the database before the signature and the event id
    have been verified. The merchant comes only from the Subscription row
    found by the ref ReviewFlow stored at checkout, never from the payload.
    """
    # 2. The signature over the raw body; request.data is never touched.
    raw_body = request.body
    if not razorpay.verify_webhook_signature(raw_body, request.META.get("HTTP_X_RAZORPAY_SIGNATURE")):
        # Never log the body, the signature or a secret.
        logger.warning("Razorpay webhook rejected: invalid signature")
        raise WebhookRejected()

    # 3. The event id (A5): identical 401 for every failure, category only logged.
    event_id = request.META.get("HTTP_X_RAZORPAY_EVENT_ID")
    failure = event_id_failure(event_id)
    if failure is not None:
        logger.warning("Razorpay webhook rejected: event id %s", failure)
        raise WebhookRejected()

    # 4-5. The JSON body and the provider ref. No subscription entity: not a
    # billing event for ReviewFlow. A malformed ref is treated like an unknown
    # one (F1), before any lookup, and its raw value is never logged. Neither
    # "ignored" line logs the provider event id: a fixed reason and the event
    # type only. The event type is logged with %r, so a control character in it
    # is escaped and cannot forge a log line; the stored value is never altered.
    event_type, has_entity, ref = _parse_event(raw_body)
    if not has_entity:
        return None
    if not is_valid_billing_ref(ref):
        logger.info("Razorpay webhook (%r) ignored: malformed subscription ref", event_type)
        return None

    # 6. The pre-tenant lookup through the SELECT-only billing_ref_lookup policy.
    try:
        with billing_ref_lookup_atomic(ref):
            subscription = Subscription.objects.for_lookup_ref(ref).first()
    except TenantContextError:
        # Called inside a merchant context: accounts.middleware keeps this URL
        # pre-tenant, so this does not happen in practice. Fail closed, as the
        # integrations receiver does.
        raise WebhookRejected() from None
    if subscription is None:
        logger.info("Razorpay webhook (%r) ignored: no matching subscription", event_type)
        return None

    # 7. Persist first, then process.
    merchant_id = subscription.merchant_id
    with tenant_context(merchant_id), tenant_atomic():
        try:
            with transaction.atomic():
                BillingEvent.objects.create(
                    merchant_id=merchant_id,
                    provider=BillingEvent.Provider.RAZORPAY,
                    provider_event_id=event_id,
                    event_type=event_type,
                    provider_ref=ref,
                )
        except IntegrityError as exc:
            if not _is_duplicate_event(exc):
                raise  # an unrelated constraint failure is never a duplicate
            return None  # a duplicate delivery: no enqueue, no other write
        # No PaymentAttempt here (decided 2026-10-01): the payload carries no
        # invoice, so no paid_at. The sync records the payment from the
        # qualifying paid invoice.
        transaction.on_commit(_enqueue_sync(merchant_id))
    return None


def _is_duplicate_event(exc: IntegrityError) -> bool:
    """True only for a unique violation (SQLSTATE 23505) of
    EVENT_UNIQUE_CONSTRAINT. config.settings requires PostgreSQL, whose driver
    (psycopg 3) exposes both on the exception Django wraps."""
    cause = exc.__cause__
    diag = getattr(cause, "diag", None)
    return (
        getattr(cause, "sqlstate", None) == "23505"
        and getattr(diag, "constraint_name", None) == EVENT_UNIQUE_CONSTRAINT
    )


def _enqueue_sync(merchant_id: uuid.UUID):
    def _run():
        # Local import: billing.tasks imports this module.
        from billing.tasks import sync_subscription as sync_task

        try:
            sync_task.delay(merchant_id)
        except Exception as exc:
            # Deliberately broad. The BillingEvent is already committed, so
            # whatever .delay raises (a broker outage surfaces as kombu,
            # redis or OS errors depending on the transport) must not become a
            # 500: Razorpay would retry the same event id, which is then a
            # duplicate and enqueues nothing. The event stays unprocessed for
            # the next event's task (or the sweep). Narrowing this to named
            # exception types would turn an unlisted transport error into
            # exactly that failure. Log the class name only: a connection
            # error can embed a URL.
            logger.warning(
                "Billing sync enqueue for merchant %s failed with %s", merchant_id, type(exc).__name__
            )

    return _run
