"""Event ingestion pipeline services (Coding-Standards.md §1;
Event-Processing.md; spec Decisions 6-9, 17-19).

record_event() is the inbox write. process_event() is one processing
attempt. due_event_ids() is the retry/stale-sweep query used by the Beat
task. Every function requires a tenant context.
"""
import logging
import traceback
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone as dj_timezone

from core.exceptions import TenantContextError
from core.tenancy import get_current_merchant_id, tenant_atomic
from events.models import IntegrationEvent
from integrations.core.registry import AdapterNotFound, get_adapter
from integrations.core.schemas import PayloadValidationError
from integrations.exceptions import IntegrationDisconnected, LocationUnresolved
from integrations.models import Integration
from integrations.services import resolve_location
from transactions.services import record_sale

logger = logging.getLogger(__name__)

MAX_EVENT_ATTEMPTS = 5
RETRY_BASE = timedelta(minutes=5)
STALE_RECEIVED_AFTER = timedelta(minutes=10)

# The ONLY source of stored error_code/error_message (spec Decision 19).
# Never built from str(exc), payload values, phone numbers, credentials,
# tokens or request bodies -- every entry here is fixed, PII-safe text.
SAFE_ERRORS: dict[str, str] = {
    "ADAPTER_NOT_FOUND": "No adapter is registered for this integration's provider.",
    "PAYLOAD_INVALID": "The event payload failed validation.",
    "INVALID_PHONE": "The customer phone is not a valid E.164 number.",
    "INVALID_CURRENCY": "The currency is not a valid ISO 4217 code.",
    "INVALID_AMOUNT": "The amount is missing or negative.",
    "INVALID_OCCURRED_AT": "The sale time is missing or has no timezone.",
    "INVALID_EXTERNAL_TRANSACTION_ID": "The external transaction ID is missing or too long.",
    "LOCATION_UNRESOLVED": "The sale could not be mapped to a location.",
    "PROCESSING_ERROR": "Event processing failed",
}


def record_event(
    *, integration: Integration, external_event_id: str, payload: dict
) -> tuple[IntegrationEvent, bool]:
    """The inbox write, called by a Phase 06 receiver after verify().
    Requires an active tenant context equal to integration.merchant_id --
    this makes IntegrationEvent.merchant_id == Integration.merchant_id an
    enforced invariant, never resolved after the row exists."""
    merchant_id = get_current_merchant_id()
    if merchant_id is None or merchant_id != integration.merchant_id:
        raise TenantContextError(
            "record_event() requires a tenant context matching the integration's merchant."
        )
    if integration.status == Integration.Status.DISCONNECTED:
        raise IntegrationDisconnected("Cannot record an event for a disconnected integration.")

    with tenant_atomic():
        try:
            with transaction.atomic():
                event = IntegrationEvent.objects.create(
                    merchant_id=integration.merchant_id,
                    integration=integration,
                    source=integration.provider,
                    external_event_id=external_event_id,
                    payload=payload,
                    status=IntegrationEvent.Status.RECEIVED,
                    received_at=dj_timezone.now(),
                )
        except IntegrityError:
            # No enqueue, no reprocessing: a re-delivered event is a no-op.
            existing = IntegrationEvent.objects.get(
                integration=integration, external_event_id=external_event_id
            )
            return existing, False

        transaction.on_commit(_enqueue_processing(merchant_id, event.id))
        return event, True


def _enqueue_processing(merchant_id: uuid.UUID, event_id: uuid.UUID):
    def _run():
        # Local import breaks the events.services <-> events.tasks cycle.
        from events.tasks import process_integration_event

        process_integration_event.delay(merchant_id, event_id)

    return _run


def _safe_error(exc: Exception) -> tuple[str, str]:
    """Maps an exception to a stable (error_code, error_message) pair from
    SAFE_ERRORS only. NEVER reads str(exc) or exc.args: a database
    unique-violation message can embed row values, and an adapter's own
    exception text could embed payload content or a phone number."""
    if isinstance(exc, AdapterNotFound):
        code = "ADAPTER_NOT_FOUND"
    elif isinstance(exc, PayloadValidationError):
        code = exc.code if exc.code in SAFE_ERRORS else "PAYLOAD_INVALID"
    elif isinstance(exc, LocationUnresolved):
        code = "LOCATION_UNRESOLVED"
    else:
        code = "PROCESSING_ERROR"
    return code, SAFE_ERRORS[code]


