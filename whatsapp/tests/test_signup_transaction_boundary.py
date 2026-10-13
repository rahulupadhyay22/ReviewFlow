"""The transaction boundary that ES-4 needs (spec
08-whatsapp-embedded-signup D-1 Option A, P6a): inside the one exempt view the
service's own short blocks commit mid-request, so an external call can run
with nothing open and the pending row already committed; every other request
still holds one request-wide transaction.

The real completion (P6b) is blocked on evidence. These tests exercise the
real middleware and the real ES-18 state primitives with a stand-in "Meta
call", and assert the properties the completion depends on.

transaction=True: a second connection can only see committed data."""
import threading

import pytest
from django.db import connection, transaction
from django.test import RequestFactory, override_settings
from django.utils import timezone

from core.middleware import TenantMiddleware
from core.tenancy import get_current_merchant_id, tenant_atomic, tenant_context
from whatsapp import services
from whatsapp.models import EmbeddedSignupState

pytestmark = pytest.mark.django_db(transaction=True)

URLCONF = "core.tests.urls_middleware"
EXEMPT = "/signup/complete"
ORDINARY = "/signup/other"


def rows_seen_by_another_connection(merchant_id):
    """Count the merchant's state rows from a brand-new connection (so only
    committed data is visible), then close it."""
    box = {}

    def work():
        try:
            with tenant_context(merchant_id), tenant_atomic():
                box["count"] = EmbeddedSignupState.objects.count()
        except BaseException as exc:  # surfaced below
            box["error"] = exc
        finally:
            connection.close()

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(timeout=30)
    assert "error" not in box, box.get("error")
    return box["count"]


def run(path, merchant_id, handler):
    request = RequestFactory().post(path)
    request.merchant_id = merchant_id
    return TenantMiddleware(handler)(request)


@pytest.fixture
def owner(make_merchant):
    return make_merchant("A")


@override_settings(ROOT_URLCONF=URLCONF)
def test_in_the_exempt_view_a_service_block_commits_before_the_request_ends(owner):
    seen = {}

    def handler(request):
        services.issue_signup_state(actor=owner.user)
        seen["mid_request"] = rows_seen_by_another_connection(owner.merchant_id)
        seen["in_atomic_block"] = connection.in_atomic_block
        seen["merchant"] = get_current_merchant_id()
        return "response"

    run(EXEMPT, owner.merchant_id, handler)

    assert seen["mid_request"] == 1  # already committed, still inside the request
    assert seen["in_atomic_block"] is False
    assert seen["merchant"] == owner.merchant_id  # the tenant context is kept


@override_settings(ROOT_URLCONF=URLCONF)
def test_in_any_other_request_the_same_write_stays_uncommitted_until_the_end(owner):
    seen = {}

    def handler(request):
        services.issue_signup_state(actor=owner.user)
        seen["mid_request"] = rows_seen_by_another_connection(owner.merchant_id)
        seen["in_atomic_block"] = connection.in_atomic_block
        return "response"

    run(ORDINARY, owner.merchant_id, handler)

    assert seen["mid_request"] == 0  # the request-wide transaction holds it
    assert seen["in_atomic_block"] is True
    assert rows_seen_by_another_connection(owner.merchant_id) == 1  # committed at the end


@override_settings(ROOT_URLCONF=URLCONF)
def test_state_is_consumed_and_committed_before_the_external_call(owner):
    """The ES-4 order: consume (own short block, committed), then the Meta
    call with no transaction open and the consumption already durable."""
    with tenant_context(owner.merchant_id):
        state = services.issue_signup_state(actor=owner.user)
    observed = {}

    def meta_call():
        observed["in_atomic_block"] = connection.in_atomic_block
        observed["visible_state_rows"] = rows_seen_by_another_connection(owner.merchant_id)

    def handler(request):
        assert services.consume_signup_state(actor=owner.user, state=state) is True
        meta_call()
        return "response"

    run(EXEMPT, owner.merchant_id, handler)

    assert observed == {"in_atomic_block": False, "visible_state_rows": 0}


@override_settings(ROOT_URLCONF=URLCONF)
def test_a_failure_after_consumption_never_restores_the_state(owner):
    with tenant_context(owner.merchant_id):
        state = services.issue_signup_state(actor=owner.user)

    def handler(request):
        assert services.consume_signup_state(actor=owner.user, state=state) is True
        raise RuntimeError("a Meta call failed after consumption")

    with pytest.raises(RuntimeError):
        run(EXEMPT, owner.merchant_id, handler)

    # Committed consumption survives the failure; the same state cannot be reused.
    with tenant_context(owner.merchant_id):
        assert services.consume_signup_state(actor=owner.user, state=state) is False
    assert rows_seen_by_another_connection(owner.merchant_id) == 0
    assert connection.in_atomic_block is False
    assert get_current_merchant_id() is None


@override_settings(ROOT_URLCONF=URLCONF)
def test_an_on_commit_enqueue_registered_in_the_service_block_fires_only_after_the_commit(owner):
    """The activation enqueue pattern: register it inside a short block; it
    runs when that block commits, never before, and never on rollback."""
    fired = []

    def enqueue():
        # By the time it runs, the block's write is committed and visible.
        fired.append(rows_seen_by_another_connection(owner.merchant_id))

    def handler(request):
        with tenant_atomic():
            EmbeddedSignupState.objects.create(
                merchant_id=owner.merchant_id,
                user=owner.user,
                state_hash="a" * 64,
                expires_at=timezone.now(),
            )
            transaction.on_commit(enqueue)
            assert fired == []  # not before the commit
        assert fired == [1]  # at the block's commit, row already visible
        return "response"

    run(EXEMPT, owner.merchant_id, handler)


@override_settings(ROOT_URLCONF=URLCONF)
def test_an_on_commit_enqueue_is_discarded_when_its_block_rolls_back(owner):
    fired = []

    def handler(request):
        with pytest.raises(RuntimeError):
            with tenant_atomic():
                transaction.on_commit(lambda: fired.append("boom"))
                raise RuntimeError("persistence failed")
        return "response"

    run(EXEMPT, owner.merchant_id, handler)

    assert fired == []


@override_settings(ROOT_URLCONF=URLCONF)
def test_consecutive_exempt_requests_for_different_merchants_do_not_leak_context(make_merchant):
    a, b = make_merchant("A"), make_merchant("B")
    seen = {}

    def handler_for(name, owner):
        def handler(request):
            services.issue_signup_state(actor=owner.user)
            seen[name] = (get_current_merchant_id(), rows_seen_by_another_connection(owner.merchant_id))
            return "response"

        return handler

    run(EXEMPT, a.merchant_id, handler_for("a", a))
    run(EXEMPT, b.merchant_id, handler_for("b", b))

    assert seen["a"] == (a.merchant_id, 1)
    assert seen["b"] == (b.merchant_id, 1)  # B sees only its own row
    assert get_current_merchant_id() is None
