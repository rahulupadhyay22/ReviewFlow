"""Enroll a staff user in TOTP 2FA for Django Admin (spec 16 D2, LC-1).

Staff have no merchant session, so they cannot use POST /auth/2fa/setup. The
operator runs this with the staff member present: it prints the secret and
otpauth URI once, reads the confirming code, then prints the 10 recovery codes
once. Nothing here is logged; confirmation writes one platform audit row
(user.totp_enabled) with no metadata. Every failure exits non-zero with a
fixed message."""
from django.core.management.base import BaseCommand, CommandError
from django.views.decorators.debug import sensitive_variables

from accounts import services
from accounts.exceptions import InvalidTotpCode, NotActiveStaff, TotpAlreadyEnabled, TotpSetupRequired
from accounts.models import User, UserManager

_FAILURES = {
    NotActiveStaff: "Not enrolled: the user is not an active staff user.",
    TotpAlreadyEnabled: "Not enrolled: two-factor authentication is already enabled.",
    TotpSetupRequired: "Not enrolled: setup was not started.",
    InvalidTotpCode: "Not enrolled: the code did not verify. Run the command again.",
}


class Command(BaseCommand):
    help = "Enroll a staff user in TOTP two-factor authentication for Django Admin."

    def add_arguments(self, parser):
        parser.add_argument("email")

    @sensitive_variables("secret", "uri", "code", "codes")
    def handle(self, *args, **options):
        try:
            user = User.objects.get(email=UserManager.normalize_email(options["email"]))
        except User.DoesNotExist:
            raise CommandError("Not enrolled: no user with that email.") from None
        try:
            secret, uri = services.begin_staff_totp_setup(user=user)
            self.stdout.write("Add this account to the authenticator app (shown once):")
            self.stdout.write(f"  Secret: {secret}")
            self.stdout.write(f"  URI:    {uri}")
            code = input("Enter the 6-digit code from the app: ")
            codes = services.confirm_staff_totp_setup(user=user, code=code)
        except tuple(_FAILURES) as exc:
            raise CommandError(_FAILURES[type(exc)]) from None
        self.stdout.write("Recovery codes (shown once; store them securely):")
        for recovery_code in codes:
            self.stdout.write(f"  {recovery_code}")
        self.stdout.write(self.style.SUCCESS("Two-factor authentication enabled."))
