"""Unit tests for integrations/services.py (spec Definition of done
"Integrations API and services")."""
import json
import uuid
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from auditlog.models import AuditLog
from core.crypto import decrypt
from core.tenancy import tenant_atomic, tenant_context
from events.models import IntegrationEvent
from integrations import services
from integrations.core.events import SaleCreated
from integrations.exceptions import IntegrationNotFound, LocationUnresolved, MappingExists
from integrations.models import Integration
from locations.models import Location

pytestmark = pytest.mark.django_db


def _connect(owner, provider="webhook", **kwargs):
    with tenant_context(owner.merchant_id), tenant_atomic():
        return services.connect_integration(actor=owner, provider=provider, **kwargs)


def _sale(**overrides):
    fields = dict(
        source="webhook",
        external_transaction_id="INV-1",
        amount=Decimal("10.00"),
        currency="INR",
        occurred_at=datetime(2024, 1, 1, tzinfo=dt_timezone.utc),
    )
    fields.update(overrides)
    return SaleCreated(**fields)


# --- connect_integration ------------------------------------------------


def test_connect_integration_requires_registered_provider(make_merchant):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(ValidationError):
            services.connect_integration(actor=owner, provider="webhook")


def test_connect_integration_rejects_unknown_provider(make_merchant, register_webhook_provider):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(ValidationError):
            services.connect_integration(actor=owner, provider="not-a-real-provider")


def test_connect_integration_stores_encrypted_credentials_and_audits(make_merchant, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner, credentials={"api_key": "secret-123"}, config_json={"a": 1})

    assert integration.status == Integration.Status.CONNECTED
    assert integration.credentials_encrypted is not None
    assert "secret-123" not in integration.credentials_encrypted
    assert json.loads(decrypt(integration.credentials_encrypted)) == {"api_key": "secret-123"}

    with tenant_context(owner.merchant_id), tenant_atomic():
        log = AuditLog.objects.get(action=services.AUDIT_CONNECTED, target_id=str(integration.id))
    assert log.metadata_json == {"provider": "webhook"}
    assert "secret-123" not in json.dumps(log.metadata_json)


