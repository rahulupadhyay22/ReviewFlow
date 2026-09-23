"""Unit tests for events/services.py: the event ingestion pipeline (spec
Definition of done "Inbox and idempotency", "Pipeline", "Sales with and
without a customer phone", "Safe error storage", "Disconnect and
CANCELLED")."""
import logging
import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone as dj_timezone

from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context
from customers.models import Customer
from events import services as events_services
from events.models import IntegrationEvent
from events.tests.conftest import make_payload
from integrations import services as integration_services
from integrations.core.registry import ADAPTERS
from integrations.exceptions import IntegrationDisconnected
from integrations.models import Integration
from transactions.models import Transaction

pytestmark = pytest.mark.django_db


def _record(owner, integration, external_event_id, payload):
    with tenant_context(owner.merchant_id), tenant_atomic():
        return events_services.record_event(
            integration=integration, external_event_id=external_event_id, payload=payload
        )


# --- record_event: invariants and idempotency ---------------------------


def test_record_event_sets_merchant_and_source_from_integration(make_merchant, make_integration):
    owner = make_merchant("A")
    integration = make_integration(owner.merchant)
    event, created = _record(owner, integration, "1", make_payload())
    assert created is True
    assert event.merchant_id == integration.merchant_id
    assert event.source == integration.provider
    assert event.status == IntegrationEvent.Status.RECEIVED
    assert event.received_at is not None


def test_record_event_requires_a_tenant_context(make_merchant, make_integration):
    owner = make_merchant("A")
    integration = make_integration(owner.merchant)
    with pytest.raises(TenantContextError):
        events_services.record_event(integration=integration, external_event_id="1", payload=make_payload())


def test_record_event_requires_matching_tenant_context(make_merchant, make_integration):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    int_a = make_integration(owner_a.merchant)
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        with pytest.raises(TenantContextError):
            events_services.record_event(integration=int_a, external_event_id="1", payload=make_payload())


