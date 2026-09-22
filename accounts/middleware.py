from django.urls import reverse


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
    IsMerchantMember.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._pre_tenant_paths = None

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
            merchant_id
            and request.path_info not in self._pre_tenant_paths
            and request.user.is_authenticated
        ):
            request.merchant_id = merchant_id
        return self.get_response(request)
