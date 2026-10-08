"""WhatsApp services (spec 08; Coding-Standards.md §1). Views, tasks and the
management command call these; none contains business logic of its own.

No message is sent by anything here: WhatsAppMessage, send_template and the
status persistence are Phase 11.
"""
import hmac
import json
import logging
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from auditlog.services import record, record_platform
from core.exceptions import TenantContextError
from core.tenancy import get_current_merchant_id, platform_write_atomic, tenant_atomic
from integrations.core.schemas import PayloadValidationError, validate_e164
from whatsapp.exceptions import (
    SenderNotFound,
    TemplateConflict,
    WebhookSignatureInvalid,
    WebhookVerificationFailed,
    WhatsAppNotConfigured,
)
from whatsapp.models import MessageTemplate, WhatsAppAccount, WhatsAppLocationMapping
from whatsapp.providers import get_provider, get_provider_by_name
from whatsapp.providers.base import ProviderPermanentError, ProviderTransientError

logger = logging.getLogger(__name__)

STANDARD_VARIABLES = frozenset({"customer_name", "business_name", "location_name", "review_link"})
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


# --- shared sender -----------------------------------------------------------


def upsert_shared_pool_account(
    *, phone_number_id: str, business_account_id: str, status: str, actor_user=None
) -> WhatsAppAccount:
    """Create or update the single SHARED_POOL row (spec 08 Change 1).

    Management/platform infrastructure only: called by configure_shared_pool
    and seed_dev, never from a request, task or tenant service. Idempotent:
    unchanged values write nothing and no audit row. Raises TenantContextError
    inside a merchant context (platform_write_atomic refuses it)."""
    if not phone_number_id or not business_account_id:
        raise ValidationError("phone_number_id and business_account_id are required.")
    if status not in WhatsAppAccount.Status.values:
        raise ValidationError("Unknown status.")
    with platform_write_atomic("whatsapp_shared_pool"):
        account = WhatsAppAccount.objects.shared_pool().select_for_update().first()
        created = False
        if account is None:
            # On an empty table the lock above holds nothing, so two first runs
            # can race; the single-shared-pool constraint decides, and the loser
            # re-reads the winner's row and continues as an update.
            try:
                with transaction.atomic():
                    account = WhatsAppAccount._base_manager.create(
                        merchant=None,
                        sender_type=WhatsAppAccount.SenderType.SHARED_POOL,
                        provider=WhatsAppAccount.Provider.META_CLOUD,
                        phone_number_id=phone_number_id,
                        business_account_id=business_account_id,
                        status=status,
                        connected_at=timezone.now() if status == WhatsAppAccount.Status.ACTIVE else None,
                    )
                    created = True
            except IntegrityError as conflict:
                try:
                    account = WhatsAppAccount.objects.shared_pool().select_for_update().get()
                except WhatsAppAccount.DoesNotExist:
                    # Not the single-shared-pool race: another constraint (e.g.
                    # an OWN_NUMBER row already uses this phone_number_id).
                    # Surface the real error, not a misleading DoesNotExist.
                    raise conflict from None
        if not created:
            if (account.phone_number_id, account.business_account_id, account.status) == (
                phone_number_id,
                business_account_id,
                status,
            ):
                return account
            account.phone_number_id = phone_number_id
            account.business_account_id = business_account_id
            if status == WhatsAppAccount.Status.ACTIVE and account.connected_at is None:
                account.connected_at = timezone.now()
            account.status = status
            account.save()
        # Metadata is the status only: never a provider id or phone number.
        record_platform(
            "whatsapp.shared_pool_configured", actor=actor_user, target=account, metadata={"status": status}
        )
        return account


def _active_shared_account() -> WhatsAppAccount | None:
    return WhatsAppAccount.objects.shared_pool().filter(status=WhatsAppAccount.Status.ACTIVE).first()


# --- location sender mapping --------------------------------------------------


def list_senders():
    """Accounts the current merchant may map to: its OWN_NUMBER rows plus the
    shared row when ACTIVE."""
    return WhatsAppAccount.objects.filter(
        Q(sender_type=WhatsAppAccount.SenderType.OWN_NUMBER)
        | Q(sender_type=WhatsAppAccount.SenderType.SHARED_POOL, status=WhatsAppAccount.Status.ACTIVE)
    ).order_by("created_at", "id")


def list_templates(status: str | None = None):
    """The current merchant's templates, optionally filtered by status (an
    unknown status is ignored, as the endpoint documents no error for it)."""
    templates = MessageTemplate.objects.order_by("created_at", "id")
    if status in MessageTemplate.Status.values:
        templates = templates.filter(status=status)
    return templates


