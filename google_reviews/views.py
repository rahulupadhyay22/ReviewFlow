"""Thin HTTP layer (spec 09-google-business-profile): every rule lives in
google_reviews.services.

Two views, GoogleConnectionLocationsView (GET) and LocationGoogleLocationView
(PUT), are exempt from the request-wide tenant transaction (core.middleware,
decision P-1), because they make read-only Google calls and no provider call
may run inside a database transaction. They therefore open their own short
tenant_atomic() around authentication and the permission check (initial()), and
their services open one around every other database access.
"""
from datetime import UTC, datetime, time

from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsMerchantMember, IsOwnerAdminOrManager, IsOwnerOrAdmin
from apikeys.authentication import ApiKeyOptInMixin
from apikeys.models import ApiKey
from apikeys.permissions import HasApiKeyScope
from core.pagination import CursorPagination
from core.tenancy import tenant_atomic
from google_reviews import services
from google_reviews.serializers import (
    ConnectionSerializer,
    GoogleLocationSerializer,
    MapLocationSerializer,
    OAuthAttemptSerializer,
    OAuthCompleteSerializer,
    ReviewFilterSerializer,
    ReviewSerializer,
)
from locations.services import get_accessible_location


class TenantInitialMixin:
    """For a view exempt from the request-wide transaction: authentication (the
    session user and CSRF), the permission check (the membership read) and
    throttles all run inside one short committed tenant transaction, before the
    view body. Without it the membership read would find nothing under RLS and
    every request would be denied. No Google call happens here."""

    def initial(self, request, *args, **kwargs):
        with tenant_atomic():
            super().initial(request, *args, **kwargs)


class OAuthStartView(APIView):
    """POST /google/oauth/start. OWNER and ADMIN only. Returns the
    authorization URL; 503 while the feature is off or unset. No Google call."""

    permission_classes = [IsOwnerOrAdmin]

    def post(self, request):
        return Response(services.begin_google_oauth(actor=request.user))


class OAuthCompleteView(APIView):
    """POST /google/oauth/complete. Claims the attempt and enqueues the
    exchange. Makes no Google call; the outcome is read from the attempt."""

    permission_classes = [IsOwnerOrAdmin]

    def post(self, request):
        body = OAuthCompleteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        attempt = services.complete_google_oauth(
            actor=request.user, code=body.validated_data["code"], state=body.validated_data["state"]
        )
        return Response({"attempt_id": str(attempt.pk), "status": attempt.status}, status=202)


class OAuthAttemptView(APIView):
    """GET /google/oauth/attempts/{id}: only the user who started it."""

    permission_classes = [IsOwnerOrAdmin]

    def get(self, request, pk):
        attempt = services.get_oauth_attempt(actor=request.user, attempt_id=pk)
        return Response(OAuthAttemptSerializer(attempt).data)


class ConnectionsView(APIView):
    """GET /google/connections: any role (it feeds the NEEDS_REAUTH banner)."""

    def get(self, request):
        paginator = CursorPagination()
        page = paginator.paginate_queryset(services.list_connections(), request, view=self)
        return paginator.get_paginated_response(ConnectionSerializer(page, many=True).data)


class GoogleConnectionLocationsView(TenantInitialMixin, APIView):
    """GET /google/connections/{id}/locations. OWNER and ADMIN only. Read-only
    Google calls; 409 google_token_refresh_pending when a refresh is needed."""

    permission_classes = [IsOwnerOrAdmin]

    def get(self, request, pk):
        return Response({"results": services.list_available_locations(pk)})


class LocationGoogleLocationView(TenantInitialMixin, APIView):
    """GET /locations/{id}/google-location (any role; a MANAGER gets 404 for an
    unassigned location) and PUT (OWNER, ADMIN, or an assigned MANAGER)."""

    def get_permissions(self):
        if self.request.method == "PUT":
            return [IsOwnerAdminOrManager()]
        return [IsMerchantMember()]

    def get(self, request, pk):
        location = get_accessible_location(request.team_member, pk)
        mapping = services.get_location_mapping(location)
        return Response({"google_location": GoogleLocationSerializer(mapping).data if mapping else None})

    def put(self, request, pk):
        body = MapLocationSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        mapping = services.map_location(
            team_member=request.team_member,
            location_id=pk,
            connection_id=body.validated_data["google_connection_id"],
            google_location_id=body.validated_data["google_location_id"],
        )
        return Response(GoogleLocationSerializer(mapping).data)


class ReviewCursorPagination(CursorPagination):
    ordering = ("-review_created_at", "-id")


class ReviewsView(ApiKeyOptInMixin, APIView):
    """GET /reviews. A session request is judged by the role gate and the
    MANAGER location scope; a Bearer request is judged by the key alone and
    must hold reviews:read (merchant-wide, like GET /sales). Never an
    attribution field, never reply_text / reply_status."""

    api_key_methods = frozenset({"GET"})

    def get_permissions(self):
        if isinstance(self.request.auth, ApiKey):
            return [HasApiKeyScope("reviews:read")()]
        return [IsMerchantMember()]

    def get(self, request):
        filters = ReviewFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        data = filters.validated_data
        date_from, date_to = data.get("date_from"), data.get("date_to")
        reviews = services.list_reviews(
            team_member=getattr(request, "team_member", None),
            location_id=data.get("location_id"),
            date_from=datetime.combine(date_from, time.min, tzinfo=UTC) if date_from else None,
            date_to=datetime.combine(date_to, time.max, tzinfo=UTC) if date_to else None,
            rating_min=data.get("rating_min"),
        )
        paginator = ReviewCursorPagination()
        page = paginator.paginate_queryset(reviews, request, view=self)
        return paginator.get_paginated_response(ReviewSerializer(page, many=True).data)
