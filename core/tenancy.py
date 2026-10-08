"""
Tenant context (Multi-Tenancy.md §Isolation Layers).

tenant_context() sets the current merchant for a request or task;
tenant_atomic() runs one database transaction with the transaction-local
SET LOCAL app.current_merchant_id that the RLS policies read.
"""
import functools
import re
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


_current_lookup_key_hash = ContextVar("current_lookup_key_hash", default=None)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def get_current_lookup_key_hash() -> str | None:
    return _current_lookup_key_hash.get()


@contextmanager
def api_key_lookup_atomic(key_hash: str) -> Iterator[str]:
    """Public-API key lookup only: one transaction with
    SET LOCAL app.current_api_key_hash, read by the SELECT-only
    api_key_lookup policy on apikeys_apikey.

    Refuses to run inside a merchant context: the tenant_isolation and
    api_key_lookup policies are PERMISSIVE, so PostgreSQL ORs them, and
    nesting (a savepoint) would expose the key's row (and, transitively, its
    merchant) to an unrelated merchant's transaction.

    It must also be its own outermost transaction: durable=True makes Django
    raise RuntimeError if it is opened inside any other atomic block (Django's
    own test-case transactions excepted), so the SET LOCAL can never outlive
    this block. Mirrors user_lookup_atomic().
    """
    if _current_merchant_id.get() is not None:
        raise TenantContextError("api_key_lookup_atomic() must not run inside a tenant context.")
    if not _HEX64.match(key_hash or ""):
        raise ValueError("key_hash must be a 64-character lowercase hex sha256 digest.")
    token = _current_lookup_key_hash.set(key_hash)
    try:
        with transaction.atomic(durable=True):
            with connection.cursor() as cursor:
                # Transaction-local; the hash is validated hex above, so the
                # literal is injection-safe.
                cursor.execute(f"SET LOCAL app.current_api_key_hash = '{key_hash}'")
            yield key_hash
    finally:
        _current_lookup_key_hash.reset(token)


_current_lookup_integration_id = ContextVar("current_lookup_integration_id", default=None)


def get_current_lookup_integration_id() -> uuid.UUID | None:
    return _current_lookup_integration_id.get()


@contextmanager
def integration_lookup_atomic(integration_id: uuid.UUID | str) -> Iterator[uuid.UUID]:
    """Pre-tenant Integration lookup only (spec 06 Decision 1): one
    transaction with SET LOCAL app.current_integration_id, read by the
    SELECT-only integration_lookup policy on integrations_integration.

    Refuses to run inside a merchant context: the tenant_isolation and
    integration_lookup policies are PERMISSIVE, so PostgreSQL ORs them, and
    nesting (a savepoint) would expose the integration's row (and,
    transitively, its merchant) to an unrelated merchant's transaction.

    It must also be its own outermost transaction: durable=True makes Django
    raise RuntimeError if it is opened inside any other atomic block (Django's
    own test-case transactions excepted), so the SET LOCAL can never outlive
    this block. Mirrors api_key_lookup_atomic().
    """
    if _current_merchant_id.get() is not None:
        raise TenantContextError("integration_lookup_atomic() must not run inside a tenant context.")
    if not isinstance(integration_id, uuid.UUID):
        integration_id = uuid.UUID(str(integration_id))
    token = _current_lookup_integration_id.set(integration_id)
    try:
        with transaction.atomic(durable=True):
            with connection.cursor() as cursor:
                # Transaction-local; str() of a uuid.UUID is canonical hex,
                # so the literal is injection-safe.
                cursor.execute(f"SET LOCAL app.current_integration_id = '{integration_id}'")
            yield integration_id
    finally:
        _current_lookup_integration_id.reset(token)


_current_lookup_billing_ref = ContextVar("current_lookup_billing_ref", default=None)

_BILLING_REF = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def get_current_lookup_billing_ref() -> str | None:
    return _current_lookup_billing_ref.get()


