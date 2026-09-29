"""Generic REST API: POST /sales, GET /sales
(spec .claude/specs/06-priority-integrations.md Decisions 4, 8, 13, 14).
Uses the real, production-registered ApiAdapter."""
import datetime

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from core.tenancy import tenant_atomic, tenant_context
from events.models import IntegrationEvent
from integrations import services
from integrations.core.schemas import sale_event_key
from transactions.models import Transaction

pytestmark = pytest.mark.django_db(transaction=True)

SALES = "/api/v1/sales"


def _bearer(raw):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _connect_api(owner):
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration, _ = services.connect_integration(actor=owner, provider="api")
    return integration


def _sale_body(txn_id="INV-1", **overrides):
    body = {
        "external_transaction_id": txn_id,
        "amount": "100.00",
        "currency": "INR",
        "occurred_at": "2024-01-01T10:00:00+00:00",
        "customer": {"phone": "+919999999999", "name": "Rahul"},
        "payment_method": "UPI",
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def local_cache(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def owner(make_merchant):
    return make_merchant("A")


# --- happy path -----------------------------------------------------------


def test_post_sales_with_sales_write_key_returns_201(
    owner, make_key, make_location, django_capture_on_commit_callbacks
):
    integration = _connect_api(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)

    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(SALES, _sale_body(), format="json", **_bearer(raw))
    assert resp.status_code == 201
    body = resp.json()
    assert body["transaction_id"]
    assert body["event_id"]

    with tenant_context(owner.merchant_id), tenant_atomic():
        txn = Transaction.objects.get(location=location, external_transaction_id="INV-1")
        assert str(txn.id) == body["transaction_id"]


def test_replaying_the_same_body_returns_200_with_no_new_rows(
    owner, make_key, make_location, django_capture_on_commit_callbacks
):
    integration = _connect_api(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)

    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()
    with django_capture_on_commit_callbacks(execute=True):
        first = client.post(SALES, _sale_body(), format="json", **_bearer(raw))
    with django_capture_on_commit_callbacks(execute=True):
        second = client.post(SALES, _sale_body(), format="json", **_bearer(raw))
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["transaction_id"] == first.json()["transaction_id"]

    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Transaction.objects.filter(location=location).count() == 1


def test_same_transaction_id_at_two_locations_creates_two_transactions(
    owner, make_key, make_location, django_capture_on_commit_callbacks
):
    integration = _connect_api(owner)
    loc1 = make_location(owner.merchant, name="Store 1")
    loc2 = make_location(owner.merchant, name="Store 2")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=loc1.id, config_json={"external_location_id": "s1"})
        services.add_location_mapping(integration, location_id=loc2.id, config_json={"external_location_id": "s2"})

    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()
    with django_capture_on_commit_callbacks(execute=True):
        r1 = client.post(SALES, _sale_body(external_location_id="s1"), format="json", **_bearer(raw))
    with django_capture_on_commit_callbacks(execute=True):
        r2 = client.post(SALES, _sale_body(external_location_id="s2"), format="json", **_bearer(raw))
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["transaction_id"] != r2.json()["transaction_id"]


# --- ignored body fields --------------------------------------------------


def test_body_location_id_and_merchant_id_are_ignored(
    owner, make_key, make_location, django_capture_on_commit_callbacks
):
    integration = _connect_api(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)

    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()
    body = _sale_body()
    body["location_id"] = "11111111-1111-1111-1111-111111111111"
    body["merchant_id"] = "22222222-2222-2222-2222-222222222222"
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(SALES, body, format="json", **_bearer(raw))
    assert resp.status_code == 201
    with tenant_context(owner.merchant_id), tenant_atomic():
        txn = Transaction.objects.get(external_transaction_id="INV-1")
        assert txn.location_id == location.id


# --- validation / no event on 422 -----------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"customer": {"phone": "98765"}},
        {"amount": "not-a-number"},
        {"currency": "inr"},
        {"occurred_at": "not-a-date"},
        {"external_transaction_id": ""},
    ],
)
def test_invalid_fields_return_422_and_create_no_event(owner, make_key, make_location, overrides):
    integration = _connect_api(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)

    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()
    body = _sale_body()
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(body.get(k), dict):
            body[k].update(v)
        else:
            body[k] = v
    resp = client.post(SALES, body, format="json", **_bearer(raw))
    assert resp.status_code == 422
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.count() == 0


def test_location_unresolved_returns_422(owner, make_key):
    _connect_api(owner)  # no mappings at all
    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()
    resp = client.post(SALES, _sale_body(), format="json", **_bearer(raw))
    assert resp.status_code == 422


def test_no_connected_api_integration_returns_422(owner, make_key):
    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()
    resp = client.post(SALES, _sale_body(), format="json", **_bearer(raw))
    assert resp.status_code == 422


