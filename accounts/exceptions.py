from core.exceptions import ReviewFlowError


class InvalidCredentials(ReviewFlowError):
    """One generic login failure for every reason (no user enumeration)."""

    http_status = 401
    code = "invalid_credentials"

    def __init__(self):
        super().__init__("Invalid email or password.")
