from core.exceptions import ReviewFlowError


class InvalidCredentials(ReviewFlowError):
    """One generic login failure for every reason (no user enumeration)."""

    http_status = 401
    code = "invalid_credentials"

    def __init__(self):
        super().__init__("Invalid email or password.")


class InvalidInvite(ReviewFlowError):
    """One generic failure for every invite-token/accept problem (no
    enumeration of bad signature vs. expired vs. already accepted vs.
    wrong password)."""

    http_status = 400
    code = "invalid_invite"

    def __init__(self):
        super().__init__("Invalid or expired invite.")


class AlreadyMember(ReviewFlowError):
    """The invited email is already an accepted member of this merchant."""

    http_status = 409
    code = "already_member"

    def __init__(self):
        super().__init__("This person is already a team member.")


class LastOwner(ReviewFlowError):
    """The change would leave the merchant with no accepted OWNER."""

    http_status = 409
    code = "last_owner"

    def __init__(self):
        super().__init__("A merchant must always have at least one owner.")


class TeamPermissionDenied(ReviewFlowError):
    """A role-management action the actor's role does not allow (self
    target, or a non-OWNER touching/granting OWNER)."""

    http_status = 403
    code = "team_permission_denied"

    def __init__(self):
        super().__init__("You do not have permission to manage this team member.")


class TeamMemberNotFound(ReviewFlowError):
    """The member id is not in the current merchant (never 403: don't reveal
    cross-tenant existence)."""

    http_status = 404
    code = "not_found"

    def __init__(self):
        super().__init__("Team member not found.")


class ReauthenticationFailed(ReviewFlowError):
    """One generic failure for a wrong password or a wrong/replayed 2FA code
    on setup/disable (no enumeration of which factor was wrong)."""

    http_status = 400
    code = "reauthentication_failed"

    def __init__(self):
        super().__init__("Incorrect password or code.")


class InvalidTotpCode(ReviewFlowError):
    """The code given to confirm enrollment does not verify."""

    http_status = 400
    code = "invalid_totp_code"

    def __init__(self):
        super().__init__("Invalid authentication code.")


class TotpAlreadyEnabled(ReviewFlowError):
    """Setup or confirm was called for a user who already has 2FA enabled."""

    http_status = 409
    code = "totp_already_enabled"

    def __init__(self):
        super().__init__("Two-factor authentication is already enabled.")


class TotpSetupRequired(ReviewFlowError):
    """Confirm was called with no pending setup."""

    http_status = 409
    code = "totp_setup_required"

    def __init__(self):
        super().__init__("Start two-factor setup first.")


class TotpNotEnabled(ReviewFlowError):
    """Disable was called for a user who does not have 2FA enabled."""

    http_status = 409
    code = "totp_not_enabled"

    def __init__(self):
        super().__init__("Two-factor authentication is not enabled.")
