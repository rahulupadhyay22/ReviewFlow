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
