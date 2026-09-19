"""
Tenant context (Multi-Tenancy.md §Isolation Layers).

tenant_context() sets the current merchant for a request or task;
tenant_atomic() runs one database transaction with the transaction-local
SET LOCAL app.current_merchant_id that the RLS policies read.
"""
import functools
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import connection, transaction

from core.exceptions import TenantContextError

_current_merchant_id = ContextVar("current_merchant_id", default=None)


def get_current_merchant_id():
    return _current_merchant_id.get()


@contextmanager
def tenant_context(merchant_id):
    # uuid.UUID() raises ValueError for None or anything that isn't a UUID.
    if not isinstance(merchant_id, uuid.UUID):
        merchant_id = uuid.UUID(str(merchant_id))
    active = _current_merchant_id.get()
    if active is not None and active != merchant_id:
        raise TenantContextError("A different merchant's tenant context is already active.")
    token = _current_merchant_id.set(merchant_id)
    try:
        yield merchant_id
    finally:
        _current_merchant_id.reset(token)


@contextmanager
def tenant_atomic():
    """One tenant DB transaction with SET LOCAL app.current_merchant_id.

    Must be the outermost transaction of a tenant context: inside an unrelated
    transaction.atomic() it becomes a savepoint, and SET LOCAL then outlives
    this block until that outer transaction ends.
    """
    merchant_id = _current_merchant_id.get()
    if merchant_id is None:
        raise TenantContextError("tenant_atomic() requires an active tenant_context().")
    with transaction.atomic():
        with connection.cursor() as cursor:
            # Transaction-local, never session-level (FINAL-ARCHITECTURE-REVIEW.md §8).
            # str() of a uuid.UUID is canonical hex, so the literal is injection-safe.
            cursor.execute(f"SET LOCAL app.current_merchant_id = '{merchant_id}'")
        yield


def tenant_task(fn):
    """Celery entry point: the task's first argument is always merchant_id.

    Apply under @shared_task. The body uses tenant_atomic() per transaction.
    """

    @functools.wraps(fn)
    def wrapper(merchant_id, *args, **kwargs):
        with tenant_context(merchant_id):
            return fn(merchant_id, *args, **kwargs)

    return wrapper
