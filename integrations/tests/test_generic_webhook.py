"""Generic Webhook receiver: POST /webhooks/generic/{integration_id}
(spec .claude/specs/06-priority-integrations.md Decisions 1, 5, 6, 11, 15,
16). Exercises the real GenericWebhookAdapter, not a test double -- it is
the production-registered adapter for "webhook"."""
import hashlib
import hmac
import json
import uuid

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from core.tenancy import tenant_atomic, tenant_context
from events.models import IntegrationEvent
from integrations import services
from integrations.models import Integration
from transactions.models import Transaction

pytestmark = pytest.mark.django_db(transaction=True)

FIELD_MAP = {
    "external_transaction_id": "order.id",
    "amount": "order.total",
    "currency": "order.currency",
    "occurred_at": "order.completed_at",
    "customer_phone": "customer.phone",
    "customer_name": "customer.name",
    "payment_method": "payment.method",
    "external_location_id": "store.id",
}


def _url(integration_id):
    return f"/api/v1/webhooks/generic/{integration_id}"


def _connect_webhook(owner, *, field_map=None, default_currency="INR"):
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration, issued = services.connect_integration(
            actor=owner,
            provider="webhook",
            config_json={"field_map": field_map or FIELD_MAP, "default_currency": default_currency},
        )
    return integration, issued["webhook_secret"]


