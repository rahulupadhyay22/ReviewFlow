"""
Tenant context (Multi-Tenancy.md §Isolation Layers).

tenant_context() sets the current merchant for a request or task;
tenant_atomic() runs one database transaction with the transaction-local
SET LOCAL app.current_merchant_id that the RLS policies read.
"""
import functools
import uuid
from collections.abc import Iterator
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


_current_lookup_user_id = ContextVar("current_lookup_user_id", default=None)


def get_current_lookup_user_id() -> uuid.UUID | None:
    return _current_lookup_user_id.get()


@contextmanager
def user_lookup_atomic(user_id: uuid.UUID | str) -> Iterator[uuid.UUID]:
    """Login membership lookup only: one transaction with
    SET LOCAL app.current_user_id, read by the SELECT-only self_membership
    policy on accounts_teammember.

    Refuses to run inside a merchant context: the tenant_isolation and
    self_membership policies are PERMISSIVE, so PostgreSQL ORs them, and
    nesting (a savepoint) would expose the user's memberships in other
    merchants to that merchant's transaction.

    It must also be its own outermost transaction: durable=True makes Django
    raise RuntimeError if it is opened inside any other atomic block (Django's
    own test-case transactions excepted), so the SET LOCAL can never outlive
    this block.
    """
    if _current_merchant_id.get() is not None:
        raise TenantContextError("user_lookup_atomic() must not run inside a tenant context.")
    if not isinstance(user_id, uuid.UUID):
        user_id = uuid.UUID(str(user_id))
    token = _current_lookup_user_id.set(user_id)
    try:
        with transaction.atomic(durable=True):
            with connection.cursor() as cursor:
                # Transaction-local; canonical UUID str keeps the literal injection-safe.
                cursor.execute(f"SET LOCAL app.current_user_id = '{user_id}'")
            yield user_id
    finally:
        _current_lookup_user_id.reset(token)


def tenant_task(fn):
    """Celery entry point: the task's first argument is always merchant_id.

    Apply under @shared_task. The body uses tenant_atomic() per transaction.
    """

    @functools.wraps(fn)
    def wrapper(merchant_id, *args, **kwargs):
        with tenant_context(merchant_id):
            return fn(merchant_id, *args, **kwargs)

    return wrapper
