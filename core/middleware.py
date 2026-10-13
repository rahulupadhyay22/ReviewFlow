from django.urls import Resolver404, resolve

from core.tenancy import tenant_atomic, tenant_context

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
