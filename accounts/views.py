from django.contrib.auth import login, logout
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from accounts import services
from accounts.exceptions import InvalidCredentials
from accounts.permissions import IsMerchantMember, IsOwnerOrAdmin
from accounts.serializers import (
    AcceptInviteSerializer,
    LoginSerializer,
    MerchantSerializer,
    MerchantUpdateSerializer,
    TeamMemberInviteSerializer,
    TeamMemberRoleSerializer,
    TeamMemberSerializer,
    TotpCodeSerializer,
    TotpDisableSerializer,
    TotpSetupSerializer,
    session_body,
)


class LoginRateThrottle(AnonRateThrottle):
    scope = "login"


# APIView.as_view() marks views csrf-exempt; login is public, so opt CSRF back in.
@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_throttles(self):
        return [LoginRateThrottle()] if self.request.method == "POST" else []

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        """Sets the csrftoken cookie for the frontend's first POST."""
        return Response(status=204)

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        member, totp_required = services.begin_login(
            request=request._request, **serializer.validated_data
        )
        if totp_required:
            # Not a login: flush any existing session and store only the
            # unauthenticated pending marker (no _auth_user_id, no
            # merchant_id). login() runs only after LoginTotpView verifies
            # a code — see accounts/middleware.py and the spec's "step-2
            # auth race" section for why.
            request.session.flush()
            request.session["pending_totp"] = services.make_pending_totp(member.user)
            return Response({"totp_required": True})
        login(request._request, member.user)  # cycles the session key
        request.session["merchant_id"] = str(member.merchant_id)
        return Response(session_body(member))


class LoginTotpRateThrottle(AnonRateThrottle):
    scope = "login_totp"


# Public and pre-tenant, like LoginView: accounts.middleware.SessionMerchantMiddleware
# skips this path too.
@method_decorator(csrf_protect, name="dispatch")
class LoginTotpView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_throttles(self):
        return [LoginTotpRateThrottle()]

    def post(self, request):
        serializer = TotpCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pending = request.session.get("pending_totp")
        try:
            member = services.complete_totp_login(pending=pending, **serializer.validated_data)
        except InvalidCredentials:
            # Persist the failed-attempt count (or drop the marker at the
            # cap) so a fresh step 1 is required after too many wrong codes.
            # Unlocked by design; see services.record_failed_totp_attempt
            # for the accepted bound.
            updated = services.record_failed_totp_attempt(pending) if pending else None
            if updated is None:
                request.session.pop("pending_totp", None)
            else:
                request.session["pending_totp"] = updated
            raise
        request.session.pop("pending_totp", None)
        login(request._request, member.user)  # cycles the session key
        request.session["merchant_id"] = str(member.merchant_id)
        return Response(session_body(member))


class TotpManageRateThrottle(UserRateThrottle):
    scope = "totp_manage"


class TotpSetupView(APIView):
    """Start or restart enrollment. Session required, all roles: 2FA is
    per user (Authentication.md §1)."""

    throttle_classes = [TotpManageRateThrottle]

    def post(self, request):
        serializer = TotpSetupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        secret, otpauth_uri = services.begin_totp_setup(
            user=request.user, **serializer.validated_data
        )
        return Response({"secret": secret, "otpauth_uri": otpauth_uri})


class TotpConfirmView(APIView):
    throttle_classes = [TotpManageRateThrottle]

    def post(self, request):
        serializer = TotpCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recovery_codes = services.confirm_totp_setup(user=request.user, **serializer.validated_data)
        return Response({"recovery_codes": recovery_codes})


class TotpDisableView(APIView):
    throttle_classes = [TotpManageRateThrottle]

    def post(self, request):
        serializer = TotpDisableSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.disable_totp(user=request.user, **serializer.validated_data)
        return Response(status=204)


class LogoutView(APIView):
    # Session only: a member removed mid-session must still be able to log out.
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request._request)
        return Response(status=204)


class RefreshView(APIView):
    """Uses the membership resolved by IsMerchantMember in the current tenant
    context; never the login lookup."""

    def post(self, request):
        request.session.modified = True  # re-saved with a fresh expiry
        return Response(session_body(request.team_member))


class MerchantView(APIView):
    def get_permissions(self):
        if self.request.method == "PATCH":
            return [IsOwnerOrAdmin()]
        return [IsMerchantMember()]

    def get(self, request):
        return Response(MerchantSerializer(self._merchant(request)).data)

    def patch(self, request):
        serializer = MerchantUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        merchant = services.update_merchant(self._merchant(request), **serializer.validated_data)
        return Response(MerchantSerializer(merchant).data)

    @staticmethod
    def _merchant(request):
        # The session merchant, already loaded by IsMerchantMember; never an
        # id from the request.
        return request.team_member.merchant


class TeamMemberListView(APIView):
    permission_classes = [IsOwnerOrAdmin]

    def get(self, request):
        members = services.list_team_members()
        return Response({"results": TeamMemberSerializer(members, many=True).data})

    def post(self, request):
        serializer = TeamMemberInviteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        member, invite_token = services.invite_team_member(
            actor=request.team_member, **serializer.validated_data
        )
        body = TeamMemberSerializer(member).data
        body["invite_token"] = invite_token
        return Response(body, status=201)


class TeamMemberDetailView(APIView):
    permission_classes = [IsOwnerOrAdmin]

    def patch(self, request, pk):
        serializer = TeamMemberRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        member = services.change_team_member_role(
            actor=request.team_member, member_id=pk, **serializer.validated_data
        )
        return Response(TeamMemberSerializer(member).data)

    def delete(self, request, pk):
        services.revoke_team_member(actor=request.team_member, member_id=pk)
        return Response(status=204)


class InviteAcceptRateThrottle(AnonRateThrottle):
    scope = "invite_accept"


# APIView.as_view() marks views csrf-exempt; accept-invite is public, so opt
# CSRF back in, same as LoginView. Public and pre-tenant-context, like login:
# accounts.middleware.SessionMerchantMiddleware skips this path too.
@method_decorator(csrf_protect, name="dispatch")
class AcceptInviteView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_throttles(self):
        return [InviteAcceptRateThrottle()]

    def post(self, request):
        serializer = AcceptInviteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.accept_invite(**serializer.validated_data)
        return Response(status=204)
