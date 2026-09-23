"""Integration services (Coding-Standards.md §1: business logic lives here,
never in views/serializers). Every function requires a tenant context."""
import json
import uuid
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone as dj_timezone

from accounts.models import TeamMember
from auditlog.services import record
from core.crypto import encrypt
from core.tenancy import get_current_merchant_id, tenant_atomic
from integrations.core.events import SaleCreated
from integrations.core.registry import is_registered
from integrations.exceptions import IntegrationNotFound, LocationUnresolved, MappingExists
from integrations.models import Integration, IntegrationLocationMapping

if TYPE_CHECKING:
    # Import-time cycle (integrations <-> locations) is avoided by the local
    # imports inside the functions below; this is the annotation-only path,
    # the same pattern as accounts/services.py.
    from locations.models import Location

AUDIT_CONNECTED = "integration.connected"
AUDIT_DISCONNECTED = "integration.disconnected"


def connect_integration(
    *,
    actor: TeamMember,
    provider: str,
    credentials: dict | None = None,
    config_json: dict | None = None,
) -> Integration:
    """Generic credential-based connect (spec Decision 11). There is no
    provider handshake in Phase 04 -- Phase 06 adds it. The provider must be
    a Data-Dictionary.md enum value AND have a registered adapter, so an
    integration whose events can never be processed cannot be connected."""
    if provider not in Integration.Provider.values or not is_registered(provider):
        raise ValidationError({"provider": ["This provider is not available."]})

    with tenant_atomic():
        integration = Integration.objects.create(
            merchant_id=get_current_merchant_id(),
            provider=provider,
            status=Integration.Status.CONNECTED,
            credentials_encrypted=encrypt(json.dumps(credentials)) if credentials else None,
            config_json=config_json,
        )
        # Credentials are never included in audit metadata.
        record(AUDIT_CONNECTED, actor=actor.user, target=integration, metadata={"provider": provider})
        return integration


def get_integration(integration_id: uuid.UUID | str) -> Integration:
    try:
        return Integration.objects.get(pk=integration_id)
    except (Integration.DoesNotExist, ValueError, TypeError, ValidationError):
        raise IntegrationNotFound() from None


def list_integrations() -> QuerySet[Integration]:
    return Integration.objects.prefetch_related("location_mappings").order_by("created_at")


def update_integration_config(integration: Integration, *, config_json: dict | None) -> Integration:
    integration.config_json = config_json
    integration.save(update_fields=["config_json", "updated_at"])
    return integration


def disconnect_integration(*, actor: TeamMember, integration: Integration) -> None:
    """Idempotent. Locks the Integration row (FOR NO KEY UPDATE, spec
    Decision 20), disconnects it, deactivates its mappings, clears
    credentials, and cancels its pending RECEIVED/FAILED events, all in one
    transaction."""
    from events.models import IntegrationEvent  # local import: events has no reverse dependency

    with tenant_atomic():
        integration = Integration.objects.select_for_update(no_key=True).get(pk=integration.pk)
        if integration.status == Integration.Status.DISCONNECTED:
            return  # idempotent: no second audit row, nothing re-cancelled

        integration.status = Integration.Status.DISCONNECTED
        integration.credentials_encrypted = None
        integration.save(update_fields=["status", "credentials_encrypted", "updated_at"])

        integration.location_mappings.update(is_active=False)

        # PROCESSED, DEAD_LETTER and already-CANCELLED are excluded by this
        # WHERE -- never touched, never deleted (spec Decision 18).
        IntegrationEvent.objects.filter(
            integration=integration,
            status__in=[IntegrationEvent.Status.RECEIVED, IntegrationEvent.Status.FAILED],
        ).update(status=IntegrationEvent.Status.CANCELLED, updated_at=dj_timezone.now())

        record(
            AUDIT_DISCONNECTED,
            actor=actor.user,
            target=integration,
            metadata={"provider": integration.provider},
        )


def _assert_same_merchant(
    integration: Integration, location: "Location", *, field: str = "location_id"
) -> None:
    """The documented three-way invariant, checked explicitly (same pattern
    as accounts.services._assert_same_merchant): RLS and the scoped manager
    already filter cross-merchant rows in the normal path, but this must not
    rely on that having happened -- it runs before every insert, and has its
    own unit test using a mismatched, unsaved Location."""
    merchant_id = get_current_merchant_id()
    bad = ValidationError({field: ["One or more locations were not found."]})
    if integration.merchant_id != merchant_id:
        raise bad
    if location.merchant_id != merchant_id:
        raise bad


