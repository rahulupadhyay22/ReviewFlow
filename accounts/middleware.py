from django.urls import Resolver404, resolve, reverse

# Provider webhook receivers (spec 06 Decision 1/11): pre-tenant by
# url_name, not path, because their path carries a variable integration_id.
# A webhook request that also happens to carry a logged-in dashboard
# session must never be tenant-wrapped: integration_lookup_atomic()
# refuses to run inside an active merchant context, and the receiver's own
# signature/lookup outcome -- not the caller's unrelated session -- must
# decide the response (spec 06 Definition of done).
_PRE_TENANT_URL_NAMES = frozenset({"webhook-generic", "webhook-shopify", "webhook-billing-razorpay"})


class SessionMerchantMiddleware:
    """Sets request.merchant_id from the session for TenantMiddleware.

    Skips the pre-tenant URLs: login, login/totp and accept-invite all run
    before any tenant context (user_lookup_atomic refuses nesting), even for
    an already logged-in user — e.g. an existing user, logged in to merchant
    B, opening a merchant A invite link must not be tenant-wrapped into B
    while accept_invite() opens its own user_lookup_atomic(). login/totp is
    pre-tenant for a stronger reason: the session there is only a pending,
    unauthenticated 2FA marker (accounts.services.make_pending_totp), never
    request.user.is_authenticated, so this middleware would not act on it
    anyway — the path is listed for clarity, not because it changes
    behavior. Membership and role are re-checked per request by
    IsMerchantMember. Provider webhook receivers are skipped by resolved
    url_name -- see _PRE_TENANT_URL_NAMES.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._pre_tenant_paths = None

    @staticmethod
    def _is_pre_tenant_url_name(request) -> bool:
        try:
            match = resolve(request.path_info, urlconf=getattr(request, "urlconf", None))
        except Resolver404:
            return False
        return match.url_name in _PRE_TENANT_URL_NAMES

    def __call__(self, request):
        if self._pre_tenant_paths is None:
            # ponytail: pre-tenant endpoints, matched by path.
            self._pre_tenant_paths = {
                reverse("auth-login"),
                reverse("auth-login-totp"),
                reverse("auth-accept-invite"),
            }
        merchant_id = request.session.get("merchant_id")
        if (
            not merchant_id
            or request.path_info in self._pre_tenant_paths
            or not request.user.is_authenticated
        ):
            return self.get_response(request)
        # resolve() only runs for the rare case a session is present and the
        # path isn't already a known pre-tenant path.
        if self._is_pre_tenant_url_name(request):
            return self.get_response(request)
        request.merchant_id = merchant_id
        return self.get_response(request)
