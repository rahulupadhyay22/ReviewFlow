"""Unit tests for core.pagination.CursorPagination (spec §Services:
page_size=25, page_size_query_param="limit", max_page_size=100,
ordering="created_at", {results, next_cursor} shape)."""
import uuid

import pytest
from django.db import connection, models
from django.test.utils import isolate_apps
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from core.pagination import CursorPagination


@pytest.fixture
def widget_model():
    """A throwaway global (non-tenant) model with the created_at field the
    shared CursorPagination orders by. Declared inside the fixture so it
    never registers in the real app registry (see core/tests/conftest.py's
    tenant_models fixture for the same pattern)."""
    with isolate_apps("core"):

        class Widget(models.Model):
            id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
            created_at = models.DateTimeField(auto_now_add=True)

            class Meta:
                app_label = "core"

        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(Widget)

        yield Widget


@pytest.mark.django_db
def test_default_page_size_is_25(widget_model):
    for _ in range(30):
        widget_model.objects.create()
    request = Request(APIRequestFactory().get("/widgets"))

    paginator = CursorPagination()
    page = paginator.paginate_queryset(widget_model.objects.order_by("created_at"), request)

    assert len(page) == 25


@pytest.mark.django_db
def test_limit_query_param_over_max_is_clamped_to_100(widget_model):
    for _ in range(150):
        widget_model.objects.create()
    request = Request(APIRequestFactory().get("/widgets", {"limit": 500}))

    paginator = CursorPagination()
    page = paginator.paginate_queryset(widget_model.objects.order_by("created_at"), request)

    assert len(page) == 100


@pytest.mark.django_db
def test_get_paginated_response_shape_has_results_and_next_cursor(widget_model):
    for _ in range(30):
        widget_model.objects.create()
    request = Request(APIRequestFactory().get("/widgets"))

    paginator = CursorPagination()
    page = paginator.paginate_queryset(widget_model.objects.order_by("created_at"), request)
    response = paginator.get_paginated_response([str(obj.pk) for obj in page])

    assert set(response.data) == {"results", "next_cursor"}
    assert response.data["next_cursor"] is not None


@pytest.mark.django_db
def test_next_cursor_is_null_on_last_page(widget_model):
    for _ in range(5):
        widget_model.objects.create()
    request = Request(APIRequestFactory().get("/widgets"))

    paginator = CursorPagination()
    page = paginator.paginate_queryset(widget_model.objects.order_by("created_at"), request)
    response = paginator.get_paginated_response([str(obj.pk) for obj in page])

    assert len(page) == 5
    assert response.data["next_cursor"] is None
