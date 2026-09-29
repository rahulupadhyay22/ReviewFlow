"""Integration services (Coding-Standards.md §1: business logic lives here,
never in views/serializers). Every function requires a tenant context."""
import csv
import io
import json
import logging
import uuid
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone as dj_timezone

from accounts.models import TeamMember
from auditlog.services import record
from core import storage
from core.crypto import encrypt
from core.exceptions import TenantContextError
from core.tenancy import get_current_merchant_id, integration_lookup_atomic, tenant_atomic, tenant_context
from integrations.core.events import SaleCreated
from integrations.core.registry import get_adapter, get_adapter_class, is_registered
from integrations.core.schemas import PayloadValidationError
from integrations.exceptions import (
    IntegrationDisconnected,
    IntegrationExists,
    IntegrationNotConnected,
    IntegrationNotFound,
    InvalidWebhookPayload,
    LocationUnresolved,
    MappingExists,
    PayloadRejected,
    ShopifyConnectNotSupported,
    WebhookRejected,
)
from integrations.models import Integration, IntegrationLocationMapping
from transactions.models import Transaction

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Import-time cycle (integrations <-> locations) is avoided by the local
    # imports inside the functions below; this is the annotation-only path,
    # the same pattern as accounts/services.py.
    from locations.models import Location

AUDIT_CONNECTED = "integration.connected"
AUDIT_DISCONNECTED = "integration.disconnected"


def assert_generic_connect_allowed(provider: str) -> None:
    """The generic credentials-based connect path can never create a
    Shopify Integration (spec 06-shopify-app O2, user decision
    2026-09-29): the OAuth install/link flow is the only way. Raises
    before ANY credential/config is read, validated, issued, encrypted or
    inserted -- called first by both connect_integration() (defense in
    depth for any other caller) and IntegrationConnectView.post() (before
    the request body is even deserialized, so a malformed body for
    provider=shopify still gets this 400, never the serializer's 422)."""
    if provider == Integration.Provider.SHOPIFY:
        raise ShopifyConnectNotSupported()


def connect_integration(
    *,
    actor: TeamMember,
    provider: str,
    credentials: dict | None = None,
    config_json: dict | None = None,
) -> tuple[Integration, dict | None]:
    """Generic credential-based connect (spec Decision 11; provider hooks
    per spec 06 Decision 6). The provider must be a Data-Dictionary.md enum
    value AND have a registered adapter, so an integration whose events can
    never be processed cannot be connected.

    Returns (integration, issued_credentials): issued_credentials is the
    plaintext of any server-generated credential (e.g. the Generic Webhook
    signing secret), returned to the caller exactly once, or None."""
    assert_generic_connect_allowed(provider)
    if provider not in Integration.Provider.values or not is_registered(provider):
        raise ValidationError({"provider": ["This provider is not available."]})

    adapter_cls = get_adapter_class(provider)
    adapter_cls.validate_connection(credentials, config_json)
    issued_credentials = adapter_cls.issue_credentials()
    stored_credentials = issued_credentials if issued_credentials is not None else credentials

    with tenant_atomic():
        try:
            with transaction.atomic():
                integration = Integration.objects.create(
                    merchant_id=get_current_merchant_id(),
                    provider=provider,
                    status=Integration.Status.CONNECTED,
                    credentials_encrypted=(
                        encrypt(json.dumps(stored_credentials)) if stored_credentials else None
                    ),
                    config_json=config_json,
                )
        except IntegrityError:
            # Only the "api" provider has a uniqueness constraint (spec 06
            # Decision 4: at most one CONNECTED api integration per
            # merchant) -- this is not check-then-insert.
            raise IntegrationExists() from None

        # Credentials are never included in audit metadata.
        record(AUDIT_CONNECTED, actor=actor.user, target=integration, metadata={"provider": provider})
        return integration, issued_credentials


def get_integration(integration_id: uuid.UUID | str) -> Integration:
    try:
        return Integration.objects.get(pk=integration_id)
    except (Integration.DoesNotExist, ValueError, TypeError, ValidationError):
        raise IntegrationNotFound() from None


