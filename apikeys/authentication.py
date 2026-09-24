"""
Public-API key authentication (Authentication.md §2, spec
.claude/specs/05-public-api-keys.md §"Opt-in mechanism (normative)").

Opt-in is explicit and per-view, never inferred from the URL: a view accepts
API keys only by inheriting ApiKeyOptInMixin and declaring a non-empty
api_key_methods. apikeys.middleware.ApiKeyMiddleware is the only thing that
reads is_api_key_eligible() / sets the markers this module reads; nothing
here infers eligibility on its own.
"""
from django.urls import Resolver404, resolve
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed, Throttled

from apikeys.throttling import ApiKeyRateThrottle


class ApiKeyOptInMixin:
    """Mixed into a view class to opt specific HTTP methods into API-key
    auth. The default (empty) api_key_methods means the view is never
    eligible -- listing ApiKeyAuthentication in authentication_classes
    without this mixin has no effect, because eligibility and the
    authenticator swap both live here, keyed off the same attribute."""

    api_key_methods: frozenset[str] = frozenset()

    def _is_api_key_attempt(self) -> bool:
        # self.request is the Django HttpRequest set by View.setup() before
        # DRF builds its Request; DRF's Request proxies unknown attribute
        # reads to it, so this works in both phases.
        return bool(getattr(self.request, "api_key_attempt", False))

    def get_authenticators(self):
        if self._is_api_key_attempt():
            # Exclusive: a Bearer attempt is judged by the key alone. The
            # session is never consulted, and an invalid key never falls
            # back to it.
            return [ApiKeyAuthentication()]
        return super().get_authenticators()

    def get_throttles(self):
        throttles = super().get_throttles()
        if self._is_api_key_attempt():
            throttles.append(ApiKeyRateThrottle())
        return throttles


def is_api_key_eligible(request) -> bool:
    """True iff the resolved view for this request/method has explicitly
    opted in via ApiKeyOptInMixin.api_key_methods. Never inferred from the
    path, URL name, or any naming convention -- only from the resolved view
    class's own declared attribute."""
    try:
        match = resolve(request.path_info, urlconf=getattr(request, "urlconf", None))
    except Resolver404:
        return False
    cls = getattr(match.func, "cls", None)
    api_key_methods = getattr(cls, "api_key_methods", frozenset())
    return request.method in api_key_methods


class ApiKeyAuthenticationFailed(AuthenticationFailed):
    """default_code (not just the instance detail's code) must be
    invalid_api_key, because core.api.exception_handler reads
    exc.default_code for the response body's error.code."""

    default_detail = "Invalid or revoked API key."
    default_code = "invalid_api_key"


class ApiKeyPrincipal:
    """Minimal non-User authenticated principal for a Bearer request. Never
    a User, never has a TeamMember -- accounts.permissions.IsMerchantMember
    (which calls accounts.services.get_active_membership(request.user)) must
    never be reached for this principal; see
    accounts.permissions.IsMerchantMemberOrApiKey."""

    is_authenticated = True

    def __init__(self, api_key):
        self.api_key = api_key

    def __str__(self):
        return f"ApiKeyPrincipal({self.api_key_id})"

    @property
    def api_key_id(self):
        return self.api_key.pk


class ApiKeyAuthentication(BaseAuthentication):
    """Only ever installed for a request apikeys.middleware.ApiKeyMiddleware
    marked as a Bearer attempt (via ApiKeyOptInMixin.get_authenticators()).
    Owns the DRF-side 401/429 so the response goes through the standard
    core.api error shape, including WWW-Authenticate."""

    def authenticate(self, request):
        django_request = request._request
        if getattr(django_request, "api_key_throttled", False):
            raise Throttled(wait=getattr(django_request, "api_key_throttle_wait", None))
        if getattr(django_request, "api_key_error", False):
            raise ApiKeyAuthenticationFailed()
        api_key = getattr(django_request, "api_key", None)
        if api_key is None:
            return None
        return (ApiKeyPrincipal(api_key), api_key)

    def authenticate_header(self, request):
        return "Bearer"
