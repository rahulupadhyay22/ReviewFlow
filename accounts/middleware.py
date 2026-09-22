from django.urls import reverse


class SessionMerchantMiddleware:
    """Sets request.merchant_id from the session for TenantMiddleware.

    Skips the login URL: login runs before any tenant context
    (user_lookup_atomic refuses nesting), even for an already logged-in user.
    Membership and role are re-checked per request by IsMerchantMember.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._login_path = None

    def __call__(self, request):
        if self._login_path is None:
            # ponytail: login is the one pre-tenant endpoint, matched by path.
            self._login_path = reverse("auth-login")
        merchant_id = request.session.get("merchant_id")
        if (
            merchant_id
            and request.path_info != self._login_path
            and request.user.is_authenticated
        ):
            request.merchant_id = merchant_id
        return self.get_response(request)