def list_integrations() -> QuerySet[Integration]:
    return Integration.objects.prefetch_related("location_mappings").order_by("created_at")


def update_integration_config(integration: Integration, *, config_json: dict | None) -> Integration:
    adapter_cls = get_adapter_class(integration.provider)
    adapter_cls.validate_connection(None, config_json)
    integration.config_json = config_json
    integration.save(update_fields=["config_json", "updated_at"])
    return integration


def disconnect_integration(
    *, actor: TeamMember | None, integration: Integration, reason: str | None = None
) -> None:
    """Idempotent. Locks the Integration row (FOR NO KEY UPDATE, spec
    Decision 20), disconnects it, deactivates its mappings, clears
    credentials, and cancels its pending RECEIVED/FAILED events, all in one
    transaction.

    actor=None means a SYSTEM disconnect (spec 06-shopify-app Decision 3
    uninstall): the audit row has no actor user. reason, when given, is
    recorded in the audit metadata alongside the provider -- e.g.
    reason="app_uninstalled" for a verified Shopify app/uninstalled
    delivery."""
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

        metadata = {"provider": integration.provider}
        if reason is not None:
            metadata["reason"] = reason
        record(
            AUDIT_DISCONNECTED,
            actor=actor.user if actor is not None else None,
            target=integration,
            metadata=metadata,
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


def receive_webhook(*, provider: str, integration_id: str, request) -> None:
    """The generic provider-webhook receiver core (spec 06 Decisions 1, 11,
    15; spec 06-shopify-app R2/O1 add the uninstall step). Must be called
    with NO active tenant context.

    Normative order:
      lookup -> provider check -> verify() -> [Shopify only] uninstall
      path (webhook-id check, JSON decode, current-topic discriminator,
      DISCONNECTED no-op or system disconnect) -> DISCONNECTED rejection
      (every other topic) -> sale-topic filter -> JSON decode -> event id
      -> tenant_context -> record_event.

    Nothing is stored before verification, and every rejection before
    verification is the identical WebhookRejected (401) -- an unknown id, a
    malformed id, a wrong-provider id and a bad/missing signature are all
    indistinguishable to the caller. The Shopify uninstall path runs
    whatever the Integration's status: the DISCONNECTED no-op (a duplicate
    delivery) is reached only AFTER the webhook-id check and the
    current-topic discriminator both pass -- it can never be used to
    bypass them.
    """
    try:
        # integration_lookup_atomic() normalizes to a uuid.UUID and stores
        # that in the contextvar; for_lookup_id() compares against it, so
        # it must be given that SAME normalized value -- not the raw
        # (possibly str) integration_id from the URL, or the comparison
        # would always fail.
        with integration_lookup_atomic(integration_id) as normalized_id:
            integration = Integration.objects.for_lookup_id(normalized_id).first()
    except (ValueError, TenantContextError):
        # ValueError: a malformed (non-UUID) id. TenantContextError: called
        # from inside an active merchant context (e.g. a webhook request
        # that also carries a dashboard session cookie) -- see
        # accounts.middleware.SessionMerchantMiddleware, which keeps
        # provider webhook receivers pre-tenant so this never actually
        # fires in production; it is still handled here, fail closed.
        raise WebhookRejected() from None

    if integration is None or integration.provider != provider:
        raise WebhookRejected()

    adapter = get_adapter(integration)
    if not adapter.verify(request):
        # Never log the payload, headers or signature -- only the
        # integration id (Security-Controls.md §Logging Rules).
        logger.warning("Webhook verification failed for integration %s", integration.id)
        raise WebhookRejected()

    if adapter.is_uninstall_event(request):
        # [User decision 2026-09-29, O1] X-Shopify-Webhook-Id is required
        # on app/uninstalled exactly as on orders/paid, checked FIRST --
        # before the current-topic discriminator, the status read and any
        # disconnect -- and whatever the Integration's status. This is
        # what stops the DISCONNECTED no-op below from ever being reached
        # without it. ShopifyAdapter.get_external_event_id() raises
        # WebhookRejected (not PayloadValidationError) for a missing
        # header, so it propagates as the identical 401 unmodified.
        adapter.get_external_event_id(request, {})

        try:
            payload = json.loads(request.body)
        except (ValueError, TypeError):
            raise WebhookRejected() from None
        if not isinstance(payload, dict) or not adapter.uninstall_payload_matches(payload):
            # A malformed body, or one that fails the Phase 06
            # current-topic discriminator, is rejected exactly like a bad
            # signature: it is NOT a general proof of an app/uninstalled
            # payload, and it does not authenticate X-Shopify-Topic --
            # see ShopifyAdapter.uninstall_payload_matches. Nothing is
            # read or changed.
            raise WebhookRejected()

        if integration.status == Integration.Status.DISCONNECTED:
            return None  # idempotent duplicate: no state change, no audit row

        with tenant_context(integration.merchant_id):
            disconnect_integration(actor=None, integration=integration, reason="app_uninstalled")
        return None

    if integration.status == Integration.Status.DISCONNECTED:
        raise WebhookRejected()

    if not adapter.is_sale_event(request):
        return None

    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        raise InvalidWebhookPayload() from None
    if not isinstance(payload, dict):
        raise InvalidWebhookPayload()

    try:
        external_event_id = adapter.get_external_event_id(request, payload)
    except PayloadValidationError as exc:
        raise PayloadRejected(exc.code) from None

    with tenant_context(integration.merchant_id):
        # Local import: events.services imports integrations.services
        # (resolve_location), so a top-level import here would cycle.
        from events.services import record_event

        try:
            record_event(integration=integration, external_event_id=external_event_id, payload=payload)
        except IntegrationDisconnected:
            # A disconnect that raced this request between the status check
            # above and here.
            raise WebhookRejected() from None


def start_csv_import(*, integration: Integration, file) -> int:
    """Synchronous whole-file validation, then staging in R2 and a robust
    on-commit enqueue of the row-by-row import task (spec 06 Decision 10).
    Any invalid row rejects the whole file with 422 -- nothing is uploaded
    or stored."""
    if integration.provider != Integration.Provider.CSV or integration.status != Integration.Status.CONNECTED:
        raise ValidationError({"integration": ["This integration cannot accept a CSV import."]})
    if file is None:
        raise ValidationError({"file": ["A file is required."]})

    raw = file.read()
    if not raw:
        raise ValidationError({"file": ["The file is empty."]})
    if len(raw) > settings.CSV_IMPORT_MAX_BYTES:
        raise ValidationError({"file": ["The file is too large."]})
    try:
        text = raw.decode("utf-8-sig")  # utf-8-sig also accepts a BOM
    except UnicodeDecodeError:
        raise ValidationError({"file": ["The file must be UTF-8 encoded."]}) from None

    adapter_cls = get_adapter_class(integration.provider)

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    missing_columns = [c for c in adapter_cls.REQUIRED_COLUMNS if c not in fieldnames]
    if missing_columns:
        raise ValidationError({"file": [f"Missing required column(s): {', '.join(missing_columns)}."]})

    rows = list(reader)
    if not rows:
        raise ValidationError({"file": ["The file has no data rows."]})
    if len(rows) > settings.CSV_IMPORT_MAX_ROWS:
        raise ValidationError({"file": ["The file has too many rows."]})

    adapter = get_adapter(integration)
    field_errors: dict[str, list[str]] = {}
    for idx, row in enumerate(rows, start=1):
        try:
            adapter.normalize(adapter.parse(row))
        except PayloadValidationError as exc:
            if len(field_errors) < 50:
                field_errors[f"row_{idx}"] = [exc.code]
    if field_errors:
        raise ValidationError(field_errors)

    # Server-built key only -- never derived from client input.
    key = f"csv-imports/{integration.merchant_id}/{uuid.uuid4()}.csv"
    storage.put_object(key, raw)

    merchant_id = get_current_merchant_id()
    integration_id = integration.id
    transaction.on_commit(_enqueue_csv_import(merchant_id, integration_id, key))

    return len(rows)


def _enqueue_csv_import(merchant_id: uuid.UUID, integration_id: uuid.UUID, object_key: str):
    def _run():
        # Local import breaks the integrations.services <-> integrations.tasks cycle.
        from integrations.tasks import import_csv

        try:
            import_csv.delay(merchant_id, integration_id, object_key)
        except Exception as exc:
            # The object is already staged; a failed enqueue here leaves it
            # in R2 for manual recovery rather than raising after the
            # upload already succeeded (mirrors events.services robust
            # enqueue, spec 06 Decision 9). Never logs the exception
            # message.
            logger.warning(
                "CSV import enqueue failed for integration %s object %s with %s",
                integration_id,
                object_key,
                type(exc).__name__,
            )

    return _run


def import_csv_rows(*, integration_id: uuid.UUID | str, object_key: str) -> None:
    """One row per IntegrationEvent, same idempotency as a single webhook
    event (spec 06 Decision 10). Deletes the staged object only after every
    row is recorded -- a mid-run failure leaves it in place for a safe
    re-run. Requires an active tenant context (the task sets it via
    @core.tenancy.tenant_task)."""
    from botocore.exceptions import ClientError

    try:
        raw = storage.get_object(object_key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404"):
            return  # Already purged by an earlier successful run -- no-op.
        logger.warning("CSV import object %s unreadable with %s", object_key, type(exc).__name__)
        return

    from events.services import record_event

    # RLS is FORCE-enabled: the tenant contextvar alone (set by @tenant_task)
    # is not enough for the read to see the row -- SET LOCAL must actually
    # run inside a real transaction, exactly like every other tenant read.
    with tenant_atomic():
        integration = Integration.objects.get(pk=integration_id)
    adapter = get_adapter(integration)
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    # ponytail: one record_event() call (its own transaction) per row --
    # correct and simple, but O(rows) round trips for a large file. Upgrade
    # path: a single bulk_create with ignore_conflicts if throughput ever
    # matters.
    for row in reader:
        external_event_id = adapter.get_external_event_id(None, row)
        try:
            record_event(integration=integration, external_event_id=external_event_id, payload=row)
        except IntegrationDisconnected:
            # disconnect_integration() already cancelled the pending events
            # it created; nothing further to record for this run.
            storage.delete_object(object_key)
            return

    storage.delete_object(object_key)


def ingest_api_sale(*, payload: dict) -> tuple:
    """POST /sales core (spec 06 Decisions 8, 13, 14). Runs inside the
    request-wide tenant_atomic() the API key's tenant context already
    opened (core.middleware.TenantMiddleware).

    Pre-validates through get_adapter(integration) -- the SAME
    parse()/normalize()/resolve_location() functions events.services.
    process_event() uses on the stored payload. There is no second,
    duplicate normalization path; process_event() remains the only code
    that ever creates a Transaction.

    Returns (event, transaction_or_None, created). The view maps this to
    201/200/202/409 purely from `created` and `event.status`
    (spec Decision 8's response table); it holds no other status logic."""
    from events.models import IntegrationEvent

    if not isinstance(payload, dict):
        raise ValidationError({"body": ["Must be a JSON object."]})

    try:
        integration = Integration.objects.get(
            provider=Integration.Provider.API, status=Integration.Status.CONNECTED
        )
    except Integration.DoesNotExist:
        raise IntegrationNotConnected() from None

    adapter = get_adapter(integration)
    try:
        sale = adapter.normalize(adapter.parse(payload))
        resolve_location(integration, sale)  # validates only; process_event() resolves again
        external_event_id = adapter.get_external_event_id(None, payload)
    except PayloadValidationError as exc:
        raise PayloadRejected(exc.code) from None

    # Local import: events.services imports integrations.services
    # (resolve_location), so a top-level import here would cycle.
    from events.services import process_event, record_event

    event, created = record_event(integration=integration, external_event_id=external_event_id, payload=payload)
    if created:
        event = process_event(event.id)

    transaction_obj = None
    if event.status == IntegrationEvent.Status.PROCESSED:
        transaction_obj = Transaction.objects.filter(
            location=event.location, external_transaction_id=sale.external_transaction_id
        ).first()

    return event, transaction_obj, created
