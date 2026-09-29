"""Per-IP throttle for unauthenticated provider webhook receivers (spec 06
Decision 15). Mirrors apikeys.throttling.ApiKeyIpRateThrottle: it runs
before any lookup or signature check, so failed/invalid deliveries are
counted too."""
from rest_framework.throttling import SimpleRateThrottle


class WebhookIpRateThrottle(SimpleRateThrottle):
    scope = "webhook_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}
