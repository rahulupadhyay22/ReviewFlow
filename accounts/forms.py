"""Django Admin staff login form (spec 16 D2). Lives here, not in
accounts/admin.py, because config.admin imports it while the admin site is
being built, and accounts/admin.py itself touches admin.site at import."""
import logging

from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.db import DatabaseError
from django.views.decorators.debug import sensitive_variables

from accounts.services import verify_staff_admin_login

logger = logging.getLogger(__name__)


class StaffAdminLoginForm(AdminAuthenticationForm):
    """Password plus a TOTP or recovery code. Every failure (wrong password,
    non-staff, no 2FA enrolled, wrong/replayed/missing code) raises the same
    invalid-login error. On success it only marks the request; the admin site
    sets the session flag after auth_login() has run."""

    otp_code = forms.CharField(
        label="Authentication or recovery code",
        max_length=64,
        required=False,
        strip=True,
        # PasswordInput never re-renders the submitted value, so a failed
        # login cannot echo a recovery code back into the page.
        widget=forms.PasswordInput(attrs={"autocomplete": "one-time-code"}),
    )

    @sensitive_variables()  # scrub every local: password, TOTP and recovery codes
    def clean(self):
        cleaned = super().clean()  # password + is_staff (raises invalid_login)
        user = self.get_user()
        if user is None:
            # A blank username or password: the parent never authenticated, so
            # there is no user to verify a code for. Same generic error.
            raise self.get_invalid_login_error()
        code = cleaned.get("otp_code") or ""
        try:
            verified = verify_staff_admin_login(user=user, code=code)
        except DatabaseError:
            # The recovery-code audit failed, so its use rolled back: fail
            # closed with the generic error. No code or user detail logged.
            logger.error("admin.login_second_factor_audit_failed")
            verified = False
        if not verified:
            raise self.get_invalid_login_error()
        self.request._admin_2fa_user_id = user.pk
        return cleaned
