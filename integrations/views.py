"""Integrations views (Coding-Standards.md §1: thin, no business logic).

Every endpoint here is OWNER/ADMIN only (spec Decision 10) -- integrations
are merchant-level settings, like /team-members."""
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerOrAdmin
from core.pagination import CursorPagination
from integrations import services
from integrations.serializers import (
    AddMappingSerializer,
    ConnectSerializer,
    IntegrationConfigSerializer,
    IntegrationSerializer,
    MappingSerializer,
    ReplaceMappingsSerializer,
)


class IntegrationConnectView(APIView):
    permission_classes = [IsOwnerOrAdmin]

    def post(self, request, provider):
        serializer = ConnectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        integration = services.connect_integration(
            actor=request.team_member, provider=provider, **serializer.validated_data
        )
        return Response(IntegrationSerializer(integration).data, status=201)


class IntegrationListView(APIView):
    permission_classes = [IsOwnerOrAdmin]

    def get(self, request):
        integrations = services.list_integrations()
        paginator = CursorPagination()
        page = paginator.paginate_queryset(integrations, request, view=self)
        return paginator.get_paginated_response(IntegrationSerializer(page, many=True).data)


class IntegrationDetailView(APIView):
    permission_classes = [IsOwnerOrAdmin]

    def patch(self, request, pk):
        integration = services.get_integration(pk)
        serializer = IntegrationConfigSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        integration = services.update_integration_config(integration, **serializer.validated_data)
        return Response(IntegrationSerializer(integration).data)

    def delete(self, request, pk):
        integration = services.get_integration(pk)
        services.disconnect_integration(actor=request.team_member, integration=integration)
        return Response(status=204)


class IntegrationLocationsView(APIView):
    permission_classes = [IsOwnerOrAdmin]

    def post(self, request, pk):
        integration = services.get_integration(pk)
        serializer = AddMappingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mapping = services.add_location_mapping(integration, **serializer.validated_data)
        return Response(MappingSerializer(mapping).data, status=201)

    def put(self, request, pk):
        integration = services.get_integration(pk)
        serializer = ReplaceMappingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        integration = services.replace_location_mappings(
            integration, mappings=serializer.validated_data["mappings"]
        )
        return Response(IntegrationSerializer(integration).data)
