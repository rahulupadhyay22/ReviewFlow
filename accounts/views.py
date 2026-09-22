from django.contrib.auth import login, logout
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from accounts import services
from accounts.permissions import IsMerchantMember, IsOwnerOrAdmin
from accounts.serializers import (
    AcceptInviteSerializer,
    LoginSerializer,
    MerchantSerializer,
    MerchantUpdateSerializer,
    TeamMemberInviteSerializer,
    TeamMemberRoleSerializer,
    TeamMemberSerializer,
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
        member = services.authenticate_login(request=request._request, **serializer.validated_data)
        login(request._request, member.user)  # cycles the session key
        request.session["merchant_id"] = str(member.merchant_id)
        return Response(session_body(member))


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