def get_sender(account_id) -> WhatsAppAccount:
    """The account to map to, by explicit id. SenderNotFound for an unknown id
    or one the merchant can't see (RLS and the manager already hide other
    merchants' accounts). A non-ACTIVE account the merchant can see (its own,
    or the shared one) is returned so set_location_sender refuses it with 422;
    list_senders still lists only selectable ones."""
    try:
        return WhatsAppAccount.objects.get(pk=account_id)
    except (WhatsAppAccount.DoesNotExist, ValidationError, ValueError):
        raise SenderNotFound() from None


def get_location_sender(location) -> WhatsAppAccount | None:
    """The account this location sends from, or None if unmapped or the
    account is not ACTIVE (WhatsApp-Architecture.md §Why the Sender Belongs
    to the Merchant). Phase 10 activation and Phase 11 dispatch call this."""
    mapping = (
        WhatsAppLocationMapping.objects.select_related("whatsapp_account").filter(location=location).first()
    )
    if mapping is None or mapping.whatsapp_account.status != WhatsAppAccount.Status.ACTIVE:
        return None
    return mapping.whatsapp_account


def _locked_mapping(location):
    return (
        WhatsAppLocationMapping.objects.select_for_update(of=("self",))
        .select_related("whatsapp_account")
        .filter(location=location)
        .first()
    )


def set_location_sender(*, location, account, actor) -> WhatsAppLocationMapping:
    """Map or repoint a location. Idempotent when unchanged. The invariant RLS
    cannot prove (the account is shared, or belongs to the location's
    merchant) is enforced here, like TeamMemberLocation."""
    if account.status != WhatsAppAccount.Status.ACTIVE:
        raise ValidationError({"whatsapp_account_id": ["This sender is not active."]})
    if (
        account.sender_type == WhatsAppAccount.SenderType.OWN_NUMBER
        and account.merchant_id != location.merchant_id
    ):
        raise ValidationError({"whatsapp_account_id": ["This sender cannot serve this location."]})
    with tenant_atomic():
        mapping = _locked_mapping(location)
        created = False
        if mapping is None:
            try:
                with transaction.atomic():
                    mapping = WhatsAppLocationMapping.objects.create(
                        whatsapp_account=account, location=location
                    )
                    created = True
            except IntegrityError:  # UNIQUE(location): a concurrent request mapped it first
                mapping = _locked_mapping(location)
        from_type = None if created else mapping.whatsapp_account.sender_type
        changed = created or mapping.whatsapp_account_id != account.id
        if changed and not created:
            mapping.whatsapp_account = account
            mapping.save(update_fields=["whatsapp_account", "updated_at"])
        if changed:
            record(
                "whatsapp.sender_changed",
                actor=actor,
                target=location,
                metadata={"from_sender_type": from_type, "to_sender_type": account.sender_type},
            )
        return mapping


def clear_location_sender(*, location, actor) -> None:
    """Idempotent; audited only when a mapping existed."""
    with tenant_atomic():
        mapping = _locked_mapping(location)
        if mapping is None:
            return
        from_type = mapping.whatsapp_account.sender_type
        mapping.delete()
        record(
            "whatsapp.sender_changed",
            actor=actor,
            target=location,
            metadata={"from_sender_type": from_type, "to_sender_type": None},
        )


# --- templates ------------------------------------------------------------------


def validate_template(*, name: str, language: str, body: str) -> None:
    errors: dict[str, list[str]] = {}
    if not (name or "").strip() or len(name) > 255:
        errors["name"] = ["Required, at most 255 characters."]
    if language not in settings.WHATSAPP_TEMPLATE_LANGUAGES:
        errors["language"] = ["Unsupported language."]
    body = body or ""
    body_errors = []
    if not body.strip():
        body_errors.append("Required.")
    elif len(body) > settings.WHATSAPP_TEMPLATE_BODY_MAX:
        body_errors.append(f"At most {settings.WHATSAPP_TEMPLATE_BODY_MAX} characters.")
    else:
        used = set(_PLACEHOLDER.findall(body))
        unknown = used - STANDARD_VARIABLES
        if unknown:
            body_errors.append("Unknown placeholder: only " + ", ".join(sorted(STANDARD_VARIABLES)) + " are allowed.")
        if "{{" in _PLACEHOLDER.sub("", body) or "}}" in _PLACEHOLDER.sub("", body):
            body_errors.append("Malformed placeholder.")
        # Shared-pool sends must name the business (Business-Rules §9).
        if "business_name" not in used:
            body_errors.append("Must include {{business_name}}.")
    if body_errors:
        errors["body"] = body_errors
    if errors:
        raise ValidationError(errors)


