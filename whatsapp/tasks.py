"""Celery tasks for whatsapp (SAD.md §5; spec 08). Thin wrappers around
whatsapp.services / customers.services -- no business logic lives here
(Coding-Standards.md §1). Nothing here sends a message, and nothing is ever
scheduled with eta/countdown.

Every tenant task takes merchant_id explicitly and first (tenant_task sets the
context). The per-merchant loops follow billing.tasks.run_billing_maintenance:
each merchant runs in its own try/with block, so one merchant's failure is
logged (merchant id and exception class only) and the loop goes on.

Two patterns live here. poll_template_statuses registers its follow-up enqueue
with transaction.on_commit inside the merchant's tenant_atomic, so it fires only
after that transaction commits. fan_out_inbound_opt_out (foundation Amendment
A-1) does NOT enqueue anything per merchant: it opts out inline and retries the
failures itself. A phone number is a task argument but is never interpolated
into a log line.
"""
import logging
import uuid
from typing import NoReturn

from celery import shared_task
from celery.utils.time import get_exponential_backoff_interval
from django.conf import settings
from django.db import transaction

from config import celery_app
from customers import services as customer_services
from core.tenancy import tenant_atomic, tenant_context, tenant_task
from whatsapp import services
from whatsapp.exceptions import OptOutFanOutIncomplete, OptOutTargetLookupFailed
from whatsapp.providers.base import ProviderTransientError

logger = logging.getLogger(__name__)


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
    # ponytail: O(merchants) scan per tick, like run_billing_maintenance.
    for merchant_id in services.active_merchant_ids():
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


OPT_OUT_MAX_RETRIES = 5
OPT_OUT_BACKOFF_FACTOR = 5  # seconds; exponential, full jitter, capped
OPT_OUT_BACKOFF_MAX = 300
OPT_OUT_LOGGED_MERCHANT_IDS_CAP = 20  # the count is always logged in full


def publish_opt_out_fan_out(phone_number_id: str, phone: str) -> None:
    """Publish one fan-out for the webhook, with a SHORT time to fail
    (foundation Amendment A-1). Meta's response timeout is undocumented
    (WhatsApp-Meta.md M-11), so the publish uses a dedicated connection with a
    short connect and socket timeout and a small publish retry.

    Two things bound the time, and BOTH are needed (measured 2026-10-13):

    - the connection options here: Celery's defaults took 4.5 s (connection
      refused) to 21 s (black-holed broker) to fail;
    - `ignore_result=True` on the task: without it Celery subscribes through
      the RESULT BACKEND's own Redis client during the publish, which these
      options do not reach. With broker and backend both unreachable that took
      about 64 s; with it, about 2.0-2.4 s. Nothing reads this task's result.

    Measured with both unreachable and a 2 s timeout: 2.0-2.4 s. The computed
    ceiling is (max_retries + 1) * timeout + max_retries * 0.2 s (4.2 s at the
    defaults), which was NOT observed. Not covered, not measured: DNS
    resolution of a broker hostname, which the socket timeouts do not bound.
    This is a short, tested bound, not a guarantee.

    Raises the broker error on failure: the caller answers 503."""
    timeout = settings.WHATSAPP_WEBHOOK_PUBLISH_CONNECT_TIMEOUT
    with celery_app.connection_for_write(
        connect_timeout=timeout,
        transport_options={"socket_connect_timeout": timeout, "socket_timeout": timeout},
    ) as connection:
        fan_out_inbound_opt_out.apply_async(
            args=(phone_number_id, phone),
            connection=connection,
            retry=True,
            retry_policy={
                "max_retries": settings.WHATSAPP_WEBHOOK_PUBLISH_MAX_RETRIES,
                "interval_start": 0,
                "interval_step": 0.2,
                "interval_max": 0.2,
            },
        )