def test_record_event_rejects_disconnected_integration(make_merchant, make_integration):
    owner = make_merchant("A")
    integration = make_integration(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration_services.disconnect_integration(actor=owner, integration=integration)
        integration.refresh_from_db()
        with pytest.raises(IntegrationDisconnected):
            events_services.record_event(integration=integration, external_event_id="1", payload=make_payload())


def test_record_event_duplicate_five_times_creates_exactly_one_event_and_transaction(
    make_merchant, make_location, make_integration, make_mapping
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)
    payload = make_payload()

    with tenant_context(owner.merchant_id), tenant_atomic():
        results = [
            events_services.record_event(integration=integration, external_event_id="evt-1", payload=payload)
            for _ in range(5)
        ]
    event_ids = {r[0].id for r in results}
    created_flags = [r[1] for r in results]
    assert len(event_ids) == 1
    assert created_flags == [True, False, False, False, False]

    event_id = next(iter(event_ids))
    with tenant_context(owner.merchant_id), tenant_atomic():
        events_services.process_event(event_id)
        events_services.process_event(event_id)  # second call: PROCESSED no-op
        assert Transaction.objects.count() == 1
        assert IntegrationEvent.objects.count() == 1


def test_cross_merchant_same_external_event_id_creates_two_separate_events(make_merchant, make_integration):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    int_a = make_integration(owner_a.merchant)
    int_b = make_integration(owner_b.merchant)
    payload = make_payload()

    event_a, created_a = _record(owner_a, int_a, "1", payload)
    event_b, created_b = _record(owner_b, int_b, "1", payload)

    assert created_a is True
    assert created_b is True
    assert event_a.id != event_b.id
    assert event_a.merchant_id == owner_a.merchant_id
    assert event_b.merchant_id == owner_b.merchant_id


def test_same_external_event_id_across_two_integrations_same_merchant_creates_two_events(
    make_merchant, make_integration
):
    owner = make_merchant("A")
    int1 = make_integration(owner.merchant)
    int2 = make_integration(owner.merchant)
    payload = make_payload()

    with tenant_context(owner.merchant_id), tenant_atomic():
        event1, created1 = events_services.record_event(integration=int1, external_event_id="1", payload=payload)
        event2, created2 = events_services.record_event(integration=int2, external_event_id="1", payload=payload)

    assert created1 is True
    assert created2 is True
    assert event1.id != event2.id


# --- process_event: the full pipeline ------------------------------------


def test_process_event_success_with_phone_creates_customer_and_transaction(
    make_merchant, make_location, make_integration, make_mapping
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)
    payload = make_payload(customer_phone="+919999999999", customer_name="Rahul")

    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(integration=integration, external_event_id="1", payload=payload)
        processed = events_services.process_event(event.id)

        assert processed.status == IntegrationEvent.Status.PROCESSED
        assert processed.location_id == location.id
        assert processed.processed_at is not None
        assert processed.error_code is None
        assert processed.error_message is None

        txn = Transaction.objects.get()
        assert txn.customer.phone == "+919999999999"
        assert Customer.objects.count() == 1


def test_process_event_success_without_phone_creates_transaction_only(
    make_merchant, make_location, make_integration, make_mapping
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)
    payload = make_payload()

    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(integration=integration, external_event_id="1", payload=payload)
        processed = events_services.process_event(event.id)

        assert processed.status == IntegrationEvent.Status.PROCESSED
        assert processed.attempt_count == 0
        assert processed.error_code is None

        txn = Transaction.objects.get()
        assert txn.customer is None
        assert Customer.objects.count() == 0


def test_process_event_duplicate_transaction_still_ends_processed(
    make_merchant, make_location, make_integration, make_mapping
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    with tenant_context(owner.merchant_id), tenant_atomic():
        event1, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload()
        )
        events_services.process_event(event1.id)

        event2, _ = events_services.record_event(
            integration=integration, external_event_id="2", payload=make_payload()  # same external_transaction_id
        )
        processed2 = events_services.process_event(event2.id)

        assert processed2.status == IntegrationEvent.Status.PROCESSED
        assert Transaction.objects.count() == 1


def test_duplicate_task_style_call_produces_one_transaction(make_merchant, make_location, make_integration, make_mapping):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload()
        )
        events_services.process_event(event.id)
        second = events_services.process_event(event.id)  # duplicate call
        assert second.status == IntegrationEvent.Status.PROCESSED
        assert Transaction.objects.count() == 1


# --- location resolution failure ------------------------------------------


def test_process_event_location_unresolved_when_no_mapping_exists(make_merchant, make_integration):
    owner = make_merchant("A")
    integration = make_integration(owner.merchant)  # no mapping created
    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload()
        )
        processed = events_services.process_event(event.id)

    assert processed.status == IntegrationEvent.Status.FAILED
    assert processed.error_code == "LOCATION_UNRESOLVED"
    assert (processed.error_code, processed.error_message) in events_services.SAFE_ERRORS.items()


# --- adapter not found ------------------------------------------------


def test_process_event_adapter_not_found(make_merchant, make_location, make_integration, make_mapping, monkeypatch):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)
    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload()
        )

    monkeypatch.delitem(ADAPTERS, "webhook")

    with tenant_context(owner.merchant_id), tenant_atomic():
        processed = events_services.process_event(event.id)

    assert processed.status == IntegrationEvent.Status.FAILED
    assert processed.error_code == "ADAPTER_NOT_FOUND"
    assert (processed.error_code, processed.error_message) in events_services.SAFE_ERRORS.items()


# --- deterministic validation failures map to safe codes --------------


