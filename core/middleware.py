from core.tenancy import tenant_atomic, tenant_context


class TenantMiddleware:
    """Runs the request inside the tenant context once auth has set
    request.merchant_id (Phase 02). Without it, the request passes through."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        merchant_id = getattr(request, "merchant_id", None)
        if merchant_id is None:
            return self.get_response(request)
        with tenant_context(merchant_id), tenant_atomic():
            return self.get_response(request)
