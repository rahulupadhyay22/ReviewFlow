"""Celery tasks for event ingestion (SAD.md §5; spec Decisions 3, 8, 9).

Thin wrappers around events.services -- no business logic lives here
(Coding-Standards.md §1). Never scheduled with eta/countdown: dispatch here
is Beat-interval + timestamp-comparison backoff, not broker-side scheduling.
"""
from celery import shared_task
from django.db import transaction

from accounts.models import Merchant
from core.tenancy import tenant_atomic, tenant_context, tenant_task
from events import services


@shared_task(queue="events")
@tenant_task
def process_integration_event(merchant_id, event_id):
    """merchant_id is explicit and first (Multi-Tenancy.md §Layer 1) --
    tenant_task sets the tenant context before any query, and merchant_id is
    never re-derived from the event itself."""
    services.process_event(event_id)


def _enqueue_retry(merchant_id, event_id):
    def _run():
        process_integration_event.delay(merchant_id, event_id)

    return _run


@shared_task(queue="events")
def retry_failed_events():
    """Merchant-agnostic by design (spec Decision 9). Merchant is a global,
    non-RLS table, so iterating it and entering each merchant's own tenant
    context is not an RLS bypass -- there is no BYPASSRLS and no privileged
    connection anywhere in this task.

    # ponytail: O(active merchants) scan per Beat tick; upgrade path is a
    # narrow, audited SQL function returning due (merchant_id, event_id) pairs
    # directly, if the merchant count ever makes this loop too slow.
    """
    # SUSPENDED/DELETED merchants are skipped: their events stay put, they are
    # not cancelled or advanced, and they are picked up again if the merchant
    # returns to ACTIVE. This only narrows which merchants are swept -- retry
    # semantics for an ACTIVE merchant are unchanged.
    active_merchant_ids = Merchant.objects.filter(status=Merchant.Status.ACTIVE).values_list(
        "id", flat=True
    )
    for merchant_id in active_merchant_ids:
        with tenant_context(merchant_id), tenant_atomic():
            for event_id in services.due_event_ids():
                transaction.on_commit(_enqueue_retry(merchant_id, event_id))