def test_missing_or_blank_phone_returns_201_with_customerless_transaction(
    owner, make_key, make_location, django_capture_on_commit_callbacks
):
    integration = _connect_api(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)

    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()
    body = _sale_body(customer={})
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(SALES, body, format="json", **_bearer(raw))
    assert resp.status_code == 201
    with tenant_context(owner.merchant_id), tenant_atomic():
        txn = Transaction.objects.get(external_transaction_id="INV-1")
        assert txn.customer_id is None


# --- inline failure -> 202, then recovery ---------------------------------


def test_inline_processing_failure_returns_202_and_retry_recovers(
    owner, make_key, make_location, monkeypatch, django_capture_on_commit_callbacks
):
    from events import services as events_services

    integration = _connect_api(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)

    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()

    original_record_sale = events_services.record_sale

    def _boom(**kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(events_services, "record_sale", _boom)

    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(SALES, _sale_body(), format="json", **_bearer(raw))
    assert resp.status_code == 202
    body = resp.json()
    assert body["event_status"] == "FAILED"
    assert body["transaction_id"] is None

    with tenant_context(owner.merchant_id), tenant_atomic():
        event = IntegrationEvent.objects.get(id=body["event_id"])
        assert event.attempt_count == 1
        assert event.error_code == "PROCESSING_ERROR"

    monkeypatch.setattr(events_services, "record_sale", original_record_sale)
    # Force the backoff window to have passed, then let the sweep's
    # process_event() call do the recovery.
    with tenant_context(owner.merchant_id), tenant_atomic():
        IntegrationEvent.objects.filter(id=body["event_id"]).update(
            updated_at=event.updated_at - datetime.timedelta(minutes=10)
        )
        events_services.process_event(body["event_id"])

    with django_capture_on_commit_callbacks(execute=True):
        replay = client.post(SALES, _sale_body(), format="json", **_bearer(raw))
    assert replay.status_code == 200
    assert replay.json()["transaction_id"]


def test_dead_letter_and_cancelled_replay_return_409(owner, make_key, make_location):
    integration = _connect_api(owner)
    location = make_location(owner.merchant)
    external_event_id = sale_event_key(None, "INV-DEAD")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)
        IntegrationEvent.objects.create(
            merchant_id=owner.merchant_id,
            integration=integration,
            source="api",
            external_event_id=external_event_id,
            payload=_sale_body(txn_id="INV-DEAD"),
            status=IntegrationEvent.Status.DEAD_LETTER,
            attempt_count=5,
        )

    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()
    resp = client.post(SALES, _sale_body(txn_id="INV-DEAD"), format="json", **_bearer(raw))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "sale_not_processed"
    assert resp.json()["event_status"] == "DEAD_LETTER"


def test_cancelled_replay_returns_409(owner, make_key, make_location):
    """DoD "409": replaying a CANCELLED event's body also returns 409, not
    just DEAD_LETTER (the previous test)."""
    integration = _connect_api(owner)
    location = make_location(owner.merchant)
    external_event_id = sale_event_key(None, "INV-CANCELLED")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)
        IntegrationEvent.objects.create(
            merchant_id=owner.merchant_id,
            integration=integration,
            source="api",
            external_event_id=external_event_id,
            payload=_sale_body(txn_id="INV-CANCELLED"),
            status=IntegrationEvent.Status.CANCELLED,
        )

    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()
    resp = client.post(SALES, _sale_body(txn_id="INV-CANCELLED"), format="json", **_bearer(raw))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "sale_not_processed"
    assert resp.json()["event_status"] == "CANCELLED"
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Transaction.objects.filter(external_transaction_id="INV-CANCELLED").count() == 0


def test_disconnect_race_between_record_and_process_gives_cancelled_and_409(
    owner, make_key, make_location, monkeypatch, django_capture_on_commit_callbacks
):
    """DoD "409": a disconnect racing the request, simulated by setting the
    api integration to DISCONNECTED between pre-validation/record_event and
    process_event, gives CANCELLED and 409 -- exercising process_event's own
    race guard (events/services.py, spec Decision 18), not record_event's
    disconnected-integration check."""
    from events import services as events_services
    from integrations.models import Integration

    integration = _connect_api(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)

    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()

    original_record_event = events_services.record_event

    def _record_then_disconnect(**kwargs):
        result = original_record_event(**kwargs)
        # Flip the status column directly (not disconnect_integration(),
        # which would cancel the just-created RECEIVED event itself) so
        # process_event()'s own race guard is what produces CANCELLED.
        Integration.objects.filter(pk=integration.id).update(status=Integration.Status.DISCONNECTED)
        return result

    monkeypatch.setattr(events_services, "record_event", _record_then_disconnect)

    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(SALES, _sale_body(txn_id="INV-RACE"), format="json", **_bearer(raw))
    assert resp.status_code == 409
    assert resp.json()["event_status"] == "CANCELLED"
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Transaction.objects.filter(location=location, external_transaction_id="INV-RACE").count() == 0


