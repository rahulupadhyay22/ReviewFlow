"""Celery tasks for google_reviews (SAD.md section 5; spec 09). Thin wrappers
around google_reviews.services: no business logic lives here
(Coding-Standards.md section 1). Everything runs on the google_sync queue, and
nothing is ever scheduled with eta/countdown.

Every tenant task takes merchant_id explicitly and first (tenant_task sets the
context). The two Beat tasks take no merchant_id and read only Merchant ids,
then enqueue one tenant task per merchant; a failure enqueueing one merchant is
logged (merchant id and exception class only) and the loop goes on.

No task retries automatically (max_retries=0): the Beat schedule is the retry.
A spent authorization code in particular is never retried.
"""
import logging

from celery import shared_task

from core.tenancy import tenant_task
from google_reviews import services

logger = logging.getLogger(__name__)


def _fan_out(task, merchant_ids) -> None:
    for merchant_id in merchant_ids:
        try:
            task.delay(str(merchant_id))
        except Exception as exc:
            logger.warning("Enqueue of %s for merchant %s failed with %s", task.name, merchant_id, type(exc).__name__)


@shared_task(queue="google_sync")
def sync_google_reviews():
    """Beat, every GOOGLE_SYNC_BEAT_SECONDS: one sync task per merchant."""
    _fan_out(sync_merchant_google_reviews, services.merchant_ids_with_google())


@shared_task(queue="google_sync", max_retries=0)
@tenant_task
def sync_merchant_google_reviews(merchant_id):
    services.sync_merchant_reviews()


@shared_task(queue="google_sync", max_retries=0)
@tenant_task
def sync_location(merchant_id, google_location_pk):
    """The first sync after a mapping (enqueued on commit)."""
    services.sync_location_reviews(google_location_pk)


@shared_task(queue="google_sync")
def refresh_google_tokens():
    """Beat, daily: one refresh task per merchant."""
    _fan_out(refresh_merchant_google_tokens, services.merchant_ids_with_google())


@shared_task(queue="google_sync", max_retries=0)
@tenant_task
def refresh_merchant_google_tokens(merchant_id):
    services.refresh_merchant_tokens()


@shared_task(queue="google_sync", max_retries=0)
@tenant_task
def exchange_oauth_code(merchant_id, attempt_pk):
    """The asynchronous code exchange (spec OD-1, GA-1 rejected). The code is
    never an argument: it is taken from the encrypted column of the attempt."""
    services.exchange_attempt(attempt_pk)


@shared_task(queue="google_sync", max_retries=0)
@tenant_task
def refresh_connection_token(merchant_id, connection_pk):
    """Enqueued on commit by a request that answered 409
    google_token_refresh_pending. The same conditional refresh as the daily
    task, so running it twice is safe."""
    services.refresh_connection(connection_pk)
