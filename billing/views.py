"""Billing views (Coding-Standards.md §1: thin, no business logic).

Session-only (the default DRF SessionAuthentication): nothing here opts into
API keys, so a key can never read or change billing. OWNER has full billing
access, ADMIN reads only, MANAGER and VIEWER none. The merchant always comes
from the session's tenant context; the only request input to a write is
`plan_id`.
"""
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from accounts.models import TeamMember
from accounts.permissions import IsOwner, IsOwnerOrAdmin
from billing import services
from billing.serializers import CheckoutRequestSerializer, PlanSerializer, subscription_body
from core.pagination import CursorPagination


class BillingWriteRateThrottle(UserRateThrottle):
    scope = "billing_write"


class PlanCursorPagination(CursorPagination):
    ordering = ("monthly_price", "id")


class PlanListView(APIView):
    permission_classes = [IsOwnerOrAdmin]

    def get(self, request):
        paginator = PlanCursorPagination()
        page = paginator.paginate_queryset(services.list_plans(), request, view=self)
        return paginator.get_paginated_response(PlanSerializer(page, many=True).data)


class SubscriptionView(APIView):
    permission_classes = [IsOwnerOrAdmin]

    def get(self, request):
        overview = services.get_subscription_overview(role=request.team_member.role)
        return Response(subscription_body(overview))


class CheckoutView(APIView):
    permission_classes = [IsOwner]
    throttle_classes = [BillingWriteRateThrottle]

    def post(self, request):
        serializer = CheckoutRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.start_checkout(
            actor=request.team_member, plan_id=serializer.validated_data["plan_id"]
        )
        overview = services.get_subscription_overview(role=TeamMember.Role.OWNER)
        body = {"checkout": result.checkout, "subscription": subscription_body(overview)}
        return Response(body, status=201 if result.created else 200)


class SubscriptionCancelView(APIView):
    permission_classes = [IsOwner]
    throttle_classes = [BillingWriteRateThrottle]

    def post(self, request):
        # The body is ignored: nothing the client sends reaches the service.
        services.cancel_subscription(actor=request.team_member)
        overview = services.get_subscription_overview(role=TeamMember.Role.OWNER)
        return Response(subscription_body(overview))
