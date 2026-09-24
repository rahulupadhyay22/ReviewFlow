"""
Per-key and per-IP throttles for the public API (Authentication.md §2,
Security-Controls.md §Rate Limiting). Both are DRF SimpleRateThrottle
subclasses so their rate/window bookkeeping lives in the shared Redis cache
(rate-limit counters only, never business data -- SAD.md).
"""
from rest_framework.throttling import SimpleRateThrottle

from apikeys.models import ApiKey


class ApiKeyRateThrottle(SimpleRateThrottle):
    """Independent counter per key (Authentication.md: "rate-limited
    independently"). Applies only when request.auth is an ApiKey -- see
    apikeys.authentication.ApiKeyOptInMixin.get_throttles()."""

    scope = "api_key"

    def get_cache_key(self, request, view):
        auth = getattr(request, "auth", None)
        if not isinstance(auth, ApiKey):
            return None
        return self.cache_format % {"scope": self.scope, "ident": str(auth.pk)}


class ApiKeyIpRateThrottle(SimpleRateThrottle):
    """Keyed on the client IP via DRF's existing get_ident(), which honors
    NUM_PROXIES exactly as the Phase 02 login throttles do -- no new
    trusted-proxy logic. Runs inside apikeys.middleware.ApiKeyMiddleware,
    before the key lookup, so failed-key attempts are counted too."""

    scope = "api_key_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}