def test_replay_of_a_pending_received_event_returns_202_without_inline_processing(
    owner, make_key, make_location
):
    """DoD "202 for a replay of a pending event": a stored api event in
    RECEIVED (created directly, as if left by an earlier request whose
    inline process_event has not run) is replayed. created=False, so
    ingest_api_sale never calls process_event -- only created=True
    processes inline."""
    integration = _connect_api(owner)
    location = make_location(owner.merchant)
    external_event_id = sale_event_key(None, "INV-PENDING")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)
        IntegrationEvent.objects.create(
            merchant_id=owner.merchant_id,
            integration=integration,
            source="api",
            external_event_id=external_event_id,
            payload=_sale_body(txn_id="INV-PENDING"),
            status=IntegrationEvent.Status.RECEIVED,
        )

    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()
    resp = client.post(SALES, _sale_body(txn_id="INV-PENDING"), format="json", **_bearer(raw))
    assert resp.status_code == 202
    body = resp.json()
    assert body["event_status"] == "RECEIVED"
    assert body["transaction_id"] is None
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Transaction.objects.filter(external_transaction_id="INV-PENDING").count() == 0


# --- no duplicate normalization --------------------------------------------


def test_normalize_is_called_exactly_twice_through_get_adapter_no_duplicate_path(
    owner, make_key, make_location, monkeypatch, django_capture_on_commit_callbacks
):
    """DoD "No duplicate normalization": ApiAdapter.normalize is wrapped by
    a spy; one successful POST /sales calls it exactly twice (pre-validation
    in ingest_api_sale, then again in process_event), both instances
    obtained through get_adapter -- there is no second, ad hoc parser."""
    from integrations.api.adapter import ApiAdapter

    integration = _connect_api(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)

    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()

    calls = []
    original_normalize = ApiAdapter.normalize

    def _spy(self, parsed):
        calls.append(parsed)
        return original_normalize(self, parsed)

    monkeypatch.setattr(ApiAdapter, "normalize", _spy)

    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(SALES, _sale_body(txn_id="INV-SPY"), format="json", **_bearer(raw))
    assert resp.status_code == 201
    assert len(calls) == 2

    with tenant_context(owner.merchant_id), tenant_atomic():
        txn = Transaction.objects.get(location=location, external_transaction_id="INV-SPY")
        assert str(txn.amount) == "100.00"
        assert txn.currency == "INR"


def test_sales_post_delegates_to_the_service_with_no_field_parsing_in_the_view(owner, make_key):
    """DoD "code-level check confirms that the /sales serializer and view
    contain no field parsing (they accept any JSON object and pass it
    through)"."""
    import inspect

    from integrations.views import SalesView

    source = inspect.getsource(SalesView.post)
    # The view must forward request.data verbatim to the service and hold
    # no parsing/validation of individual sale fields itself.
    assert "request.data" in source
    assert "ingest_api_sale" in source
    for forbidden in ("Serializer(", "parse_amount", "parse_occurred_at", "validate_currency", "Decimal("):
        assert forbidden not in source


# --- robust enqueue: broker failure never turns a stored sale into a 500 --


def test_broker_enqueue_failure_still_returns_201_and_never_logs_the_exception_message(
    owner, make_key, make_location, monkeypatch, django_capture_on_commit_callbacks, caplog
):
    """DoD "With the Celery broker enqueue forced to raise: POST /sales
    still returns 201, because the event was processed inline." (spec 06
    Decision 9). The on_commit enqueue runs AFTER the inline process_event()
    call and the response body are already built, so a broker outage must
    never surface as a 500, and the log must never carry the exception
    message (which can embed connection details)."""
    from events.tasks import process_integration_event

    def _boom(*args, **kwargs):
        raise ConnectionError("broker unreachable at redis://secret-user:secret-pass@host")

    monkeypatch.setattr(process_integration_event, "delay", _boom)

    integration = _connect_api(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)

    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()
    with caplog.at_level("WARNING"):
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(SALES, _sale_body(txn_id="INV-BROKER"), format="json", **_bearer(raw))
    assert resp.status_code == 201
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Transaction.objects.filter(external_transaction_id="INV-BROKER").count() == 1
    for record in caplog.records:
        assert "secret-user" not in record.getMessage()
        assert "secret-pass" not in record.getMessage()


# --- cross-tenant isolation (priority scenario) ----------------------------


