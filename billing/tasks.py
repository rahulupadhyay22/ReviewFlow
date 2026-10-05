"""Celery tasks for billing (SAD.md §5; spec 07). Thin wrappers around
billing.services -- no business logic lives here (Coding-Standards.md §1).
Never scheduled with eta/countdown, and none retries on its own: the only
retry is the next 15-minute `run_billing_maintenance` tick.

- `sync_subscription`: the task the webhook receiver enqueues (receive order,
  step 7). It skips the provider fetch when the merchant has no unprocessed
  BillingEvent.
- `maintain_subscription`: the sweep's provider sync. It calls the sync
  directly, never the pending-event skip, so the sweep always fetches.
- `advance_subscription_dunning`: the sweep's dunning step (day 3 / day 6
  checkpoints, day-7 expiry, owed provider cancel). It is a separate message
  from the sync on purpose, so expiry cannot be stalled by a sync that fails,
  hangs or is killed.
"""
import logging

from celery import shared_task
from django.db import transaction

from accounts.models import Merchant
from billing import services
from core.tenancy import tenant_atomic, tenant_context, tenant_task

logger = logging.getLogger(__name__)


@shared_task(queue="default")
@tenant_task
def sync_subscription(merchant_id):
    """merchant_id is explicit and first (Multi-Tenancy.md §Layer 1):
    tenant_task sets the tenant context before any query. Fetches only while
    the merchant has an unprocessed BillingEvent (services.sync_pending_events)."""
    services.sync_pending_events()


@shared_task(queue="default")
@tenant_task
def maintain_subscription(merchant_id):
    """The sweep's provider sync: always fetches (spec 07 W7)."""
    services.sync_subscription()


@shared_task(queue="default")
@tenant_task
def advance_subscription_dunning(merchant_id):
    """The sweep's dunning step. Never fetches or applies a snapshot."""
    services.advance_dunning()


def _enqueue_maintenance(merchant_id):
    """The on-commit callback for one due merchant: its two independent tasks.
    Each `.delay` has its own guard (a callback that raises would stop the
    ones registered after it, and a failed first enqueue must not skip the
    second). Class name only is logged: a broker error can embed a URL."""

    def _run():
        for task in (maintain_subscription, advance_subscription_dunning):
            try:
                task.delay(merchant_id)
            except Exception as exc:
                logger.warning(
                    "Billing maintenance enqueue of %s for merchant %s failed with %s",
                    task.name,
                    merchant_id,
                    type(exc).__name__,
                )

    return _run


@shared_task(queue="default")
def run_billing_maintenance():
    """Beat, every 900 seconds. Merchant is a global, non-RLS table, so reading
    its ids and entering each merchant's own tenant context is not an RLS
    bypass -- there is no BYPASSRLS and no privileged connection. `SUSPENDED`
    merchants keep syncing (O14); only `DELETED` ones are skipped.

    Each merchant is handled on its own: a failure checking or enqueueing one
    is logged (merchant id and exception class name only) and the loop goes on;
    that merchant is tried again only on the next tick.

    # ponytail: O(merchants) scan per Beat tick, like retry_failed_events;
    # upgrade path is a narrow, audited SQL function returning the due
    # merchant ids directly, if the merchant count ever makes this slow.
    """
    merchant_ids = list(
        Merchant.objects.exclude(status=Merchant.Status.DELETED).values_list("id", flat=True)
    )
    for merchant_id in merchant_ids:
        try:
            with tenant_context(merchant_id), tenant_atomic():
                if services.maintenance_due():
                    transaction.on_commit(_enqueue_maintenance(merchant_id))
        except Exception as exc:
            logger.warning(
                "Billing maintenance for merchant %s failed with %s", merchant_id, type(exc).__name__
            )