def _validate_external_location_id(
    integration: Integration,
    config_json: dict | None,
    is_active: bool,
    *,
    exclude_location_id: uuid.UUID | str | None = None,
) -> None:
    if not is_active or not config_json:
        return
    ext_id = config_json.get("external_location_id")
    if ext_id is None:
        return
    if not isinstance(ext_id, str):
        raise ValidationError({"config_json": ["external_location_id must be a string."]})
    qs = integration.location_mappings.filter(is_active=True, config_json__external_location_id=ext_id)
    if exclude_location_id is not None:
        qs = qs.exclude(location_id=exclude_location_id)
    if qs.exists():
        raise ValidationError(
            {"config_json": ["external_location_id must be unique among the active mappings."]}
        )


def add_location_mapping(
    integration: Integration,
    *,
    location_id: uuid.UUID | str,
    config_json: dict | None = None,
    is_active: bool = True,
) -> IntegrationLocationMapping:
    from locations.models import Location  # local import: avoids an integrations<->locations cycle

    with tenant_atomic():
        # Lock first (Decision 20): serializes against a concurrent replace.
        integration = Integration.objects.select_for_update(no_key=True).get(pk=integration.pk)
        try:
            location = Location.objects.get(pk=location_id)
        except (Location.DoesNotExist, ValueError, TypeError, ValidationError):
            raise ValidationError({"location_id": ["One or more locations were not found."]}) from None
        _assert_same_merchant(integration, location)
        _validate_external_location_id(integration, config_json, is_active)
        try:
            with transaction.atomic():
                return IntegrationLocationMapping.objects.create(
                    merchant_id=integration.merchant_id,
                    integration=integration,
                    location=location,
                    config_json=config_json,
                    is_active=is_active,
                )
        except IntegrityError:
            raise MappingExists() from None


def replace_location_mappings(integration: Integration, *, mappings: list[dict]) -> Integration:
    """Atomically replaces the integration's mapping set. Validates the
    complete requested set before any write, so a failure anywhere leaves
    the previous set unchanged (spec Decision 20)."""
    from locations.models import Location  # local import: avoids an integrations<->locations cycle

    with tenant_atomic():
        integration = Integration.objects.select_for_update(no_key=True).get(pk=integration.pk)

        location_ids = [m["location_id"] for m in mappings]
        if len(location_ids) != len(set(location_ids)):
            raise ValidationError({"mappings": ["Duplicate location_id in the request."]})

        locations_by_id = {loc.pk: loc for loc in Location.objects.filter(pk__in=location_ids)}
        if len(locations_by_id) != len(location_ids):
            raise ValidationError({"mappings": ["One or more locations were not found."]})

        for loc in locations_by_id.values():
            _assert_same_merchant(integration, loc, field="mappings")

        # Validate the resulting active set's external_location_id
        # uniqueness as a whole, not one item against the others in isolation.
        seen_ext_ids = set()
        for m in mappings:
            if not m.get("is_active", True):
                continue
            cfg = m.get("config_json") or {}
            ext_id = cfg.get("external_location_id")
            if ext_id is None:
                continue
            if not isinstance(ext_id, str):
                raise ValidationError({"mappings": ["external_location_id must be a string."]})
            if ext_id in seen_ext_ids:
                raise ValidationError(
                    {"mappings": ["external_location_id must be unique among active mappings."]}
                )
            seen_ext_ids.add(ext_id)

        existing = {m.location_id: m for m in integration.location_mappings.all()}
        wanted_ids = set(location_ids)

        for m in mappings:
            loc_id = m["location_id"]
            config_json = m.get("config_json")
            is_active = m.get("is_active", True)
            if loc_id in existing:
                row = existing[loc_id]
                row.config_json = config_json
                row.is_active = is_active
                row.save(update_fields=["config_json", "is_active", "updated_at"])
            else:
                IntegrationLocationMapping.objects.create(
                    merchant_id=integration.merchant_id,
                    integration=integration,
                    location_id=loc_id,
                    config_json=config_json,
                    is_active=is_active,
                )

        integration.location_mappings.exclude(location_id__in=wanted_ids).delete()
        return integration


def resolve_location(integration: Integration, sale: SaleCreated) -> "Location":
    """Location resolution (spec Decision 5), using only the integration's
    active mappings. Never guesses and never falls back to the first
    mapping."""
    active = list(integration.location_mappings.filter(is_active=True).select_related("location"))

    if len(active) == 1 and not (active[0].config_json or {}).get("external_location_id"):
        return active[0].location

    if sale.external_location_id:
        matches = [
            m
            for m in active
            if (m.config_json or {}).get("external_location_id") == sale.external_location_id
        ]
        if len(matches) == 1:
            return matches[0].location

    raise LocationUnresolved("Could not resolve a ReviewFlow location for this sale.")