def test_connect_integration_without_credentials_leaves_credentials_null(make_merchant, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    assert integration.credentials_encrypted is None


# --- get_integration / list_integrations ---------------------------------


def test_get_integration_raises_for_cross_merchant(make_merchant, register_webhook_provider):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_b = _connect(owner_b)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(IntegrationNotFound):
            services.get_integration(integration_b.id)


def test_get_integration_raises_for_malformed_id(make_merchant):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(IntegrationNotFound):
            services.get_integration("not-a-uuid")


def test_list_integrations_scoped_to_merchant(make_merchant, register_webhook_provider):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_a = _connect(owner_a)
    _connect(owner_b)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        ids = {i.id for i in services.list_integrations()}
    assert ids == {integration_a.id}


# --- update_integration_config -------------------------------------------


def test_update_integration_config_replaces_config(make_merchant, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner, config_json={"a": 1})
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration = services.update_integration_config(integration, config_json={"b": 2})
    assert integration.config_json == {"b": 2}


# --- disconnect_integration -----------------------------------------------


def test_disconnect_integration_is_idempotent(make_merchant, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.disconnect_integration(actor=owner, integration=integration)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.disconnect_integration(actor=owner, integration=integration)
        count = AuditLog.objects.filter(
            action=services.AUDIT_DISCONNECTED, target_id=str(integration.id)
        ).count()
    assert count == 1


def test_disconnect_integration_clears_credentials_and_deactivates_mappings(
    make_merchant, make_location, register_webhook_provider
):
    owner = make_merchant("A")
    integration = _connect(owner, credentials={"k": "v"})
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        mapping = services.add_location_mapping(integration, location_id=location.id)
        services.disconnect_integration(actor=owner, integration=integration)
        integration.refresh_from_db()
        mapping.refresh_from_db()
    assert integration.status == Integration.Status.DISCONNECTED
    assert integration.credentials_encrypted is None
    assert mapping.is_active is False


def _make_event(merchant_id, integration, external_event_id, **overrides):
    fields = dict(
        merchant_id=merchant_id,
        integration=integration,
        source="webhook",
        external_event_id=external_event_id,
        payload={},
        status=IntegrationEvent.Status.RECEIVED,
    )
    fields.update(overrides)
    return IntegrationEvent.objects.create(**fields)


def test_disconnect_integration_cancels_only_pending_events(make_merchant, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    with tenant_context(owner.merchant_id), tenant_atomic():
        received = _make_event(owner.merchant_id, integration, "1")
        failed = _make_event(
            owner.merchant_id,
            integration,
            "2",
            status=IntegrationEvent.Status.FAILED,
            error_code="INVALID_PHONE",
            error_message="x",
            attempt_count=2,
        )
        processed = _make_event(owner.merchant_id, integration, "3", status=IntegrationEvent.Status.PROCESSED)
        dead = _make_event(owner.merchant_id, integration, "4", status=IntegrationEvent.Status.DEAD_LETTER)
        already_cancelled = _make_event(
            owner.merchant_id, integration, "5", status=IntegrationEvent.Status.CANCELLED
        )

        services.disconnect_integration(actor=owner, integration=integration)

        for row in (received, failed, processed, dead, already_cancelled):
            row.refresh_from_db()

        still_exist = {row.id for row in IntegrationEvent.objects.filter(integration=integration)}

    assert received.status == IntegrationEvent.Status.CANCELLED
    assert failed.status == IntegrationEvent.Status.CANCELLED
    assert failed.error_code == "INVALID_PHONE"
    assert failed.attempt_count == 2
    assert processed.status == IntegrationEvent.Status.PROCESSED
    assert dead.status == IntegrationEvent.Status.DEAD_LETTER
    assert already_cancelled.status == IntegrationEvent.Status.CANCELLED
    for row in (received, failed, processed, dead, already_cancelled):
        assert row.id in still_exist


def test_disconnect_integration_does_not_touch_another_integrations_events(make_merchant, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    other = _connect(owner)
    with tenant_context(owner.merchant_id), tenant_atomic():
        other_event = _make_event(owner.merchant_id, other, "1")
        services.disconnect_integration(actor=owner, integration=integration)
        other_event.refresh_from_db()
    assert other_event.status == IntegrationEvent.Status.RECEIVED


def test_disconnect_integration_is_atomic_on_audit_failure(
    make_merchant, make_location, register_webhook_provider, monkeypatch
):
    owner = make_merchant("A")
    integration = _connect(owner, credentials={"k": "v"})
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        mapping = services.add_location_mapping(integration, location_id=location.id)
        event = _make_event(owner.merchant_id, integration, "1")

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(services, "record", boom)

    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(RuntimeError):
            services.disconnect_integration(actor=owner, integration=integration)

        integration.refresh_from_db()
        mapping.refresh_from_db()
        event.refresh_from_db()

    assert integration.status == Integration.Status.CONNECTED
    assert integration.credentials_encrypted is not None
    assert mapping.is_active is True
    assert event.status == IntegrationEvent.Status.RECEIVED


# --- _assert_same_merchant -------------------------------------------------


def test_assert_same_merchant_rejects_mismatched_unsaved_location(make_merchant, register_webhook_provider):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration = _connect(owner_a)
    intruder = Location(id=uuid.uuid4(), merchant_id=owner_b.merchant_id, name="Intruder")
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(ValidationError):
            services._assert_same_merchant(integration, intruder)


def test_assert_same_merchant_accepts_matching_location(make_merchant, make_location, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services._assert_same_merchant(integration, location)  # does not raise


# --- add_location_mapping --------------------------------------------------


def test_add_location_mapping_creates_row(make_merchant, make_location, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        mapping = services.add_location_mapping(integration, location_id=location.id)
    assert mapping.location_id == location.id
    assert mapping.is_active is True


def test_add_location_mapping_duplicate_raises_mapping_exists(make_merchant, make_location, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)
        with pytest.raises(MappingExists):
            services.add_location_mapping(integration, location_id=location.id)


def test_add_location_mapping_unknown_and_cross_merchant_return_same_error_body(
    make_merchant, make_location, register_webhook_provider
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration = _connect(owner_a)
    location_b = make_location(owner_b.merchant)

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(ValidationError) as cross:
            services.add_location_mapping(integration, location_id=location_b.id)
        with pytest.raises(ValidationError) as unknown:
            services.add_location_mapping(integration, location_id=uuid.uuid4())

    assert cross.value.message_dict == unknown.value.message_dict


# --- _validate_external_location_id ----------------------------------------


def test_validate_external_location_id_rejects_non_string(make_merchant, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(ValidationError):
            services._validate_external_location_id(integration, {"external_location_id": 123}, True)


def test_validate_external_location_id_rejects_duplicate_active(make_merchant, make_location, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    loc1 = make_location(owner.merchant, name="L1")
    loc2 = make_location(owner.merchant, name="L2")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=loc1.id, config_json={"external_location_id": "store-1"})
        with pytest.raises(ValidationError):
            services.add_location_mapping(integration, location_id=loc2.id, config_json={"external_location_id": "store-1"})


def test_validate_external_location_id_allows_reuse_when_prior_mapping_inactive(
    make_merchant, make_location, register_webhook_provider
):
    owner = make_merchant("A")
    integration = _connect(owner)
    loc1 = make_location(owner.merchant, name="L1")
    loc2 = make_location(owner.merchant, name="L2")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(
            integration, location_id=loc1.id, config_json={"external_location_id": "store-1"}, is_active=False
        )
        mapping2 = services.add_location_mapping(
            integration, location_id=loc2.id, config_json={"external_location_id": "store-1"}
        )
    assert mapping2.config_json["external_location_id"] == "store-1"


# --- replace_location_mappings ----------------------------------------------


def test_replace_location_mappings_upserts_and_deletes(make_merchant, make_location, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    loc1 = make_location(owner.merchant, name="L1")
    loc2 = make_location(owner.merchant, name="L2")
    loc3 = make_location(owner.merchant, name="L3")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=loc1.id)
        services.add_location_mapping(integration, location_id=loc2.id)

        services.replace_location_mappings(
            integration,
            mappings=[
                {"location_id": loc2.id, "config_json": {"external_location_id": "s2"}},
                {"location_id": loc3.id},
            ],
        )
        remaining = set(integration.location_mappings.values_list("location_id", flat=True))
    assert remaining == {loc2.id, loc3.id}


def test_replace_location_mappings_rejects_duplicate_location_id_in_request(
    make_merchant, make_location, register_webhook_provider
):
    owner = make_merchant("A")
    integration = _connect(owner)
    loc = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(ValidationError):
            services.replace_location_mappings(
                integration, mappings=[{"location_id": loc.id}, {"location_id": loc.id}]
            )


def test_replace_location_mappings_validation_failure_leaves_previous_set_unchanged(
    make_merchant, make_location, register_webhook_provider
):
    owner = make_merchant("A")
    integration = _connect(owner)
    loc1 = make_location(owner.merchant, name="L1")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=loc1.id)
        with pytest.raises(ValidationError):
            services.replace_location_mappings(integration, mappings=[{"location_id": uuid.uuid4()}])
        remaining = set(integration.location_mappings.values_list("location_id", flat=True))
    assert remaining == {loc1.id}


def test_replace_location_mappings_rejects_duplicate_external_location_id_across_items(
    make_merchant, make_location, register_webhook_provider
):
    owner = make_merchant("A")
    integration = _connect(owner)
    loc1 = make_location(owner.merchant, name="L1")
    loc2 = make_location(owner.merchant, name="L2")
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(ValidationError):
            services.replace_location_mappings(
                integration,
                mappings=[
                    {"location_id": loc1.id, "config_json": {"external_location_id": "dup"}},
                    {"location_id": loc2.id, "config_json": {"external_location_id": "dup"}},
                ],
            )


# --- resolve_location --------------------------------------------------


def test_resolve_location_single_mapping_no_external_id(make_merchant, make_location, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)
        resolved = services.resolve_location(integration, _sale())
    assert resolved.id == location.id


def test_resolve_location_multi_location_matches_external_id(make_merchant, make_location, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    loc1 = make_location(owner.merchant, name="L1")
    loc2 = make_location(owner.merchant, name="L2")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=loc1.id, config_json={"external_location_id": "s1"})
        services.add_location_mapping(integration, location_id=loc2.id, config_json={"external_location_id": "s2"})
        resolved = services.resolve_location(integration, _sale(external_location_id="s2"))
    assert resolved.id == loc2.id


def test_resolve_location_unknown_external_id_raises_unresolved(make_merchant, make_location, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    loc1 = make_location(owner.merchant, name="L1")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=loc1.id, config_json={"external_location_id": "s1"})
        with pytest.raises(LocationUnresolved):
            services.resolve_location(integration, _sale(external_location_id="unknown"))


def test_resolve_location_missing_external_id_with_multiple_mappings_raises_unresolved(
    make_merchant, make_location, register_webhook_provider
):
    owner = make_merchant("A")
    integration = _connect(owner)
    loc1 = make_location(owner.merchant, name="L1")
    loc2 = make_location(owner.merchant, name="L2")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=loc1.id, config_json={"external_location_id": "s1"})
        services.add_location_mapping(integration, location_id=loc2.id, config_json={"external_location_id": "s2"})
        with pytest.raises(LocationUnresolved):
            services.resolve_location(integration, _sale())


def test_resolve_location_zero_active_mappings_raises_unresolved(make_merchant, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(LocationUnresolved):
            services.resolve_location(integration, _sale())


def test_resolve_location_ignores_inactive_mappings(make_merchant, make_location, register_webhook_provider):
    owner = make_merchant("A")
    integration = _connect(owner)
    loc1 = make_location(owner.merchant, name="L1")
    loc2 = make_location(owner.merchant, name="L2")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=loc1.id, is_active=False)
        services.add_location_mapping(integration, location_id=loc2.id)
        resolved = services.resolve_location(integration, _sale())
    assert resolved.id == loc2.id
