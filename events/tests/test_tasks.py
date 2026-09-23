"""Tests for events/tasks.py: Celery merchant context, on_commit
enqueueing, retry/backoff, dead-letter, and the Beat schedule (spec
Definition of done "Retry and dead letter", "Celery / Retries")."""
import inspect
from datetime import timedelta

import pytest
from django.conf import settings
from django.db import transaction
from django.utils import timezone as dj_timezone

from accounts.models import Merchant
from core.tenancy import tenant_atomic, tenant_context
from customers.models import Customer
from events import services as events_services
from events import tasks as events_tasks
from events.models import IntegrationEvent
from events.tests.conftest import make_payload
from transactions.models import Transaction

pytestmark = pytest.mark.django_db


def test_beat_schedule_registers_retry_failed_events():
    entry = settings.CELERY_BEAT_SCHEDULE["retry-failed-events"]
    assert entry["task"] == "events.tasks.retry_failed_events"
    assert entry["schedule"] == 300.0
    assert entry["options"] == {"queue": "events"}


def test_tasks_module_never_uses_eta_or_countdown():
    source = inspect.getsource(events_tasks)
    assert "eta=" not in source
    assert "countdown=" not in source


# --- process_integration_event: tenant context -----------------------------


def test_process_integration_event_sets_tenant_context_and_processes(
    make_merchant, make_location, make_integration, make_mapping
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload()
        )

    events_tasks.process_integration_event(owner.merchant_id, event.id)

    with tenant_context(owner.merchant_id), tenant_atomic():
        event.refresh_from_db()
    assert event.status == IntegrationEvent.Status.PROCESSED