def _enqueue_submit(merchant_id, template_id):
    def _run():
        from whatsapp import tasks  # lazy: tasks import this module

        try:
            tasks.submit_template.delay(merchant_id, template_id)
        except Exception as exc:  # a broker error must not fail the request
            logger.warning(
                "Template submit enqueue for merchant %s failed with %s", merchant_id, type(exc).__name__
            )

    return _run


def create_template(*, name: str, language: str, body: str) -> MessageTemplate:
    """PENDING row now; submission to Meta runs after commit."""
    validate_template(name=name, language=language, body=body)
    if not settings.META_SHARED_POOL_ACCESS_TOKEN or _active_shared_account() is None:
        raise WhatsAppNotConfigured()
    merchant_id = get_current_merchant_id()
    with tenant_atomic():
        try:
            with transaction.atomic():
                template = MessageTemplate.objects.create(
                    merchant_id=merchant_id, name=name.strip(), language=language, body=body
                )
        except IntegrityError:  # UNIQUE(merchant, name, language)
            raise TemplateConflict() from None
        transaction.on_commit(_enqueue_submit(merchant_id, template.id))
    return template


def _unsubmitted(template_id):
    return MessageTemplate.objects.filter(
        pk=template_id, status=MessageTemplate.Status.PENDING, provider_template_id__isnull=True
    )


def submit_template_to_provider(template_id) -> None:
    """Idempotent: a no-op once provider_template_id is set or the template is
    no longer PENDING. A transient error propagates so the task retries.

    Meta is never called under a row lock: the template is read, Meta is
    called, and the result is applied under a lock only if the row is still
    unsubmitted. On a permanent refusal the derived name is looked up first,
    because an earlier attempt Meta accepted but ReviewFlow never recorded
    (a crash before the save, or a concurrent attempt) makes the resubmit fail:
    found -> that id is recorded and the template stays PENDING; a completed
    scan with no match -> REJECTED. An inconclusive or failed lookup raises, so
    the task retries and the template stays PENDING: REJECTED is terminal and is
    never concluded from a lookup that could not finish.
    """
    with tenant_atomic():
        template = _unsubmitted(template_id).first()
    if template is None:
        return
    account = _active_shared_account()
    if account is None:
        raise WhatsAppNotConfigured()
    provider = get_provider(account)
    status = MessageTemplate.Status.PENDING
    try:
        provider_template_id = provider.submit_template(account, template)
    except ProviderPermanentError:
        provider_template_id = provider.find_template_id(account, template)
        if provider_template_id is None:
            status = MessageTemplate.Status.REJECTED
    with tenant_atomic():
        locked = _unsubmitted(template_id).select_for_update().first()
        if locked is None:  # another attempt recorded it meanwhile
            return
        locked.provider_template_id = provider_template_id
        locked.status = status
        locked.save(update_fields=["provider_template_id", "status", "updated_at"])


def templates_pending_sync() -> bool:
    return MessageTemplate.objects.filter(
        status=MessageTemplate.Status.PENDING, provider_template_id__isnull=False
    ).exists()


def sync_template_statuses() -> int:
    """Apply PENDING -> APPROVED|REJECTED from Meta for the current merchant's
    submitted templates; an APPROVED/REJECTED template is never moved. Returns
    the number changed. Safe to run twice."""
    account = _active_shared_account()
    if account is None:
        return 0
    provider = get_provider(account)
    # Its own tenant_atomic: tenant_task only sets the contextvar, so without
    # SET LOCAL RLS would hide every row from a real (autocommit) worker.
    with tenant_atomic():
        pending = list(
            MessageTemplate.objects.filter(
                status=MessageTemplate.Status.PENDING, provider_template_id__isnull=False
            ).values_list("id", "provider_template_id")
        )
    changed = 0
    for template_id, provider_template_id in pending:
        # Meta is called with no transaction open and no row locked.
        try:
            result = provider.fetch_template_status(account, provider_template_id)
        except (ProviderTransientError, ProviderPermanentError) as exc:
            logger.warning("Template status sync failed with %s", type(exc).__name__)
            continue
        if result.status == MessageTemplate.Status.PENDING:
            continue
        with tenant_atomic():
            # Applied only if the row is still the same PENDING submission.
            updated = MessageTemplate.objects.filter(
                pk=template_id,
                status=MessageTemplate.Status.PENDING,
                provider_template_id=provider_template_id,
            ).update(status=result.status, updated_at=timezone.now())
        changed += updated
    return changed


# --- shared number quality --------------------------------------------------------