def _sign(secret, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(client, integration_id, body: dict, secret, *, capture):
    raw = json.dumps(body).encode()
    sig = _sign(secret, raw)
    with capture(execute=True):
        return client.generic(
            "POST",
            _url(integration_id),
            data=raw,
            content_type="application/json",
            HTTP_X_REVIEWFLOW_SIGNATURE=sig,
        )


def _order_payload(order_id="INV-1", **overrides):
    body = {
        "order": {"id": order_id, "total": "100.00", "currency": "INR", "completed_at": "2024-01-01T10:00:00Z"},
        "customer": {"phone": "+919999999999", "name": "Rahul"},
        "payment": {"method": "UPI"},
        "store": {"id": "store-1"},
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def local_cache(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    yield
    cache.clear()


# --- happy path -----------------------------------------------------------


def test_valid_signed_delivery_returns_200_and_creates_transaction(
    make_merchant, make_location, django_capture_on_commit_callbacks
):
    owner = make_merchant("A")
    integration, secret = _connect_webhook(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id, config_json={"external_location_id": "store-1"})

    client = APIClient()
    resp = _post(client, integration.id, _order_payload(), secret, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 200
    assert resp.json() == {}

    with tenant_context(owner.merchant_id), tenant_atomic():
        event = IntegrationEvent.objects.get(integration=integration)
        assert event.status == IntegrationEvent.Status.PROCESSED
        txn = Transaction.objects.get(location=location, external_transaction_id="INV-1")
        assert str(txn.amount) == "100.00"


# --- fail closed ------------------------------------------------------


def test_missing_signature_header_returns_401_and_stores_nothing(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration, secret = _connect_webhook(owner)
    client = APIClient()
    raw = json.dumps(_order_payload()).encode()
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.generic("POST", _url(integration.id), data=raw, content_type="application/json")
    assert resp.status_code == 401
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.count() == 0


def test_wrong_signature_returns_401(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration, secret = _connect_webhook(owner)
    client = APIClient()
    resp = _post(client, integration.id, _order_payload(), "wrong-secret", capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401


def test_signature_over_modified_body_returns_401(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration, secret = _connect_webhook(owner)
    client = APIClient()
    raw = json.dumps(_order_payload()).encode()
    sig = _sign(secret, raw)
    tampered = raw + b" "  # modifies the raw body after signing
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.generic(
            "POST", _url(integration.id), data=tampered, content_type="application/json",
            HTTP_X_REVIEWFLOW_SIGNATURE=sig,
        )
    assert resp.status_code == 401


def test_malformed_non_ascii_signature_header_returns_401_not_500(
    make_merchant, django_capture_on_commit_callbacks
):
    """Regression: hmac.compare_digest() raises TypeError for a non-ASCII
    str, which verify_hmac_sha256() previously let escape uncaught. A
    crafted header must fail closed with the same 401 as any other invalid
    signature, never surface as an unhandled 500."""
    owner = make_merchant("A")
    integration, secret = _connect_webhook(owner)
    client = APIClient()
    raw = json.dumps(_order_payload()).encode()
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.generic(
            "POST",
            _url(integration.id),
            data=raw,
            content_type="application/json",
            HTTP_X_REVIEWFLOW_SIGNATURE="sha256=é" + "0" * 63,
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.count() == 0


def test_unknown_integration_id_returns_401(django_capture_on_commit_callbacks):
    client = APIClient()
    raw = json.dumps(_order_payload()).encode()
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.generic(
            "POST", _url(uuid.uuid4()), data=raw, content_type="application/json",
            HTTP_X_REVIEWFLOW_SIGNATURE="sha256=" + "a" * 64,
        )
    assert resp.status_code == 401


def test_malformed_integration_id_returns_401(django_capture_on_commit_callbacks):
    client = APIClient()
    raw = json.dumps(_order_payload()).encode()
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.generic(
            "POST", _url("not-a-uuid"), data=raw, content_type="application/json",
            HTTP_X_REVIEWFLOW_SIGNATURE="sha256=" + "a" * 64,
        )
    assert resp.status_code == 401


def test_wrong_provider_integration_id_returns_401(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        api_integration, _ = services.connect_integration(actor=owner, provider="api")
    client = APIClient()
    raw = json.dumps(_order_payload()).encode()
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.generic(
            "POST", _url(api_integration.id), data=raw, content_type="application/json",
            HTTP_X_REVIEWFLOW_SIGNATURE="sha256=" + "a" * 64,
        )
    assert resp.status_code == 401


def test_disconnected_integration_returns_401(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration, secret = _connect_webhook(owner)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.disconnect_integration(actor=owner, integration=integration)
    client = APIClient()
    resp = _post(client, integration.id, _order_payload(), secret, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401


# --- idempotency ------------------------------------------------------


def test_duplicate_delivery_five_times_yields_one_event(make_merchant, make_location, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration, secret = _connect_webhook(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id, config_json={"external_location_id": "store-1"})

    client = APIClient()
    for _ in range(5):
        resp = _post(client, integration.id, _order_payload(), secret, capture=django_capture_on_commit_callbacks)
        assert resp.status_code == 200

    with tenant_context(owner.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.filter(integration=integration).count() == 1
        assert Transaction.objects.filter(location=location).count() == 1


def test_cross_merchant_same_payload_creates_two_events(make_merchant, make_location, django_capture_on_commit_callbacks):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_a, secret_a = _connect_webhook(owner_a)
    integration_b, secret_b = _connect_webhook(owner_b)

    client = APIClient()
    resp_a = _post(client, integration_a.id, _order_payload(), secret_a, capture=django_capture_on_commit_callbacks)
    resp_b = _post(client, integration_b.id, _order_payload(), secret_b, capture=django_capture_on_commit_callbacks)
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.filter(integration=integration_a).count() == 1
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.filter(integration=integration_b).count() == 1


def test_tenant_attribution_other_merchants_secret_on_this_url_returns_401_and_no_leak(
    make_merchant, django_capture_on_commit_callbacks
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_a, secret_a = _connect_webhook(owner_a)
    _integration_b, secret_b = _connect_webhook(owner_b)

    client = APIClient()
    resp = _post(client, integration_a.id, _order_payload(), secret_b, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.filter(integration=integration_a).count() == 0


# --- payload validation ------------------------------------------------


def test_missing_mapped_transaction_id_returns_422_and_stores_nothing(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration, secret = _connect_webhook(owner)
    client = APIClient()
    payload = _order_payload()
    del payload["order"]["id"]
    resp = _post(client, integration.id, payload, secret, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 422
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.count() == 0


def test_invalid_mapped_phone_is_stored_and_ends_failed(make_merchant, make_location, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration, secret = _connect_webhook(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id, config_json={"external_location_id": "store-1"})

    client = APIClient()
    payload = _order_payload()
    payload["customer"]["phone"] = "98765"
    resp = _post(client, integration.id, payload, secret, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 200  # stored; the pipeline outcome is FAILED, not an HTTP error
    with tenant_context(owner.merchant_id), tenant_atomic():
        event = IntegrationEvent.objects.get(integration=integration)
        assert event.status == IntegrationEvent.Status.FAILED
        assert event.error_code == "INVALID_PHONE"


def test_non_object_body_returns_400(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration, secret = _connect_webhook(owner)
    client = APIClient()
    raw = json.dumps([1, 2, 3]).encode()
    sig = _sign(secret, raw)
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.generic(
            "POST", _url(integration.id), data=raw, content_type="application/json",
            HTTP_X_REVIEWFLOW_SIGNATURE=sig,
        )
    assert resp.status_code == 400


# --- multi-location resolution ------------------------------------------


def test_multi_location_resolves_by_external_location_id(make_merchant, make_location, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration, secret = _connect_webhook(owner)
    loc1 = make_location(owner.merchant, name="Store 1")
    loc2 = make_location(owner.merchant, name="Store 2")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=loc1.id, config_json={"external_location_id": "store-1"})
        services.add_location_mapping(integration, location_id=loc2.id, config_json={"external_location_id": "store-2"})

    client = APIClient()
    payload = _order_payload(order_id="INV-42")
    payload["store"]["id"] = "store-2"
    resp = _post(client, integration.id, payload, secret, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 200
    with tenant_context(owner.merchant_id), tenant_atomic():
        txn = Transaction.objects.get(external_transaction_id="INV-42")
        assert txn.location_id == loc2.id


def test_unmapped_location_ends_failed_location_unresolved(make_merchant, make_location, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration, secret = _connect_webhook(owner)
    loc1 = make_location(owner.merchant, name="Store 1")
    loc2 = make_location(owner.merchant, name="Store 2")
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=loc1.id, config_json={"external_location_id": "store-1"})
        services.add_location_mapping(integration, location_id=loc2.id, config_json={"external_location_id": "store-2"})

    client = APIClient()
    payload = _order_payload()
    payload["store"]["id"] = "store-unknown"
    resp = _post(client, integration.id, payload, secret, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 200
    with tenant_context(owner.merchant_id), tenant_atomic():
        event = IntegrationEvent.objects.get(integration=integration)
        assert event.status == IntegrationEvent.Status.FAILED
        assert event.error_code == "LOCATION_UNRESOLVED"


# --- session cookie safety ----------------------------------------------


def test_session_cookie_plus_webhook_delivery_returns_normal_response_not_500(
    make_merchant, make_location, session_client, django_capture_on_commit_callbacks
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_a, secret_a = _connect_webhook(owner_a)
    location = make_location(owner_a.merchant)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        services.add_location_mapping(integration_a, location_id=location.id, config_json={"external_location_id": "store-1"})

    # Logged in as B's owner, but delivering a validly signed webhook for A.
    client = session_client(owner_b.user.email)
    resp = _post(client, integration_a.id, _order_payload(), secret_a, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 200
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.filter(integration=integration_a).count() == 1
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.count() == 0


# --- throttle ------------------------------------------------------------


def test_throttle_returns_429_before_lookup(monkeypatch, make_merchant, django_capture_on_commit_callbacks):
    # SimpleRateThrottle.THROTTLE_RATES is bound at class-definition (import)
    # time from api_settings.DEFAULT_THROTTLE_RATES -- mutating
    # settings.REST_FRAMEWORK at runtime does not reach it. Patch the class
    # attribute directly, mirroring apikeys/tests/conftest.py's
    # throttle_rates fixture.
    from rest_framework.settings import api_settings as drf_api_settings

    from integrations.throttling import WebhookIpRateThrottle

    merged = {**drf_api_settings.DEFAULT_THROTTLE_RATES, "webhook_ip": "1/min"}
    monkeypatch.setattr(WebhookIpRateThrottle, "THROTTLE_RATES", merged)
    cache.clear()

    client = APIClient()
    raw = json.dumps(_order_payload()).encode()
    with django_capture_on_commit_callbacks(execute=True):
        resp1 = client.generic(
            "POST", _url(uuid.uuid4()), data=raw, content_type="application/json",
            HTTP_X_REVIEWFLOW_SIGNATURE="sha256=" + "a" * 64,
        )
        resp2 = client.generic(
            "POST", _url(uuid.uuid4()), data=raw, content_type="application/json",
            HTTP_X_REVIEWFLOW_SIGNATURE="sha256=" + "a" * 64,
        )
    assert resp1.status_code == 401  # unknown id, but throttle allowed it through
    assert resp2.status_code == 429


# --- robust enqueue: broker failure never turns a stored delivery into 500 -


def test_broker_enqueue_failure_still_returns_200_event_stays_received(
    make_merchant, make_location, monkeypatch, django_capture_on_commit_callbacks, caplog
):
    """DoD "With the Celery broker enqueue forced to raise: ... A Generic
    Webhook delivery still returns 200. Its committed RECEIVED event is
    picked up by the stale-RECEIVED sweep (Decision 9)." Unlike /sales,
    the webhook receiver never processes inline, so the event is left
    RECEIVED -- the stale-RECEIVED sweep, not this request, will process it.
    The log must never carry the raw exception message."""
    from events.tasks import process_integration_event

    def _boom(*args, **kwargs):
        raise ConnectionError("broker unreachable at redis://secret-user:secret-pass@host")

    monkeypatch.setattr(process_integration_event, "delay", _boom)

    owner = make_merchant("A")
    integration, secret = _connect_webhook(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id, config_json={"external_location_id": "store-1"})

    client = APIClient()
    with caplog.at_level("WARNING"):
        resp = _post(
            client, integration.id, _order_payload(order_id="INV-BROKER"), secret,
            capture=django_capture_on_commit_callbacks,
        )
    assert resp.status_code == 200
    with tenant_context(owner.merchant_id), tenant_atomic():
        event = IntegrationEvent.objects.get(integration=integration)
        assert event.status == IntegrationEvent.Status.RECEIVED
    for record in caplog.records:
        assert "secret-user" not in record.getMessage()
        assert "secret-pass" not in record.getMessage()


# --- hygiene --------------------------------------------------------------


def test_verification_failure_log_does_not_leak_payload_or_signature(
    make_merchant, caplog, django_capture_on_commit_callbacks
):
    owner = make_merchant("A")
    integration, secret = _connect_webhook(owner)
    client = APIClient()
    with caplog.at_level("WARNING"):
        _post(client, integration.id, _order_payload(), "wrong-secret", capture=django_capture_on_commit_callbacks)
    for record in caplog.records:
        assert "wrong-secret" not in record.getMessage()
        assert "+919999999999" not in record.getMessage()
