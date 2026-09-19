class ReviewFlowError(Exception):
    """Base class for ReviewFlow application errors."""


class TenantContextError(ReviewFlowError):
    """Tenant work attempted without (or with a conflicting) tenant context."""