def is_valid_billing_ref(ref) -> bool:
    """The one check for a provider subscription reference: a string matching
    ^[A-Za-z0-9_]{1,64}$ in full (fullmatch: a trailing newline is refused).
    Shared by billing_ref_lookup_atomic and by billing code that must validate
    a ref before it is stored or looked up."""
    return isinstance(ref, str) and _BILLING_REF.fullmatch(ref) is not None


@contextmanager
def billing_ref_lookup_atomic(ref: str) -> Iterator[str]:
    """Pre-tenant billing lookup only (spec 07 Change 1 -- LOCKED DECISION
    CHANGE, signed off): one transaction with
    SET LOCAL app.current_billing_ref, read by the SELECT-only
    billing_ref_lookup policy on billing_subscription.

    Refuses to run inside a merchant context: the tenant_isolation and
    billing_ref_lookup policies are PERMISSIVE, so PostgreSQL ORs them, and
    nesting (a savepoint) would expose the subscription row (and,
    transitively, its merchant) to an unrelated merchant's transaction.

    It must also be its own outermost transaction: durable=True makes Django
    raise RuntimeError if it is opened inside any other atomic block (Django's
    own test-case transactions excepted), so the SET LOCAL can never outlive
    this block. Mirrors integration_lookup_atomic().
    """
    if _current_merchant_id.get() is not None:
        raise TenantContextError("billing_ref_lookup_atomic() must not run inside a tenant context.")
    if not is_valid_billing_ref(ref):
        raise ValueError("ref must match ^[A-Za-z0-9_]{1,64}$.")
    token = _current_lookup_billing_ref.set(ref)
    try:
        with transaction.atomic(durable=True):
            with connection.cursor() as cursor:
                # Transaction-local; the ref is validated above, so the
                # literal is injection-safe.
                cursor.execute(f"SET LOCAL app.current_billing_ref = '{ref}'")
            yield ref
    finally:
        _current_lookup_billing_ref.reset(token)


_PLATFORM_WRITE_SCOPES = frozenset({"whatsapp_shared_pool"})
_current_platform_write = ContextVar("current_platform_write", default=None)


def get_current_platform_write() -> str | None:
    return _current_platform_write.get()


@contextmanager
def platform_write_atomic(scope: str) -> Iterator[str]:
    """Platform write path (spec 08 Change 1 -- LOCKED DECISION CHANGE,
    approved 2026-10-07): one transaction with SET LOCAL app.platform_write,
    read by the platform_insert / platform_update policies on
    whatsapp_whatsappaccount and the platform_insert policy on
    auditlog_auditlog.

    Management and platform-operation infrastructure ONLY: its sole caller is
    whatsapp.services.upsert_shared_pool_account (itself called only by the
    configure_shared_pool command and seed_dev). Never call it from a view,
    serializer, tenant service, task or admin.

    Refuses to run inside a merchant context (the policies are PERMISSIVE and
    therefore ORed with tenant_isolation) and is always its own outermost
    transaction (durable=True), so the SET LOCAL cannot outlive this block.
    """
    if _current_merchant_id.get() is not None:
        raise TenantContextError("platform_write_atomic() must not run inside a tenant context.")
    if scope not in _PLATFORM_WRITE_SCOPES:
        raise ValueError("Unknown platform write scope.")
    token = _current_platform_write.set(scope)
    try:
        with transaction.atomic(durable=True):
            with connection.cursor() as cursor:
                # Transaction-local; the scope is allowlisted above, so the
                # literal is injection-safe.
                cursor.execute(f"SET LOCAL app.platform_write = '{scope}'")
            yield scope
    finally:
        _current_platform_write.reset(token)


def tenant_task(fn):
    """Celery entry point: the task's first argument is always merchant_id.

    Apply under @shared_task. The body uses tenant_atomic() per transaction.
    """

    @functools.wraps(fn)
    def wrapper(merchant_id, *args, **kwargs):
        with tenant_context(merchant_id):
            return fn(merchant_id, *args, **kwargs)

    return wrapper
