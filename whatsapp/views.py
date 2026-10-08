"""Thin HTTP layer for the dashboard endpoints (spec 08): every rule lives in
whatsapp.services. Meta's webhook endpoint is in whatsapp/receivers.py."""
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from accounts.permissions import IsOwnerAdminOrManager, IsOwnerOrAdmin
from core.pagination import CursorPagination
from locations.services import get_accessible_location
from whatsapp import services
from whatsapp.serializers import (
    SenderSerializer,
    SetSenderSerializer,
    TemplateCreateSerializer,
    TemplateSerializer,
)


class TemplateWriteRateThrottle(UserRateThrottle):
    scope = "whatsapp_template_write"


class SendersView(APIView):
    def get(self, request):
        paginator = CursorPagination()
        page = paginator.paginate_queryset(services.list_senders(), request, view=self)
        return paginator.get_paginated_response(SenderSerializer(page, many=True).data)


class LocationSenderView(APIView):
    def get_permissions(self):
        if self.request.method in ("PUT", "DELETE"):
            return [IsOwnerAdminOrManager()]
        return super().get_permissions()  # default: IsMerchantMember

    def _body(self, location):
        account = services.get_location_sender(location)
        return Response({"sender": SenderSerializer(account).data if account else None})

    def get(self, request, pk):
        return self._body(get_accessible_location(request.team_member, pk))

    def put(self, request, pk):
        location = get_accessible_location(request.team_member, pk)
        serializer = SetSenderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = services.get_sender(serializer.validated_data["whatsapp_account_id"])
        services.set_location_sender(location=location, account=account, actor=request.user)
        return self._body(location)

    def delete(self, request, pk):
        location = get_accessible_location(request.team_member, pk)
        services.clear_location_sender(location=location, actor=request.user)
        return Response(status=204)


class TemplatesView(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [IsOwnerOrAdmin()]
        return super().get_permissions()  # default: IsMerchantMember

    def get_throttles(self):
        # Per-user limit on template creation (Security-Controls.md, Rate
        # Limiting): every template is submitted to the one shared WABA.
        if self.request.method == "POST":
            return [TemplateWriteRateThrottle()]
        return super().get_throttles()

    def get(self, request):
        templates = services.list_templates(request.query_params.get("status"))
        paginator = CursorPagination()
        page = paginator.paginate_queryset(templates, request, view=self)
        return paginator.get_paginated_response(TemplateSerializer(page, many=True).data)

    def post(self, request):
        serializer = TemplateCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        template = services.create_template(**serializer.validated_data)
        return Response(TemplateSerializer(template).data, status=201)