def _is_due(event: IntegrationEvent) -> bool:
    backoff = RETRY_BASE * (2 ** max(event.attempt_count - 1, 0))
    return event.updated_at <= dj_timezone.now() - backoff


def process_event(event_id: uuid.UUID | str) -> IntegrationEvent:
    """One processing attempt, inside tenant_atomic()."""
    with tenant_atomic():
        # of=("self",) is required, not optional: select_related("integration")
        # joins integrations_integration, and a bare select_for_update() would
        # emit FOR UPDATE over the whole join, locking the Integration row too.
        # That would recreate exactly the deadlock spec Decision 20 avoids --
        # disconnect_integration holds FOR NO KEY UPDATE on the Integration and
        # waits on an event row, while this would hold the event row and want
        # FOR UPDATE on that same Integration. process_event locks the event
        # row only; it never locks the Integration.
        event = (
            IntegrationEvent.objects.select_for_update(of=("self",))
            .select_related("integration")
            .get(pk=event_id)
        )

        if event.status in (
            IntegrationEvent.Status.PROCESSED,
            IntegrationEvent.Status.DEAD_LETTER,
            IntegrationEvent.Status.CANCELLED,
        ):
            return event
        if event.status == IntegrationEvent.Status.FAILED and not _is_due(event):
            return event

        # Race guard (spec Decision 18): an event that committed as RECEIVED
        # just after a concurrent disconnect's cancellation UPDATE ran.
        if event.integration.status == Integration.Status.DISCONNECTED:
            event.status = IntegrationEvent.Status.CANCELLED
            event.save(update_fields=["status", "updated_at"])
            return event

        try:
            with transaction.atomic():
                adapter = get_adapter(event.integration)
                sale = adapter.normalize(adapter.parse(event.payload))
                location = resolve_location(event.integration, sale)
                record_sale(integration=event.integration, location=location, sale=sale)
        except Exception as exc:  # every failure must be caught and stored safely
            _record_failure(event, exc)
            return event

        event.status = IntegrationEvent.Status.PROCESSED
        event.location = location
        event.processed_at = dj_timezone.now()
        event.error_code = None
        event.error_message = None
        event.save(
            update_fields=[
                "status",
                "location",
                "processed_at",
                "error_code",
                "error_message",
                "updated_at",
            ]
        )
        return event


def _record_failure(event: IntegrationEvent, exc: Exception) -> None:
    event.attempt_count += 1
    event.error_code, event.error_message = _safe_error(exc)
    event.status = (
        IntegrationEvent.Status.DEAD_LETTER
        if event.attempt_count >= MAX_EVENT_ATTEMPTS
        else IntegrationEvent.Status.FAILED
    )
    event.save(update_fields=["attempt_count", "error_code", "error_message", "status", "updated_at"])

    # Never log the exception message, the payload, a phone number,
    # credentials or tokens -- only identifiers, the class name, and (for
    # the generic PROCESSING_ERROR bucket) the stack frames.
    if event.error_code == "PROCESSING_ERROR":
        logger.error(
            "IntegrationEvent %s (merchant %s) failed with %s\n%s",
            event.id,
            event.merchant_id,
            type(exc).__name__,
            "".join(traceback.format_tb(exc.__traceback__)),
        )
    else:
        logger.warning(
            "IntegrationEvent %s (merchant %s) failed with %s (%s)",
            event.id,
            event.merchant_id,
            type(exc).__name__,
            event.error_code,
        )


def due_event_ids() -> list[uuid.UUID]:
    """Runs for the current tenant. FAILED events due by backoff, plus
    RECEIVED events older than STALE_RECEIVED_AFTER (spec Decision 8).
    select_for_update(skip_locked=True) so an event already being processed
    by another worker is skipped, never selected twice. CANCELLED,
    DEAD_LETTER and PROCESSED are excluded by the status filter, so none of
    them is ever retried."""
    now = dj_timezone.now()
    stale_received_cutoff = now - STALE_RECEIVED_AFTER

    candidates = list(
        IntegrationEvent.objects.select_for_update(skip_locked=True).filter(
            status__in=[IntegrationEvent.Status.FAILED, IntegrationEvent.Status.RECEIVED]
        )
    )
    due_ids = []
    for event in candidates:
        if event.status == IntegrationEvent.Status.RECEIVED:
            if event.received_at is not None and event.received_at < stale_received_cutoff:
                due_ids.append(event.id)
        elif _is_due(event):
            due_ids.append(event.id)
    return due_ids
