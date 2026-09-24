"""ApiKeys views (Coding-Standards.md §1: thin, no business logic).

Session-only (the default DRF SessionAuthentication) and OWNER/ADMIN only,
same as /integrations and /team-members -- an API key can never list,
create, or revoke keys (spec Decision 10). Nothing here inherits
ApiKeyOptInMixin, so these endpoints never become eligible for Bearer auth."""
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerOrAdmin
from apikeys import services
from apikeys.serializers import ApiKeyCreateSerializer, ApiKeySerializer
from core.pagination import CursorPagination


class ApiKeyCursorPagination(CursorPagination):
    ordering = "-created_at"


class ApiKeyListView(APIView):
    permission_classes = [IsOwnerOrAdmin]

    def get(self, request):
        keys = services.list_api_keys()
        paginator = ApiKeyCursorPagination()
        page = paginator.paginate_queryset(keys, request, view=self)
        return paginator.get_paginated_response(ApiKeySerializer(page, many=True).data)

    def post(self, request):
        serializer = ApiKeyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        key, raw = services.create_api_key(
            actor=request.team_member, scopes=serializer.validated_data["scopes"]
        )
        body = ApiKeySerializer(key).data
        body["key"] = raw
        return Response(body, status=201)


class ApiKeyDetailView(APIView):
    permission_classes = [IsOwnerOrAdmin]

    def delete(self, request, pk):
        key = services.get_api_key(pk)
        services.revoke_api_key(actor=request.team_member, api_key=key)
        return Response(status=204)