def _retry_opt_out_fan_out(task, phone_number_id, phone, failed_ids, exc) -> NoReturn:
    """Retry (bounded) or give up LOUDLY. Never returns: it always raises,
    either the Retry that reschedules the task or `exc` once the bound is
    reached. `failed_ids` is the list of merchants still to do, or None when the
    target could not even be resolved (the whole run is repeated). Every step is
    idempotent, so a retry can never double-apply. Logs merchant ids (capped) and
    the full count, never a phone."""
    ids = None if failed_ids is None else [str(m) for m in failed_ids]
    if task.request.retries >= task.max_retries:
        logger.error(
            "whatsapp.optout_fanout_exhausted failed_merchants=%s count=%s error=%s",
            None if ids is None else ids[:OPT_OUT_LOGGED_MERCHANT_IDS_CAP],
            "all" if ids is None else len(ids),
            type(exc).__name__,
        )
        raise exc
    raise task.retry(
        args=(phone_number_id, phone),
        kwargs={"merchant_ids": ids},
        exc=exc,
        countdown=get_exponential_backoff_interval(
            factor=OPT_OUT_BACKOFF_FACTOR,
            retries=task.request.retries,
            maximum=OPT_OUT_BACKOFF_MAX,
            full_jitter=True,
        ),
    )


@shared_task(
    bind=True,
    queue="whatsapp",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=OPT_OUT_MAX_RETRIES,
    # Nothing reads this task's result (the webhook only publishes it), and a
    # stored result makes the publish call the result backend, which is not
    # bounded by publish_opt_out_fan_out's timeouts. Failures are still logged
    # by the worker and by this module; they are just not also stored in Redis.
    ignore_result=True,
)
def fan_out_inbound_opt_out(self, phone_number_id, phone, merchant_ids=None):
    """Foundation Amendment A-1 (2026-10-12). A STOP to the shared number opts
    the phone out of every merchant that has a Customer with it and a location
    mapped to the shared account (OD-1 (b)); a STOP to an own number opts it out
    of that number's merchant only (Embedded Signup spec ES-3, no status
    filter). An unknown number is a no-op.

    The opt-out is applied INLINE, merchant by merchant, each in its own
    tenant context and transaction: there is no second broker hop. Every
    merchant is attempted. Failures are collected and, after the loop, retried
    (bounded, exponential backoff) restricted to the merchants that failed, via
    `merchant_ids`. Nothing is dropped silently: exhaustion is logged at error
    level and raised. acks_late with reject_on_worker_lost redelivers the run
    if the worker dies, which is safe because everything is idempotent.

    A phone number is a task argument but is never interpolated into a log
    line."""
    lookup_error = None
    try:
        shared, universe = services.resolve_inbound_opt_out_targets(phone_number_id)
    except Exception as exc:
        lookup_error = type(exc).__name__
        logger.warning("Inbound opt-out target lookup failed with %s", lookup_error)
    if lookup_error is not None:
        # Outside the except block, and with a class-name-only exception: the
        # raw database/driver error text must not reach Celery's retry or
        # failure logs, and no implicit exception chain may carry it either.
        _retry_opt_out_fan_out(
            self, phone_number_id, phone, None, OptOutTargetLookupFailed(lookup_error)
        )

    if merchant_ids is not None:  # a retry: only the merchants that failed
        # Written by this task's own retry, never by a request. A malformed
        # value (a manual re-drive) raises ValueError here, outside the try, on
        # purpose: it is a caller bug, not an outage to retry.
        wanted = {uuid.UUID(str(m)) for m in merchant_ids}
        universe = [m for m in universe if m in wanted]

    failed = []
    for merchant_id in universe:
        try:
            services.opt_out_inbound_for_merchant(merchant_id, phone, shared=shared)
        except Exception as exc:
            failed.append(merchant_id)
            logger.warning(
                "Inbound opt-out for merchant %s failed with %s", merchant_id, type(exc).__name__
            )
    if failed:
        _retry_opt_out_fan_out(self, phone_number_id, phone, failed, OptOutFanOutIncomplete(len(failed)))


@shared_task(queue="whatsapp", acks_late=True, reject_on_worker_lost=True)
@tenant_task
def process_inbound_opt_out(merchant_id, phone):
    """Idempotent, tenant-scoped opt-out of one phone for one merchant. Since
    foundation Amendment A-1 the fan-out no longer enqueues this (it opts out
    inline); it is kept for a manual re-drive."""
    customer_services.opt_out_customer(phone=phone, source="INBOUND_KEYWORD")


@shared_task(
    queue="default",
    autoretry_for=(ProviderTransientError,),
    retry_backoff=True,
    max_retries=5,
)
@tenant_task
def activate_own_number(merchant_id, account_id):
    """Spec 08-whatsapp-embedded-signup activation: ids only, never a code,
    token or PIN (the encrypted token is read from the row). After the retry
    cap the number stays PENDING and the merchant's recovery is to reconnect
    (ES-12). Meta management calls stay off the `whatsapp` queue."""
    services.activate_own_number(account_id)
