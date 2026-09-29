from core.exceptions import ReviewFlowError


class IntegrationNotFound(ReviewFlowError):
    """The integration id does not exist or belongs to another merchant.
    Never 403: don't reveal cross-tenant existence."""

    http_status = 404
    code = "not_found"

    def __init__(self):
        super().__init__("Integration not found.")


class MappingExists(ReviewFlowError):
    """Duplicate (integration_id, location_id) mapping."""

    http_status = 409
    code = "mapping_exists"

    def __init__(self):
        super().__init__("A mapping for this location already exists.")


class IntegrationExists(ReviewFlowError):
    """A CONNECTED "api" integration already exists for this merchant (spec
    06 Decision 4: at most one connected api integration per merchant)."""

    http_status = 409
    code = "integration_exists"

    def __init__(self):
        super().__init__("A connected integration for this provider already exists.")


class IntegrationNotConnected(ReviewFlowError):
    """No CONNECTED "api" integration exists for the merchant. Raised by
    /sales, which needs exactly one to attribute the sale to (spec 06
    Decision 4)."""

    http_status = 422
    code = "integration_not_connected"

    def __init__(self):
        super().__init__("No connected Generic REST API integration for this merchant.")


class IntegrationDisconnected(ReviewFlowError):
    """record_event() was called against a DISCONNECTED integration.
    No HTTP mapping -- this is a service-boundary guard, not exposed
    through a webhook receiver in Phase 04."""


class WebhookRejected(ReviewFlowError):
    """A provider webhook failed lookup, provider-match or signature
    verification (spec 06 Decision 1). Every pre-verification failure mode
    returns this identical response -- never a more specific one, so a
    caller cannot distinguish "unknown id" from "bad signature" (fail
    closed, Authentication.md §3)."""

    http_status = 401
    code = "invalid_signature"

    def __init__(self):
        super().__init__("Invalid webhook signature.")


class PayloadRejected(ReviewFlowError):
    """A verified webhook's payload failed the receipt-time
    get_external_event_id() check (spec 06 Decision 7/16, e.g. a missing
    mapped external_transaction_id) -- 422 with the safe error code.
    Nothing is stored for this response. Wraps a
    integrations.core.schemas.PayloadValidationError's code, lower-cased to
    match the API's snake_case error codes."""

    http_status = 422

    def __init__(self, code: str):
        self.code = code.lower()
        super().__init__("The event payload failed validation.")


class InvalidWebhookPayload(ReviewFlowError):
    """A verified webhook body was not a JSON object (spec 06 Decision 8/11).
    Nothing is stored for this response."""

    http_status = 400
    code = "invalid_payload"

    def __init__(self):
        super().__init__("The webhook payload is not a JSON object.")


class SaleNotProcessed(ReviewFlowError):
    """POST /sales: the event exists but is terminal (DEAD_LETTER or
    CANCELLED) and will never produce a Transaction (spec 06 Decision 8).
    Carries event_id/event_status for the view to render alongside the
    standard error body -- it is not raised through the global DRF handler,
    because that renders only {code, message}."""

    code = "sale_not_processed"

    def __init__(self, *, event_id, event_status):
        self.event_id = event_id
        self.event_status = event_status
        super().__init__("The event will not be processed.")


class LocationUnresolved(ReviewFlowError):
    """A sale could not be mapped to a Location. No HTTP mapping in the
    Phase 04 pipeline (events.services.SAFE_ERRORS LOCATION_UNRESOLVED,
    never surfaced as an HTTP error there). POST /sales (spec 06 Decision 8)
    pre-validates and maps this to 422 location_unresolved directly -- see
    integrations.services.ingest_api_sale."""

    http_status = 422
    code = "location_unresolved"
