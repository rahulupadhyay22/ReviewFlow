"""HasApiKeyScope: not used by any endpoint in Phase 05 (GET /merchant needs
no scope -- spec Decision 13). Built for Phase 06's POST /sales
(sales:write) and unit-tested here."""
from rest_framework.permissions import BasePermission

from apikeys.models import ApiKey


def HasApiKeyScope(scope):
    class _HasApiKeyScope(BasePermission):
        def has_permission(self, request, view):
            auth = getattr(request, "auth", None)
            return isinstance(auth, ApiKey) and auth.is_active and scope in auth.scopes_json

    _HasApiKeyScope.__name__ = f"HasApiKeyScope_{scope.replace(':', '_')}"
    return _HasApiKeyScope
