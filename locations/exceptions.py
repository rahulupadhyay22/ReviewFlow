from core.exceptions import ReviewFlowError


class LocationNotFound(ReviewFlowError):
    """The location id does not exist, belongs to another merchant, or (for
    a MANAGER) is not assigned to the caller. Never 403: don't reveal
    cross-tenant existence, and don't reveal assignment scope either."""

    http_status = 404
    code = "not_found"

    def __init__(self):
        super().__init__("Location not found.")
