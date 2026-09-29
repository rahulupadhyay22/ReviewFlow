"""Integrations views (Coding-Standards.md §1: thin, no business logic).

Every /integrations endpoint is OWNER/ADMIN only (spec Decision 10) --
integrations are merchant-level settings, like /team-members. /sales (spec
06 Decisions 8, 13, 14) is a Generic REST API endpoint: API-key only, no
session fallback."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from django.conf import settings
from django.http import HttpResponseRedirect
from rest_framework.permissions import AllowAny

from accounts.permissions import IsOwnerOrAdmin
from apikeys.authentication import ApiKeyOptInMixin
from apikeys.permissions import HasApiKeyScope
from core.pagination import CursorPagination
from integrations import services
from integrations.serializers import (
    AddMappingSerializer,
    ConnectSerializer,
    CsvImportSerializer,
    IntegrationConfigSerializer,
    IntegrationSerializer,
    MappingSerializer,
    ReplaceMappingsSerializer,
)
from integrations.shopify import services as shopify_services
from integrations.throttling import WebhookIpRateThrottle
from transactions import services as transaction_services
from transactions.serializers import TransactionFilterSerializer, TransactionSerializer
from transactions.views import TransactionCursorPagination

UTC = ZoneInfo("UTC")


class IntegrationConnectView(APIView):
    permission_classes = [IsOwnerOrAdmin]

    def post(self, request, provider):
        # [User decision 2026-09-29, O2] checked before the body is even
        # deserialized: a malformed body for provider=shopify still gets
        # this 400, never the serializer's 422.
        services.assert_generic_connect_allowed(provider)
        serializer = ConnectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        integration, issued_credentials = services.connect_integration(
            actor=request.team_member, provider=provider, **serializer.validated_data
        )
        body = IntegrationSerializer(integration).data
        if issued_credentials:
            # Shown exactly once, in this response only (spec 06 Decision 5)
            # -- never returned again, e.g. by GET /integrations.
            body.update(issued_credentials)
        return Response(body, status=201)


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


class CsvImportView(APIView):
    permission_classes = [IsOwnerOrAdmin]
    parser_classes = [MultiPartParser]

    def post(self, request, pk):
        integration = services.get_integration(pk)
        serializer = CsvImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rows = services.start_csv_import(integration=integration, file=serializer.validated_data["file"])
        return Response({"rows": rows}, status=202)


class SalesView(ApiKeyOptInMixin, APIView):
    """POST /sales, GET /sales (spec 06 Decisions 8, 13, 14). API-key only:
    the only permission is HasApiKeyScope, with no session/member fallback
    -- a request without a Bearer header on an opted-in method is judged by
    the view's default authenticators (session), which then fails this
    permission check (request.auth is never an ApiKey), giving 403."""

    api_key_methods = frozenset({"GET", "POST"})

    def get_permissions(self):
        scope = "sales:write" if self.request.method == "POST" else "transactions:read"
        return [HasApiKeyScope(scope)()]

    def post(self, request):
        event, txn, created = services.ingest_api_sale(payload=request.data)
        return _sales_response(event, txn, created)

    def get(self, request):
        filters = TransactionFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        data = filters.validated_data

        date_from = data.get("date_from")
        date_to = data.get("date_to")
        sales = transaction_services.list_sales(
            location_id=data.get("location_id"),
            date_from=datetime.combine(date_from, time.min, tzinfo=UTC) if date_from else None,
            date_to=datetime.combine(date_to, time.max, tzinfo=UTC) if date_to else None,
            status=data.get("status"),
        )
        paginator = TransactionCursorPagination()
        page = paginator.paginate_queryset(sales, request, view=self)
        return paginator.get_paginated_response(TransactionSerializer(page, many=True).data)


class ShopifyInstallView(APIView):
    """GET /integrations/shopify/install: the App URL (spec 06-shopify-app
    Decision 3, merchant flow step 1). Unauthenticated, pre-tenant, and
    per-IP throttled -- there is no merchant, no login and no tenant data
    on this path. No endpoint here accepts a merchant-entered shop
    domain: `shop` comes only from Shopify's own redirect."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [WebhookIpRateThrottle]

    def get(self, request):
        shop = request.GET.get("shop", "")
        authorize_url = shopify_services.begin_shopify_install(session=request.session, shop=shop)
        return HttpResponseRedirect(authorize_url)


class ShopifyCallbackView(APIView):
    """GET /integrations/shopify/callback: Shopify's OAuth redirect (spec
    Decision 3, merchant flow step 3). Never reads request.merchant_id or
    any callback parameter to decide a merchant, and creates NO
    Integration."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [WebhookIpRateThrottle]

    def get(self, request):
        shopify_services.complete_shopify_oauth(session=request.session, query=request.GET.dict())
        # [User decision 2026-09-29, spec amendment] The exact configured
        # link-page URL, server-side only -- no query string or fragment
        # appended, so no Shopify parameter, code, hmac, state, token or
        # pending-installation data ever reaches the browser-visible
        # Location header.
        return HttpResponseRedirect(settings.SHOPIFY_LINK_PAGE_URL)


class ShopifyPendingView(APIView):
    """GET /integrations/shopify/pending: session, OWNER/ADMIN (spec
    Decision 3, merchant flow step 4)."""

    permission_classes = [IsOwnerOrAdmin]

    def get(self, request):
        shop = shopify_services.pending_shopify_shop(session=request.session)
        if shop is None:
            return Response(status=404)
        return Response({"shop": shop})


class ShopifyLinkView(APIView):
    """POST /integrations/shopify/link: session + CSRF, OWNER/ADMIN, empty
    body (spec Decision 3, merchant flow step 4). Any shop, merchant_id or
    credentials in the body is ignored -- the merchant always comes from
    the authenticated session, never from the request."""

    permission_classes = [IsOwnerOrAdmin]

    def post(self, request):
        integration = shopify_services.link_shopify_installation(
            actor=request.team_member, session=request.session
        )
        return Response(IntegrationSerializer(integration).data, status=201)


def _sales_response(event, txn, created):
    """The response mapping is purely event.status and created (spec 06
    Decision 8's table) -- no other status logic lives here or in
    services.ingest_api_sale()."""
    from events.models import IntegrationEvent

    if event.status == IntegrationEvent.Status.PROCESSED:
        body = {"transaction_id": str(txn.id) if txn else None, "event_id": str(event.id)}
        return Response(body, status=201 if created else 200)

    if event.status in (IntegrationEvent.Status.FAILED, IntegrationEvent.Status.RECEIVED):
        return Response(
            {"event_id": str(event.id), "event_status": event.status, "transaction_id": None}, status=202
        )

    # DEAD_LETTER or CANCELLED -- terminal, will never produce a Transaction
    # (spec Decision 8). Built directly here rather than through
    # core.api.exception_handler, which renders only {code, message}.
    return Response(
        {
            "error": {"code": "sale_not_processed", "message": "The event will not be processed."},
            "event_id": str(event.id),
            "event_status": event.status,
        },
        status=409,
    )
