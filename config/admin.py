"""ReviewFlow's Django Admin site (spec 16). Customized Django Admin, not a
bespoke admin app (Architecture.md §Foundational 6).

On top of the stock site:
- login needs the password plus a TOTP or recovery code (StaffAdminLoginForm),
  and POSTs are throttled per IP;
- has_permission also requires the session flag that only a successful admin
  login sets, so a dashboard-only staff session never reaches admin;
- the read-only dead-letter IntegrationEvent page (events.admin).
The IP allowlist is core.middleware.AdminIPAllowlistMiddleware."""
from django.contrib import admin
from django.http import HttpResponse
from django.urls import path
from rest_framework.throttling import SimpleRateThrottle

from accounts.forms import StaffAdminLoginForm

ADMIN_2FA_SESSION_KEY = "admin_2fa_user_id"


class AdminLoginThrottle(SimpleRateThrottle):
    """Per-IP limit on admin login POSTs, keyed on the same client ident as
    the allowlist (DJANGO_NUM_PROXIES rule)."""

    scope = "admin_login"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class ReviewFlowAdminSite(admin.AdminSite):
    login_form = StaffAdminLoginForm
    login_template = "admin/staff_login.html"  # accounts/templates: renders otp_code

    def has_permission(self, request):
        return super().has_permission(request) and request.session.get(ADMIN_2FA_SESSION_KEY) == str(
            request.user.pk
        )

    def login(self, request, extra_context=None):
        if request.method == "POST" and not AdminLoginThrottle().allow_request(request, None):
            return HttpResponse("Too many login attempts. Try again later.", status=429)
        response = super().login(request, extra_context)
        # Set only after LoginView has run auth_login(): login() may flush the
        # session (another user's session), which would drop an earlier flag.
        verified_id = getattr(request, "_admin_2fa_user_id", None)
        if request.method == "POST" and verified_id is not None and request.user.pk == verified_id:
            request.session[ADMIN_2FA_SESSION_KEY] = str(verified_id)
        return response

    def get_urls(self):
        from events.admin import dead_letter_events_view

        return [
            path(
                "events/dead-letter/<uuid:merchant_id>/",
                self.admin_view(dead_letter_events_view),
                name="events_dead_letter",
            ),
        ] + super().get_urls()
