"""
Tests for core.tenancy (contextvar + SET LOCAL), core.managers
(TenantScopedManager), and core.middleware (TenantMiddleware), per
.claude/specs/01-tenant-core.md and docs/02-architecture/Multi-Tenancy.md.
"""
import uuid

import pytest
from django.db import connection
from django.test import RequestFactory

from config import celery_app
from core.exceptions import TenantContextError
from core.middleware import TenantMiddleware
from core.tenancy import get_current_merchant_id, tenant_atomic, tenant_context, tenant_task

# ---------------------------------------------------------------------------
# tenant_context
# ---------------------------------------------------------------------------


def test_tenant_context_sets_and_resets_contextvar():
    assert get_current_merchant_id() is None
    merchant_id = uuid.uuid4()

    with tenant_context(merchant_id):
        assert get_current_merchant_id() == merchant_id

    assert get_current_merchant_id() is None


def test_tenant_context_same_merchant_nesting_is_allowed():
    merchant_id = uuid.uuid4()

    with tenant_context(merchant_id):
        with tenant_context(merchant_id):
            assert get_current_merchant_id() == merchant_id
        assert get_current_merchant_id() == merchant_id

    assert get_current_merchant_id() is None


def test_tenant_context_different_merchant_nesting_raises():
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()

    with tenant_context(merchant_a):
        with pytest.raises(TenantContextError):
            with tenant_context(merchant_b):
                pass
        # The outer context survives the failed inner attempt.
        assert get_current_merchant_id() == merchant_a


def test_tenant_context_invalid_uuid_raises_value_error():
    with pytest.raises(ValueError):
        with tenant_context("not-a-uuid"):
            pass


# ---------------------------------------------------------------------------
# tenant_atomic
# ---------------------------------------------------------------------------


def test_tenant_atomic_without_context_raises():
    with pytest.raises(TenantContextError):
        with tenant_atomic():
            pass


@pytest.mark.django_db
def test_tenant_atomic_sets_current_setting_to_merchant_uuid():
    merchant_id = uuid.uuid4()

    with tenant_context(merchant_id), tenant_atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.current_merchant_id', true)")
            value = cursor.fetchone()[0]

    assert value == str(merchant_id)


# ---------------------------------------------------------------------------
# TenantScopedManager (Multi-Tenancy.md Required Test 1)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_manager_raises_without_tenant_context(tenant_models):
    Parent, _Child = tenant_models

    with pytest.raises(TenantContextError):
        Parent.objects.all()


@pytest.mark.django_db
def test_manager_returns_only_current_merchants_rows_direct(tenant_models):
    Parent, _Child = tenant_models
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()

    with tenant_context(merchant_a), tenant_atomic():
        parent_a = Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_a)
    with tenant_context(merchant_b), tenant_atomic():
        Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_b)

    with tenant_context(merchant_a), tenant_atomic():
        visible_ids = set(Parent.objects.values_list("id", flat=True))

    assert visible_ids == {parent_a.id}


@pytest.mark.django_db
def test_manager_returns_only_current_merchants_rows_transitive(tenant_models):
    Parent, Child = tenant_models
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()

    with tenant_context(merchant_a), tenant_atomic():
        parent_a = Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_a)
        child_a = Child.objects.create(id=uuid.uuid4(), parent=parent_a)
    with tenant_context(merchant_b), tenant_atomic():
        parent_b = Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_b)
        Child.objects.create(id=uuid.uuid4(), parent=parent_b)

    with tenant_context(merchant_a), tenant_atomic():
        visible_ids = set(Child.objects.values_list("id", flat=True))

    assert visible_ids == {child_a.id}


# ---------------------------------------------------------------------------
# tenant_task (Multi-Tenancy.md Required Test 3)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_tenant_task_cannot_read_or_write_another_merchants_row(tenant_models):
    Parent, _Child = tenant_models
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()

    with tenant_context(merchant_b), tenant_atomic():
        b_parent = Parent.objects.create(id=uuid.uuid4(), merchant_id=merchant_b)

    @celery_app.task
    @tenant_task
    def _read_and_try_update(merchant_id, parent_id):
        with tenant_atomic():
            read_ids = list(Parent.objects.filter(id=parent_id).values_list("id", flat=True))
            updated_count = Parent.objects.filter(id=parent_id).update(merchant_id=merchant_id)
        return read_ids, updated_count

    # Task runs with merchant A's context even though it is handed B's row id.
    read_ids, updated_count = _read_and_try_update.delay(merchant_a, b_parent.id).get()

    assert read_ids == []
    assert updated_count == 0


def test_tenant_task_rejects_missing_merchant_id():
    @celery_app.task
    @tenant_task
    def _noop(merchant_id):
        return merchant_id

    with pytest.raises(TypeError):
        _noop.delay()


def test_tenant_task_rejects_invalid_merchant_id():
    @celery_app.task
    @tenant_task
    def _noop(merchant_id):
        return merchant_id

    with pytest.raises(ValueError):
        _noop.delay("not-a-uuid").get()


# ---------------------------------------------------------------------------
# TenantMiddleware
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_middleware_with_merchant_id_runs_view_inside_tenant_context_and_transaction():
    merchant_id = uuid.uuid4()
    captured = {}

    def get_response(request):
        captured["merchant_id"] = get_current_merchant_id()
        captured["in_atomic_block"] = connection.in_atomic_block
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.current_merchant_id', true)")
            captured["current_setting"] = cursor.fetchone()[0]
        return "view-response"

    middleware = TenantMiddleware(get_response)
    request = RequestFactory().get("/")
    request.merchant_id = merchant_id

    result = middleware(request)

    assert result == "view-response"
    assert captured["merchant_id"] == merchant_id
    assert captured["in_atomic_block"] is True
    assert captured["current_setting"] == str(merchant_id)
    # Contextvar is reset once the request has been handled.
    assert get_current_merchant_id() is None


def test_middleware_without_merchant_id_passes_through_unchanged():
    calls = []

    def get_response(request):
        calls.append(get_current_merchant_id())
        return "view-response"

    middleware = TenantMiddleware(get_response)
    request = RequestFactory().get("/")
    # No request.merchant_id set (no auth layer has run yet).

    result = middleware(request)

    assert result == "view-response"
    assert calls == [None]