def test_cross_tenant_sales_key_never_reads_or_writes_another_merchants_transactions(
    make_merchant, make_key, make_location, django_capture_on_commit_callbacks
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_a = _connect_api(owner_a)
    integration_b = _connect_api(owner_b)
    location_a = make_location(owner_a.merchant)
    location_b = make_location(owner_b.merchant)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        services.add_location_mapping(integration_a, location_id=location_a.id)
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        services.add_location_mapping(integration_b, location_id=location_b.id)

    key_a, raw_a = make_key(owner_a, scopes=["sales:write", "transactions:read"])
    key_b, raw_b = make_key(owner_b, scopes=["sales:write", "transactions:read"])
    client = APIClient()

    with django_capture_on_commit_callbacks(execute=True):
        resp_a = client.post(SALES, _sale_body(txn_id="A-ONLY"), format="json", **_bearer(raw_a))
    with django_capture_on_commit_callbacks(execute=True):
        resp_b = client.post(SALES, _sale_body(txn_id="B-ONLY"), format="json", **_bearer(raw_b))
    assert resp_a.status_code == 201
    assert resp_b.status_code == 201

    # A's key sees only A's transaction, however it filters.
    get_resp = client.get(SALES, **_bearer(raw_a))
    assert get_resp.status_code == 200
    ids = {r["external_transaction_id"] for r in get_resp.json()["results"]}
    assert ids == {"A-ONLY"}

    # A's key replaying B's txn id (a coincidence, not the same event) never
    # touches B's row: it creates A's own event/transaction under A only.
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        b_count_before = Transaction.objects.filter(location=location_b).count()
    with django_capture_on_commit_callbacks(execute=True):
        client.post(SALES, _sale_body(txn_id="B-ONLY"), format="json", **_bearer(raw_a))
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        assert Transaction.objects.filter(location=location_b).count() == b_count_before
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        assert Transaction.objects.filter(external_transaction_id="B-ONLY").exists()


# --- auth / scopes ----------------------------------------------------


def test_key_without_scope_gets_403(owner, make_key):
    key, raw = make_key(owner, scopes=["transactions:read"])
    client = APIClient()
    resp = client.post(SALES, _sale_body(), format="json", **_bearer(raw))
    assert resp.status_code == 403


def test_session_only_request_gets_403_on_post_and_get(owner, session_client):
    client = session_client(owner.user.email)
    assert client.post(SALES, _sale_body(), format="json").status_code == 403
    assert client.get(SALES).status_code == 403


def test_get_sales_returns_only_merchant_wide_transactions(
    owner, make_key, make_location, django_capture_on_commit_callbacks
):
    integration = _connect_api(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)

    key, raw = make_key(owner, scopes=["sales:write", "transactions:read"])
    client = APIClient()
    with django_capture_on_commit_callbacks(execute=True):
        client.post(SALES, _sale_body(), format="json", **_bearer(raw))

    resp = client.get(SALES, **_bearer(raw))
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 1


def test_get_sales_with_sales_write_only_key_gets_403(owner, make_key):
    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()
    resp = client.get(SALES, **_bearer(raw))
    assert resp.status_code == 403


def test_opt_in_registry_includes_salesview():
    from django.urls import get_resolver

    from accounts.views import MerchantView
    from integrations.views import SalesView

    def _iter(patterns):
        for p in patterns:
            if hasattr(p, "url_patterns"):
                yield from _iter(p.url_patterns)
                continue
            cls = getattr(p.callback, "cls", None)
            if cls is None:
                continue
            for m in getattr(cls, "api_key_methods", frozenset()):
                yield (cls, m)

    pairs = set(_iter(get_resolver().url_patterns))
    assert pairs == {(MerchantView, "GET"), (SalesView, "GET"), (SalesView, "POST")}


def test_low_api_key_rate_returns_429_on_the_nth_plus_one_sales_request(
    owner, make_key, make_location, monkeypatch
):
    from rest_framework.settings import api_settings as drf_api_settings

    from apikeys.throttling import ApiKeyIpRateThrottle, ApiKeyRateThrottle

    merged = {**drf_api_settings.DEFAULT_THROTTLE_RATES, "api_key": "1/min", "api_key_ip": "1000/min"}
    for cls in (ApiKeyRateThrottle, ApiKeyIpRateThrottle):
        monkeypatch.setattr(cls, "THROTTLE_RATES", merged)

    integration = _connect_api(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)

    key, raw = make_key(owner, scopes=["sales:write"])
    client = APIClient()
    assert client.post(SALES, _sale_body(txn_id="RATE-1"), format="json", **_bearer(raw)).status_code == 201
    resp = client.post(SALES, _sale_body(txn_id="RATE-2"), format="json", **_bearer(raw))
    assert resp.status_code == 429


def test_event_status_values_are_exactly_the_phase_04_set():
    """No migration or code in this phase adds an IntegrationEvent status
    (spec 06 Definition of done, "Event state machine unchanged")."""
    assert {c.value for c in IntegrationEvent.Status} == {
        "RECEIVED",
        "PROCESSED",
        "FAILED",
        "DEAD_LETTER",
        "CANCELLED",
    }
