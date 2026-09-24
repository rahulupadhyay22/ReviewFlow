from core.exceptions import ReviewFlowError


class InvalidApiKey(ReviewFlowError):
    """One generic failure for every public-API key rejection reason: a
    malformed token, an unknown key, a revoked key, or a key whose merchant
    is not ACTIVE (Authentication.md §2). No http_status/code here -- this
    is caught inside apikeys.middleware.ApiKeyMiddleware, which never lets
    it reach DRF. The 401 + WWW-Authenticate: Bearer response is built by
    apikeys.authentication.ApiKeyAuthentication instead."""


class InvalidScopes(ReviewFlowError):
    """create_api_key() was given an empty, non-list, unknown, or duplicate
    scope."""

    http_status = 422
    code = "invalid_scopes"

    def __init__(self, message="Invalid API key scopes."):
        super().__init__(message)


class ApiKeyNotFound(ReviewFlowError):
    """The key id is not in the current merchant (never 403: don't reveal
    cross-tenant existence -- same pattern as accounts.exceptions.TeamMemberNotFound)."""

    http_status = 404
    code = "not_found"

    def __init__(self):
        super().__init__("API key not found.")
