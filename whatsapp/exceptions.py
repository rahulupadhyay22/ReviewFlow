"""WhatsApp domain errors (spec 08). Each declares its HTTP mapping for
core.api.exception_handler. Messages are fixed text: never a phone number,
token, template body or provider response."""
from core.exceptions import ReviewFlowError


class WhatsAppNotConfigured(ReviewFlowError):
    """No ACTIVE shared sender, or the platform Meta token is unset."""

    http_status = 503
    code = "whatsapp_not_configured"

    def __init__(self):
        super().__init__("WhatsApp sending is not configured.")


class SenderNotFound(ReviewFlowError):
    http_status = 404
    code = "not_found"

    def __init__(self):
        super().__init__("Not found.")


class TemplateConflict(ReviewFlowError):
    http_status = 409
    code = "template_conflict"

    def __init__(self):
        super().__init__("A template with this name and language already exists.")


class WebhookSignatureInvalid(ReviewFlowError):
    """Missing or invalid Meta signature (or META_APP_SECRET unset): fail closed."""

    http_status = 401
    code = "invalid_signature"

    def __init__(self):
        super().__init__("Invalid signature.")


class WebhookVerificationFailed(ReviewFlowError):
    """The Meta subscription handshake had the wrong mode or verify token, or
    META_WEBHOOK_VERIFY_TOKEN is unset: fail closed."""

    http_status = 403
    code = "forbidden"

    def __init__(self):
        super().__init__("Verification failed.")


class SenderInUse(ReviewFlowError):
    """An own number cannot be disconnected while a location is still mapped to
    it (spec 08-whatsapp-embedded-signup ES-5). Locations are never unmapped
    implicitly."""

    http_status = 409
    code = "sender_in_use"

    def __init__(self):
        super().__init__("This sender is still mapped to a location.")


class WebhookEnqueueFailed(ReviewFlowError):
    """A valid, signed opt-out keyword could not be published to the broker.
    Answered 503 so Meta redelivers it (foundation Amendment A-1; WhatsApp-Meta.md
    M-11): answering 200 would lose the STOP for good."""

    http_status = 503
    code = "service_unavailable"

    def __init__(self):
        super().__init__("The webhook could not be processed. Please retry.")


class OptOutTargetLookupFailed(ReviewFlowError):
    """The target lookup for an inbound opt-out failed (the shared-account,
    merchant or own-number read). Carries ONLY the original exception's class
    name, never its text: a database or driver error can contain host, DSN or
    SQL fragments, and this is what Celery's retry and failure logs print
    (foundation Amendment A-1 follow-up)."""

    def __init__(self, error_class: str):
        super().__init__(f"Inbound opt-out target lookup failed ({error_class}).")


class OptOutFanOutIncomplete(ReviewFlowError):
    """One or more merchants could not be opted out in a fan-out run. Raised
    after every merchant was attempted, so the failed ones are retried (bounded)
    and never dropped silently (foundation Amendment A-1)."""

    def __init__(self, failed_count: int | None = None):
        suffix = "" if failed_count is None else f" ({failed_count} merchant(s))"
        super().__init__("Inbound opt-out fan-out is incomplete" + suffix + ".")
