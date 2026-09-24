"""
ApiKeyMiddleware: the only place a Bearer request is authenticated against
ApiKey (spec .claude/specs/05-public-api-keys.md §"Opt-in mechanism").

Placed after accounts.middleware.SessionMerchantMiddleware and before
core.middleware.TenantMiddleware (config/settings.py MIDDLEWARE), so
setting request.merchant_id here lets TenantMiddleware wrap the view in the
usual tenant_context()+tenant_atomic() unchanged -- this is where the
pre-tenant lookup (T1) and the best-effort last_used_at touch (T2) both run
and commit, strictly before the request-wide tenant transaction (T3) opens.

Acts only when both hold: the Authorization header uses the Bearer scheme,
and apikeys.authentication.is_api_key_eligible(request) says the resolved
view opted in for this HTTP method. On any other request it does nothing,
so a Bearer header on a session-only view is simply ignored there.
"""
import logging

from django.db import DatabaseError

from apikeys import services
from apikeys.authentication import is_api_key_eligible
from apikeys.exceptions import InvalidApiKey
from apikeys.throttling import ApiKeyIpRateThrottle

logger = logging.getLogger(__name__)


class ApiKeyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        scheme, _, token = request.META.get("HTTP_AUTHORIZATION", "").partition(" ")
        if scheme.lower() == "bearer" and is_api_key_eligible(request):
            self._authenticate(request, token)
        return self.get_response(request)

    @staticmethod
    def _authenticate(request, token):
        request.api_key_attempt = True
        # Bearer is exclusive: drop any session-derived merchant so it can
        # never mix with the key (Authentication.md §2: "never mix").
        request.merchant_id = None

        throttle = ApiKeyIpRateThrottle()
        if not throttle.allow_request(request, None):
            # Runs before the hash lookup, so invalid-key attempts are
            # counted too (spec Decision 8).
            request.api_key_throttled = True
            request.api_key_throttle_wait = throttle.wait()
            return

        try:
            key = services.authenticate_api_key(token)
        except InvalidApiKey:
            request.api_key_error = True
            return

        try:
            services.touch_last_used(key)
        except DatabaseError as exc:
            # Best-effort operational metadata only (spec Decision 6): never
            # turn a valid authenticated request into a failure. No key,
            # hash, or merchant-identifying payload is logged.
            logger.warning("api_key last_used_at update skipped: %s", type(exc).__name__)

        request.api_key = key
        request.merchant_id = key.merchant_id
