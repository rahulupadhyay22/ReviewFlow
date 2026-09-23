"""Transactions views (Coding-Standards.md §1: thin, no business logic).

Default permission is IsMerchantMember (any role) -- MANAGER scoping comes
entirely from locations.services.accessible_locations, reused inside
transactions.services, not re-implemented here (API-Specification.md
§Transactions)."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

from rest_framework.response import Response
from rest_framework.views import APIView

from core.pagination import CursorPagination
from transactions import services
from transactions.serializers import TransactionFilterSerializer, TransactionSerializer

UTC = ZoneInfo("UTC")


class TransactionCursorPagination(CursorPagination):
    ordering = "-created_at"


class TransactionListView(APIView):
    def get(self, request):
        filters = TransactionFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        data = filters.validated_data

        date_from = data.get("date_from")
        date_to = data.get("date_to")
        transactions = services.list_transactions(
            request.team_member,
            location_id=data.get("location_id"),
            date_from=datetime.combine(date_from, time.min, tzinfo=UTC) if date_from else None,
            date_to=datetime.combine(date_to, time.max, tzinfo=UTC) if date_to else None,
            status=data.get("status"),
        )
        paginator = TransactionCursorPagination()
        page = paginator.paginate_queryset(transactions, request, view=self)
        return paginator.get_paginated_response(TransactionSerializer(page, many=True).data)


class TransactionDetailView(APIView):
    def get(self, request, pk):
        txn = services.get_transaction(request.team_member, pk)
        return Response(TransactionSerializer(txn).data)