@pytest.mark.parametrize(
    "overrides, expected_code",
    [
        ({"customer_phone": "98765"}, "INVALID_PHONE"),
        ({"currency": "inr"}, "INVALID_CURRENCY"),
        ({"amount": "-1"}, "INVALID_AMOUNT"),
        ({"occurred_at": "2024-01-01T10:00:00"}, "INVALID_OCCURRED_AT"),  # naive datetime
        ({"external_transaction_id": ""}, "INVALID_EXTERNAL_TRANSACTION_ID"),
        ({"customer_name": "x" * 256}, "PAYLOAD_INVALID"),
    ],
)
def test_process_event_validation_failures_map_to_safe_error_codes(
    overrides, expected_code, make_merchant, make_location, make_integration, make_mapping
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)
    payload = make_payload(**overrides)

    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(integration=integration, external_event_id="1", payload=payload)
        processed = events_services.process_event(event.id)

        assert processed.status == IntegrationEvent.Status.FAILED
        assert processed.attempt_count == 1
        assert processed.error_code == expected_code
        assert (processed.error_code, processed.error_message) in events_services.SAFE_ERRORS.items()
        assert Transaction.objects.count() == 0
        assert Customer.objects.count() == 0


# --- PII cannot leak into stored fields or logs --------------------------


def test_unexpected_value_error_maps_to_processing_error_and_leaks_no_pii(
    make_merchant, make_location, make_integration, make_mapping, caplog
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)
    secret = '+919999999999 secret-token {"customer_phone": "+919999999999"}'
    payload = make_payload(__raise__="value_error", __raise_message__=secret)

    with caplog.at_level(logging.WARNING):
        with tenant_context(owner.merchant_id), tenant_atomic():
            event, _ = events_services.record_event(
                integration=integration, external_event_id="1", payload=payload
            )
            processed = events_services.process_event(event.id)

    assert processed.status == IntegrationEvent.Status.FAILED
    assert processed.error_code == "PROCESSING_ERROR"
    assert processed.error_message == "Event processing failed"
    assert (processed.error_code, processed.error_message) in events_services.SAFE_ERRORS.items()

    assert "+919999999999" not in (processed.error_message or "")
    assert "secret-token" not in (processed.error_message or "")
    assert "+919999999999" not in caplog.text
    assert "secret-token" not in caplog.text


def test_unexpected_reviewflow_error_also_maps_to_processing_error_and_leaks_no_pii(
    make_merchant, make_location, make_integration, make_mapping, caplog
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)
    secret = "+919999999999 secret-token"
    payload = make_payload(__raise__="review_flow_error", __raise_message__=secret)

    with caplog.at_level(logging.WARNING):
        with tenant_context(owner.merchant_id), tenant_atomic():
            event, _ = events_services.record_event(
                integration=integration, external_event_id="1", payload=payload
            )
            processed = events_services.process_event(event.id)

    assert processed.error_code == "PROCESSING_ERROR"
    assert processed.error_message == "Event processing failed"
    assert "+919999999999" not in caplog.text
    assert "secret-token" not in caplog.text


def test_process_event_exception_after_customer_insert_rolls_back_everything(
    make_merchant, make_location, make_integration, make_mapping, monkeypatch
):
    """A failure inside record_sale, after get_or_create_customer already
    created a Customer but before the Transaction insert, must roll back
    the Customer too -- process_event wraps the whole attempt in one
    savepoint."""
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(Transaction.objects, "create", boom)

    payload = make_payload(customer_phone="+919999999999")
    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(integration=integration, external_event_id="1", payload=payload)
        processed = events_services.process_event(event.id)

        assert processed.status == IntegrationEvent.Status.FAILED
        assert processed.error_code == "PROCESSING_ERROR"
        assert Transaction.objects.count() == 0
        assert Customer.objects.count() == 0


# --- CANCELLED is terminal ------------------------------------------------


def test_process_event_on_cancelled_event_is_a_no_op(make_merchant, make_location, make_integration, make_mapping):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload()
        )
        event.status = IntegrationEvent.Status.CANCELLED
        event.save(update_fields=["status"])
        before_updated_at = event.updated_at

        processed = events_services.process_event(event.id)

        assert processed.status == IntegrationEvent.Status.CANCELLED
        assert processed.updated_at == before_updated_at
        assert Transaction.objects.count() == 0
        assert Customer.objects.count() == 0


