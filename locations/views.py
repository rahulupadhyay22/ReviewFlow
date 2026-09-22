from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerAdminOrManager, IsOwnerOrAdmin
from core.pagination import CursorPagination
from locations import services
from locations.serializers import LocationCreateSerializer, LocationSerializer, LocationUpdateSerializer


class LocationListView(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [IsOwnerOrAdmin()]
        return super().get_permissions()  # default: IsMerchantMember

    def get(self, request):
        locations = services.accessible_locations(request.team_member).order_by("created_at")
        paginator = CursorPagination()
        page = paginator.paginate_queryset(locations, request, view=self)
        return paginator.get_paginated_response(LocationSerializer(page, many=True).data)

    def post(self, request):
        serializer = LocationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        location = services.create_location(**serializer.validated_data)
        return Response(LocationSerializer(location).data, status=201)


class LocationDetailView(APIView):
    def get_permissions(self):
        if self.request.method == "PATCH":
            return [IsOwnerAdminOrManager()]
        if self.request.method == "DELETE":
            return [IsOwnerOrAdmin()]
        return super().get_permissions()  # default: IsMerchantMember

    def get(self, request, pk):
        location = services.get_accessible_location(request.team_member, pk)
        return Response(LocationSerializer(location).data)

    def patch(self, request, pk):
        location = services.get_accessible_location(request.team_member, pk)
        serializer = LocationUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        location = services.update_location(location, **serializer.validated_data)
        return Response(LocationSerializer(location).data)

    def delete(self, request, pk):
        location = services.get_accessible_location(request.team_member, pk)
        services.deactivate_location(location)
        return Response(status=204)
