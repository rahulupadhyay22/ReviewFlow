"""Celery tasks for whatsapp (SAD.md §5; spec 08). Thin wrappers around
whatsapp.services / customers.services -- no business logic lives here
(Coding-Standards.md §1). Nothing here sends a message, and nothing is ever
scheduled with eta/countdown.

Every tenant task takes merchant_id explicitly and first (tenant_task sets the
context). The per-merchant fan-outs follow billing.tasks.run_billing_maintenance:
each merchant runs in its own try/with block, so one merchant's failure is
logged (merchant id and exception class only) and the loop goes on, and the
follow-up enqueue is registered with transaction.on_commit inside that
merchant's tenant_atomic -- it fires only after the transaction it belongs to
commits, and never on rollback. A phone number is a task argument but is never
interpolated into a log line.
"""
import logging

from celery import shared_task
from django.db import transaction

from accounts.models import Merchant
from customers import services as customer_services
from core.tenancy import tenant_atomic, tenant_context, tenant_task
from whatsapp import services
from whatsapp.providers.base import ProviderTransientError

logger = logging.getLogger(__name__)


def _active_merchant_ids() -> list:
    # Merchant is a global, non-RLS table, so reading its ids is not an RLS
    # bypass. # ponytail: O(merchants) scan per tick, like run_billing_maintenance.
    return list(Merchant.objects.exclude(status=Merchant.Status.DELETED).values_list("id", flat=True))


def _guarded_delay(task, *args):
    """An on-commit callback whose broker error cannot skip other merchants."""

    def _run():
        try:
            task.delay(*args)
        except Exception as exc:
            logger.warning("Enqueue of %s failed with %s", task.name, type(exc).__name__)

    return _run


@shared_task(
    queue="whatsapp",
    autoretry_for=(ProviderTransientError,),
    retry_backoff=True,
    max_retries=5,
)
@tenant_task
def submit_template(merchant_id, template_id):
    """After the cap the template stays PENDING without a provider_template_id.

    # ponytail: known V1 limitation (spec 08 final review): such a template is
    # never picked up again, because poll_template_statuses syncs only templates
    # that have a provider_template_id, and a permanent lookup error is not
    # retried at all. Upgrade path: have the poll also re-enqueue submit_template
    # for PENDING rows with no provider_template_id older than an updated_at floor
    # (submit_template_to_provider is already idempotent).
    """
    services.submit_template_to_provider(template_id)


@shared_task(queue="default")
@tenant_task
def sync_merchant_templates(merchant_id):
    services.sync_template_statuses()


@shared_task(queue="default")
def poll_template_statuses():
    """Beat. One sync_merchant_templates per merchant with a submitted PENDING
    template."""
    for merchant_id in _active_merchant_ids():
        try:
            with tenant_context(merchant_id), tenant_atomic():
                if services.templates_pending_sync():
                    transaction.on_commit(_guarded_delay(sync_merchant_templates, merchant_id))
        except Exception as exc:
            logger.warning(
                "Template poll for merchant %s failed with %s", merchant_id, type(exc).__name__
            )


@shared_task(queue="default")
def monitor_shared_pool_quality():
    services.check_shared_pool_quality()


@shared_task(queue="whatsapp")
def fan_out_inbound_opt_out(phone_number_id, phone):
    """OD-1 (b): a STOP to the shared number opts the phone out of every
    merchant that has a Customer with it and a location mapped to the shared
    account. An unknown or non-shared number is a no-op (the webhook already
    answered 200)."""
    if not services.is_shared_pool_number(phone_number_id):
        return
    for merchant_id in _active_merchant_ids():
        try:
            with tenant_context(merchant_id), tenant_atomic():
                if services.shared_opt_out_applies(phone):
                    transaction.on_commit(_guarded_delay(process_inbound_opt_out, merchant_id, phone))
        except Exception as exc:
            logger.warning(
                "Inbound opt-out fan-out for merchant %s failed with %s", merchant_id, type(exc).__name__
            )


@shared_task(queue="whatsapp")
@tenant_task
def process_inbound_opt_out(merchant_id, phone):
    customer_services.opt_out_customer(phone=phone, source="INBOUND_KEYWORD")
