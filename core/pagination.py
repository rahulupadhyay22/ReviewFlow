"""Shared cursor pagination for list endpoints (API-Specification.md
§General Conventions: cursor-based, ?cursor=/?limit=, max 100)."""
from urllib.parse import parse_qs, urlparse

from rest_framework.pagination import CursorPagination as DRFCursorPagination
from rest_framework.response import Response


class CursorPagination(DRFCursorPagination):
    page_size = 25
    page_size_query_param = "limit"
    max_page_size = 100
    ordering = "created_at"

    def get_paginated_response(self, data):
        next_link = self.get_next_link()
        next_cursor = None
        if next_link:
            next_cursor = parse_qs(urlparse(next_link).query).get(self.cursor_query_param, [None])[0]
        return Response({"results": data, "next_cursor": next_cursor})