def test_process_event_race_guard_cancels_event_when_integration_already_disconnected(
    make_merchant, make_location, make_integration, make_mapping
):
    """Simulates the residual state after the late-insert race (spec
    Decision 18): the integration is DISCONNECTED, but this event is still
    RECEIVED because it was inserted after disconnect's own cancellation
    UPDATE had already run. process_event must re-check the integration
    itself, not rely on disconnect having already caught it."""
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload()
        )
        Integration.objects.filter(pk=integration.pk).update(status=Integration.Status.DISCONNECTED)

        processed = events_services.process_event(event.id)

        assert processed.status == IntegrationEvent.Status.CANCELLED
        assert Transaction.objects.count() == 0
        assert Customer.objects.count() == 0


# --- due_event_ids: backoff, stale RECEIVED, and terminal exclusion -----


def test_due_event_ids_failed_not_due_then_due_after_backoff_window(
    make_merchant, make_location, make_integration, make_mapping
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload(customer_phone="98765")
        )
        events_services.process_event(event.id)
        event.refresh_from_db()
        assert event.status == IntegrationEvent.Status.FAILED
        assert event.attempt_count == 1

        assert event.id not in events_services.due_event_ids()

        IntegrationEvent.objects.filter(pk=event.id).update(
            updated_at=dj_timezone.now() - timedelta(minutes=6)
        )
        assert event.id in events_services.due_event_ids()


def test_due_event_ids_stale_received_is_due_fresh_received_is_not(
    make_merchant, make_location, make_integration, make_mapping
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    with tenant_context(owner.merchant_id), tenant_atomic():
        fresh, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload()
        )
        stale, _ = events_services.record_event(
            integration=integration, external_event_id="2", payload=make_payload()
        )
        IntegrationEvent.objects.filter(pk=stale.id).update(
            received_at=dj_timezone.now() - timedelta(minutes=11)
        )

        due = events_services.due_event_ids()
        assert stale.id in due
        assert fresh.id not in due


def test_due_event_ids_never_includes_terminal_statuses(
    make_merchant, make_location, make_integration, make_mapping
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    with tenant_context(owner.merchant_id), tenant_atomic():
        processed_event, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload()
        )
        events_services.process_event(processed_event.id)

        dead, _ = events_services.record_event(
            integration=integration, external_event_id="2", payload=make_payload(customer_phone="98765")
        )
        for _ in range(events_services.MAX_EVENT_ATTEMPTS):
            IntegrationEvent.objects.filter(pk=dead.id).update(
                updated_at=dj_timezone.now() - timedelta(days=1)
            )
            events_services.process_event(dead.id)
        dead.refresh_from_db()
        assert dead.status == IntegrationEvent.Status.DEAD_LETTER

        cancelled, _ = events_services.record_event(
            integration=integration, external_event_id="3", payload=make_payload()
        )
        cancelled.status = IntegrationEvent.Status.CANCELLED
        cancelled.save(update_fields=["status"])

        due = events_services.due_event_ids()
        assert processed_event.id not in due
        assert dead.id not in due
        assert cancelled.id not in due


# --- process_event locks the event row ONLY (spec Decision 20) -------------


def test_process_event_locks_only_the_event_row_not_the_joined_integration(
    make_merchant, make_location, make_integration, make_mapping
):
    """Regression guard for the review finding: select_related("integration")
    joins integrations_integration, so a bare select_for_update() would emit
    FOR UPDATE over the whole join and lock the Integration row too --
    recreating the deadlock spec Decision 20 avoids (disconnect_integration
    holds FOR NO KEY UPDATE on the Integration and waits on an event row,
    while process_event would hold that event row and want FOR UPDATE on the
    same Integration). The lock must name the event table only."""
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload()
        )
        with CaptureQueriesContext(connection) as ctx:
            events_services.process_event(event.id)

    locking = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"].upper()]
    assert locking, "expected process_event to take a row lock on the event"

    for sql in locking:
        upper = sql.upper()
        assert 'FOR UPDATE OF "EVENTS_INTEGRATIONEVENT"' in upper, sql
        # The joined Integration table must never appear in the lock clause.
        lock_clause = upper[upper.index("FOR UPDATE") :]
        assert "INTEGRATIONS_INTEGRATION" not in lock_clause, sql
