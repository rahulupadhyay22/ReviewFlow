from core.exceptions import ReviewFlowError


class CustomerNotFound(ReviewFlowError):
    """Unknown or another merchant's customer. Never 403: don't reveal
    cross-tenant existence."""

    http_status = 404
    code = "not_found"

    def __init__(self):
        super().__init__("Customer not found.")