def test_process_integration_event_cannot_read_or_write_another_merchants_event(
    make_merchant, make_location, make_integration, make_mapping
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    loc_b = make_location(owner_b.merchant)
    int_b = make_integration(owner_b.merchant)
    make_mapping(owner_b.merchant, int_b, loc_b)

    with tenant_context(owner_b.merchant_id), tenant_atomic():
        event_b, _ = events_services.record_event(
            integration=int_b, external_event_id="1", payload=make_payload()
        )

    with pytest.raises(IntegrationEvent.DoesNotExist):
        events_tasks.process_integration_event(owner_a.merchant_id, event_b.id)

    with tenant_context(owner_b.merchant_id), tenant_atomic():
        event_b.refresh_from_db()
    assert event_b.status == IntegrationEvent.Status.RECEIVED  # untouched


def test_calling_the_task_twice_for_the_same_event_produces_one_transaction(
    make_merchant, make_location, make_integration, make_mapping
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload()
        )

    events_tasks.process_integration_event(owner.merchant_id, event.id)
    events_tasks.process_integration_event(owner.merchant_id, event.id)

    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Transaction.objects.count() == 1


# --- record_event on_commit enqueueing -------------------------------------


@pytest.mark.django_db(transaction=True)
def test_record_event_enqueues_processing_on_commit(
    make_merchant, make_location, make_integration, make_mapping, django_capture_on_commit_callbacks
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    with django_capture_on_commit_callbacks(execute=True):
        with tenant_context(owner.merchant_id), tenant_atomic():
            event, created = events_services.record_event(
                integration=integration, external_event_id="1", payload=make_payload()
            )
    assert created is True

    with tenant_context(owner.merchant_id), tenant_atomic():
        event.refresh_from_db()
    assert event.status == IntegrationEvent.Status.PROCESSED  # ran via the enqueued task


@pytest.mark.django_db(transaction=True)
def test_a_rolled_back_record_event_enqueues_nothing(
    make_merchant, make_integration, django_capture_on_commit_callbacks
):
    owner = make_merchant("A")
    integration = make_integration(owner.merchant)

    with pytest.raises(RuntimeError):
        with django_capture_on_commit_callbacks(execute=True):
            with tenant_context(owner.merchant_id), tenant_atomic():
                with transaction.atomic():
                    events_services.record_event(
                        integration=integration, external_event_id="1", payload=make_payload()
                    )
                    raise RuntimeError("force rollback")

    with tenant_context(owner.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.count() == 0


# --- retry_failed_events: backoff, dead-letter, stale RECEIVED, CANCELLED --


@pytest.mark.django_db(transaction=True)
def test_retry_failed_events_retries_a_due_failed_event_to_processed(
    make_merchant, make_location, make_integration, make_mapping, django_capture_on_commit_callbacks
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload(customer_phone="98765")
        )
        events_services.process_event(event.id)  # -> FAILED
        event.refresh_from_db()
    assert event.status == IntegrationEvent.Status.FAILED

    with tenant_context(owner.merchant_id), tenant_atomic():
        IntegrationEvent.objects.filter(pk=event.id).update(
            updated_at=dj_timezone.now() - timedelta(minutes=6)
        )
        # Fix the underlying problem, as a merchant would before retrying.
        IntegrationEvent.objects.filter(pk=event.id).update(payload=make_payload())

    with django_capture_on_commit_callbacks(execute=True):
        events_tasks.retry_failed_events()

    with tenant_context(owner.merchant_id), tenant_atomic():
        event.refresh_from_db()
    assert event.status == IntegrationEvent.Status.PROCESSED


@pytest.mark.django_db(transaction=True)
def test_retry_failed_events_never_enqueues_cancelled_dead_letter_or_processed(
    make_merchant, make_location, make_integration, make_mapping, django_capture_on_commit_callbacks
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    with tenant_context(owner.merchant_id), tenant_atomic():
        processed, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload()
        )
        events_services.process_event(processed.id)

        cancelled, _ = events_services.record_event(
            integration=integration, external_event_id="2", payload=make_payload()
        )
        cancelled.status = IntegrationEvent.Status.CANCELLED
        cancelled.save(update_fields=["status"])

        IntegrationEvent.objects.filter(pk__in=[processed.id, cancelled.id]).update(
            updated_at=dj_timezone.now() - timedelta(days=1)
        )

    with django_capture_on_commit_callbacks(execute=True):
        events_tasks.retry_failed_events()

    with tenant_context(owner.merchant_id), tenant_atomic():
        processed.refresh_from_db()
        cancelled.refresh_from_db()
    assert processed.status == IntegrationEvent.Status.PROCESSED
    assert cancelled.status == IntegrationEvent.Status.CANCELLED


@pytest.mark.django_db(transaction=True)
def test_retry_failed_events_reenqueues_stale_received(
    make_merchant, make_location, make_integration, make_mapping, django_capture_on_commit_callbacks
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    with tenant_context(owner.merchant_id), tenant_atomic():
        event, _ = events_services.record_event(
            integration=integration, external_event_id="1", payload=make_payload()
        )
        IntegrationEvent.objects.filter(pk=event.id).update(
            received_at=dj_timezone.now() - timedelta(minutes=11)
        )

    with django_capture_on_commit_callbacks(execute=True):
        events_tasks.retry_failed_events()

    with tenant_context(owner.merchant_id), tenant_atomic():
        event.refresh_from_db()
    assert event.status == IntegrationEvent.Status.PROCESSED


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("inactive_status", ["SUSPENDED", "DELETED"])
def test_retry_failed_events_skips_non_active_merchants(
    inactive_status,
    make_merchant,
    make_location,
    make_integration,
    make_mapping,
    django_capture_on_commit_callbacks,
):
    """The sweep is limited to ACTIVE merchants. A suspended or deleted
    merchant's events are left exactly as they are -- not processed, not
    cancelled, not advanced -- so they resume normally if the merchant
    becomes ACTIVE again."""
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    make_mapping(owner.merchant, integration, location)

    # Created directly, not via record_event(): record_event registers an
    # on_commit enqueue that would process the event during setup, before the
    # merchant is even suspended.
    with tenant_context(owner.merchant_id), tenant_atomic():
        event = IntegrationEvent.objects.create(
            merchant_id=owner.merchant_id,
            integration=integration,
            source=integration.provider,
            external_event_id="1",
            payload=make_payload(),
            status=IntegrationEvent.Status.RECEIVED,
            received_at=dj_timezone.now() - timedelta(minutes=11),
        )

    Merchant.objects.filter(pk=owner.merchant_id).update(status=inactive_status)

    with django_capture_on_commit_callbacks(execute=True):
        events_tasks.retry_failed_events()

    with tenant_context(owner.merchant_id), tenant_atomic():
        event.refresh_from_db()
    assert event.status == IntegrationEvent.Status.RECEIVED  # untouched
    assert event.attempt_count == 0

    # Back to ACTIVE: the same event is swept normally, proving the filter
    # only gates who is swept, not the retry semantics themselves.
    Merchant.objects.filter(pk=owner.merchant_id).update(status=Merchant.Status.ACTIVE)
    with django_capture_on_commit_callbacks(execute=True):
        events_tasks.retry_failed_events()

    with tenant_context(owner.merchant_id), tenant_atomic():
        event.refresh_from_db()
    assert event.status == IntegrationEvent.Status.PROCESSED


@pytest.mark.django_db(transaction=True)
def test_retry_failed_events_covers_two_merchants_each_under_its_own_context(
    make_merchant, make_location, make_integration, make_mapping, django_capture_on_commit_callbacks
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    loc_a = make_location(owner_a.merchant)
    loc_b = make_location(owner_b.merchant)
    int_a = make_integration(owner_a.merchant)
    int_b = make_integration(owner_b.merchant)
    make_mapping(owner_a.merchant, int_a, loc_a)
    make_mapping(owner_b.merchant, int_b, loc_b)

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        event_a, _ = events_services.record_event(
            integration=int_a, external_event_id="1", payload=make_payload()
        )
        IntegrationEvent.objects.filter(pk=event_a.id).update(
            received_at=dj_timezone.now() - timedelta(minutes=11)
        )
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        event_b, _ = events_services.record_event(
            integration=int_b, external_event_id="1", payload=make_payload()
        )
        IntegrationEvent.objects.filter(pk=event_b.id).update(
            received_at=dj_timezone.now() - timedelta(minutes=11)
        )

    with django_capture_on_commit_callbacks(execute=True):
        events_tasks.retry_failed_events()

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        event_a.refresh_from_db()
        assert event_a.status == IntegrationEvent.Status.PROCESSED
        assert event_a.merchant_id == owner_a.merchant_id
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        event_b.refresh_from_db()
        assert event_b.status == IntegrationEvent.Status.PROCESSED
        assert event_b.merchant_id == owner_b.merchant_id
