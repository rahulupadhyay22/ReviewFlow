import ipaddress

from django.conf import settings
from django.http import HttpResponseNotFound
from django.urls import Resolver404, resolve, reverse
from rest_framework.throttling import BaseThrottle

from core.tenancy import tenant_atomic, tenant_context


def client_ip(request) -> str:
    """The client IP under the same DJANGO_NUM_PROXIES rule as the DRF
    throttles (REMOTE_ADDR at 0, else the Nth-from-last X-Forwarded-For
    entry). Never the raw header."""
    return BaseThrottle().get_ident(request) or ""


def admin_ip_allowed(request) -> bool:
    """Fail closed (spec 16 D5): an empty ADMIN_ALLOWED_IPS, any unparsable
    entry, or an unparsable client IP denies everyone."""
    entries = settings.ADMIN_ALLOWED_IPS
    if not entries:
        return False
    try:
        networks = [ipaddress.ip_network(entry.strip(), strict=False) for entry in entries]
        ip = ipaddress.ip_address(client_ip(request))
    except ValueError:
        return False
    return any(ip in network for network in networks)


class AdminIPAllowlistMiddleware:
    """Django Admin is reachable only from ADMIN_ALLOWED_IPS
    (Security-Controls.md §Privileged Admin Operations). Everyone else gets
    404, so the admin's existence is not revealed. Settings are read per
    request."""

    def __init__(self, get_response):
        self.get_response = get_response
        self._admin_prefix = None

    def __call__(self, request):
        if self._admin_prefix is None:
            self._admin_prefix = reverse("admin:index")
        # "/admin" without the slash must be hidden too: CommonMiddleware would
        # otherwise answer it with a 301 to "/admin/", revealing the admin.
        path = request.path_info
        if (path == self._admin_prefix.rstrip("/") or path.startswith(self._admin_prefix)) and not admin_ip_allowed(
            request
        ):
            return HttpResponseNotFound()
        return self.get_response(request)

# URL names that run inside the tenant context but NOT inside the middleware's
# request-wide transaction. Exactly one entry, by design (spec
# 08-whatsapp-embedded-signup D-1 Option A, approved 2026-10-09; ES-4).
#
# Embedded Signup completion makes three Meta calls that the approved ES-4
# exception requires to run outside any database transaction or row lock, with
# the pending account committed before the registration call. One request-wide
# tenant_atomic() would make that impossible. That view therefore sets the
# tenant context only, and its service opens its own short tenant_atomic()
# blocks (state consumption, persistence) around the external calls.
#
# Applies to POST only. Matched by resolved url_name, never cached, so a test
# URLconf is honoured. A view added to this set that does not open its own
# tenant_atomic() fails CLOSED, never cross-tenant: with no SET LOCAL,
# app.current_merchant_id is unset, so RLS returns no rows and rejects writes.
# Do not widen this set without fresh sign-off.
_NON_ATOMIC_TENANT_URL_NAMES = frozenset({"whatsapp-embedded-signup-complete"})


def _is_non_atomic_tenant_request(request) -> bool:
    if request.method != "POST":
        return False
    try:
        match = resolve(request.path_info, urlconf=getattr(request, "urlconf", None))
    except Resolver404:
        return False
    return match.url_name in _NON_ATOMIC_TENANT_URL_NAMES


class TenantMiddleware:
    """Runs the request inside the tenant context once auth has set
    request.merchant_id (Phase 02). Without it, the request passes through.

    Every request runs inside one tenant transaction, except a POST to a view
    named in _NON_ATOMIC_TENANT_URL_NAMES, which gets the tenant context only."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        merchant_id = getattr(request, "merchant_id", None)
        if merchant_id is None:
            return self.get_response(request)
        if _is_non_atomic_tenant_request(request):
            with tenant_context(merchant_id):
                return self.get_response(request)
        with tenant_context(merchant_id), tenant_atomic():
            return self.get_response(request)
