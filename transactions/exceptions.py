from core.exceptions import ReviewFlowError


class TransactionNotFound(ReviewFlowError):
    """The transaction id does not exist, belongs to another merchant, or
    (for a MANAGER) is at a location not assigned to the caller. Never 403:
    don't reveal cross-tenant existence or assignment scope."""

    http_status = 404
    code = "not_found"

    def __init__(self):
        super().__init__("Transaction not found.")