def check_shared_pool_quality() -> None:
    """V1 "internal alert": an error-level log when Meta's quality rating for
    a shared number is in WHATSAPP_QUALITY_ALERT_RATINGS (no merchant context
    needed: it reads only the GLOBAL shared row). A provider error is logged
    by class only."""
    for account in WhatsAppAccount.objects.shared_pool().filter(status=WhatsAppAccount.Status.ACTIVE):
        try:
            rating = get_provider(account).fetch_quality_rating(account)
        except (ProviderTransientError, ProviderPermanentError, WhatsAppNotConfigured) as exc:
            logger.warning("Shared pool quality check failed with %s", type(exc).__name__)
            continue
        if rating in settings.WHATSAPP_QUALITY_ALERT_RATINGS:
            logger.error("whatsapp.shared_pool_quality_low account=%s rating=%s", account.id, rating)


# --- inbound opt-out ----------------------------------------------------------------


def parse_inbound_opt_outs(raw_body: bytes, signature: str | None) -> list[tuple[str, str]]:
    """Verify the Meta signature FIRST (no database access anywhere in this
    function), then return the deduplicated (phone_number_id, E.164 phone)
    pairs whose whole message is an opt-out keyword. Raises
    WebhookSignatureInvalid (401) on a missing/invalid signature or an unset
    META_APP_SECRET. The message text is never logged or returned."""
    provider = get_provider_by_name(WhatsAppAccount.Provider.META_CLOUD)
    if not provider.verify_signature(raw_body, signature):
        # Counted by log alerting (Security-Controls.md, Webhook Security).
        # Nothing from the request is logged.
        logger.warning("whatsapp.webhook_signature_invalid")
        raise WebhookSignatureInvalid()
    try:
        payload = json.loads(raw_body)
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    keywords = {k.casefold() for k in settings.WHATSAPP_OPT_OUT_KEYWORDS}
    pairs: dict[tuple[str, str], None] = {}
    for message in provider.parse_inbound_webhook(payload):
        if message.text is None or message.text.strip().casefold() not in keywords:
            continue
        phone = "+" + message.from_phone.lstrip("+")
        try:
            validate_e164(phone)
        except PayloadValidationError:
            continue
        pairs[(message.phone_number_id, phone)] = None
    return list(pairs)


def is_shared_pool_number(phone_number_id: str) -> bool:
    return (
        WhatsAppAccount.objects.shared_pool()
        .filter(provider=WhatsAppAccount.Provider.META_CLOUD, phone_number_id=phone_number_id)
        .exists()
    )


def shared_opt_out_applies(phone: str) -> bool:
    """OD-1 (b): in the current merchant's context, true when it has a
    Customer with this phone and a location mapped to the shared account."""
    if get_current_merchant_id() is None:
        raise TenantContextError("shared_opt_out_applies requires a tenant context.")
    from customers.models import Customer

    return (
        Customer.objects.filter(phone=phone).exists()
        and WhatsAppLocationMapping.objects.filter(
            whatsapp_account__sender_type=WhatsAppAccount.SenderType.SHARED_POOL
        ).exists()
    )


# --- the single Meta webhook endpoint (spec 08, amended 2026-10-07) ---------------


def verify_subscription(*, mode: str | None, verify_token: str | None, challenge: str | None) -> str:
    """The GET handshake on /webhooks/whatsapp (gate M-2): returns the
    challenge to echo only for hub.mode=subscribe and the configured verify
    token (constant-time compare). Raises WebhookVerificationFailed (403)
    otherwise, including when META_WEBHOOK_VERIFY_TOKEN is unset. No database
    access."""
    expected = settings.META_WEBHOOK_VERIFY_TOKEN
    if (
        not expected
        or mode != "subscribe"
        or not verify_token
        or challenge is None
        or not hmac.compare_digest(verify_token.encode(), expected.encode())
    ):
        raise WebhookVerificationFailed()
    return challenge


def receive_webhook(raw_body: bytes, signature: str | None) -> None:
    """The POST delivery on /webhooks/whatsapp. Verifies the signature first
    (parse_inbound_opt_outs: no database access anywhere on this path), then
    enqueues one fan_out_inbound_opt_out per opt-out keyword message.

    Only value.messages[] is processed. value.statuses[] entries are
    acknowledged by the caller's 200 and discarded here: never persisted, and
    no WhatsAppMessage or Phase 11 behaviour exists. A mixed delivery
    processes its STOPs and ignores its statuses. A broker error is logged
    (class name only) and never fails the delivery."""
    from whatsapp import tasks  # lazy: tasks import this module

    for phone_number_id, phone in parse_inbound_opt_outs(raw_body, signature):
        try:
            tasks.fan_out_inbound_opt_out.delay(phone_number_id, phone)
        except Exception as exc:
            logger.warning("Inbound opt-out enqueue failed with %s", type(exc).__name__)
