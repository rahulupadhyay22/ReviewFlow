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


class IntegrationDisconnected(ReviewFlowError):
    """record_event() was called against a DISCONNECTED integration.
    No HTTP mapping -- this is a service-boundary guard, not exposed
    through a webhook receiver in Phase 04."""


class LocationUnresolved(ReviewFlowError):
    """A sale could not be mapped to a Location. No HTTP mapping -- this
    is a processing failure (events.services.SAFE_ERRORS
    LOCATION_UNRESOLVED), never surfaced as an HTTP error."""
