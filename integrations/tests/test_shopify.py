"""ShopifyAdapter and the Shopify webhook receiver: POST
/webhooks/shopify/{integration_id} (spec .claude/specs/06-shopify-app.md
Decisions 1, 2, 3, 6, 11, 15, 17; O1). Exercises the real,
production-registered ShopifyAdapter, not a test double.

HMAC test key: settings.SHOPIFY_CLIENT_SECRET (the platform app secret),
never a per-merchant credential."""
import base64
import copy
import hashlib
import hmac
import json
import threading
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from auditlog.models import AuditLog
from core.tenancy import tenant_atomic, tenant_context
from events.models import IntegrationEvent
from integrations import services
from integrations.core.registry import get_adapter
from integrations.models import Integration
from transactions.models import Transaction

pytestmark = pytest.mark.django_db(transaction=True)

SECRET = "test-shopify-client-secret"
PREVIOUS_SECRET = "old-shopify-client-secret"
SHOP_DOMAIN = "test-shop.myshopify.com"
OTHER_SHOP_DOMAIN = "other-shop.myshopify.com"
SHOP_ID = "820982911946154508"  # matches the fixture's order id, deliberately, for a distinct uninstall id below
UNINSTALL_SHOP_ID = "111222333"

FIXTURE_PATH = "integrations/tests/fixtures/shopify_orders_paid_2026-07.json"


def _load_fixture() -> dict:
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _override(secret=SECRET, previous=""):
    return override_settings(SHOPIFY_CLIENT_SECRET=secret, SHOPIFY_CLIENT_SECRET_PREVIOUS=previous)


def _url(integration_id):
    return f"/api/v1/webhooks/shopify/{integration_id}"


def _generic_url(integration_id):
    return f"/api/v1/webhooks/generic/{integration_id}"


def _connect_shopify(owner, *, shop_domain=SHOP_DOMAIN, shop_id=UNINSTALL_SHOP_ID, location=None):
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration = Integration.objects.create(
            merchant_id=owner.merchant_id,
            provider=Integration.Provider.SHOPIFY,
            status=Integration.Status.CONNECTED,
            credentials_encrypted=None,
            config_json={"shop_domain": shop_domain, "shop_id": shop_id, "webhook_subscription_ids": []},
        )
        if location is not None:
            services.add_location_mapping(integration, location_id=location.id)
    return integration


def _sign(secret, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def _post(
    client,
    integration_id,
    body: bytes,
    *,
    capture,
    secret=SECRET,
    topic="orders/paid",
    shop_domain=SHOP_DOMAIN,
    webhook_id="wh-1",
    bad_signature=False,
):
    headers = {"HTTP_X_SHOPIFY_TOPIC": topic, "HTTP_X_SHOPIFY_SHOP_DOMAIN": shop_domain}
    if bad_signature:
        headers["HTTP_X_SHOPIFY_HMAC_SHA256"] = "not-a-real-signature"
    elif secret:
        headers["HTTP_X_SHOPIFY_HMAC_SHA256"] = _sign(secret, body)
    if webhook_id is not None:
        headers["HTTP_X_SHOPIFY_WEBHOOK_ID"] = webhook_id
    with capture(execute=True):
        return client.generic("POST", _url(integration_id), data=body, content_type="application/json", **headers)


def _uninstall_payload(shop_id=UNINSTALL_SHOP_ID):
    return {"id": int(shop_id), "name": "Test Shop", "email": "shop@example.com", "domain": None}


@pytest.fixture(autouse=True)
def local_cache(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    yield
    cache.clear()


# --- Valid delivery ---------------------------------------------------------


def test_valid_orders_paid_creates_one_event_and_transaction(make_merchant, make_location, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = _connect_shopify(owner, location=location)
    body = json.dumps(_load_fixture()).encode()
    client = APIClient()

    with _override():
        resp = _post(client, integration.id, body, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 200, resp.content

    with tenant_context(owner.merchant_id), tenant_atomic():
        events = list(IntegrationEvent.objects.filter(integration=integration))
        assert len(events) == 1
        assert events[0].merchant_id == owner.merchant_id
        assert events[0].source == "shopify"
        assert events[0].external_event_id == "wh-1"
        txn = Transaction.objects.get(location=location, external_transaction_id="820982911946154508")
        assert txn.amount == Decimal("404.95")
        assert txn.currency == "USD"


# --- Fail closed -------------------------------------------------------------


def test_missing_hmac_returns_401(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override():
        resp = _post(client, integration.id, body, secret=None, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not IntegrationEvent.objects.filter(integration=integration).exists()


def test_wrong_hmac_returns_401(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override():
        resp = _post(client, integration.id, body, bad_signature=True, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401


def test_hmac_over_modified_body_returns_401(make_merchant, django_capture_on_commit_callbacks):
    """Proves raw-body verification: signing the original body, then
    changing whitespace/key order after signing, fails."""
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    signature = _sign(SECRET, body)
    modified = json.dumps(_load_fixture(), indent=2).encode()
    with _override():
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.generic(
                "POST",
                _url(integration.id),
                data=modified,
                content_type="application/json",
                HTTP_X_SHOPIFY_HMAC_SHA256=signature,
                HTTP_X_SHOPIFY_TOPIC="orders/paid",
                HTTP_X_SHOPIFY_SHOP_DOMAIN=SHOP_DOMAIN,
                HTTP_X_SHOPIFY_WEBHOOK_ID="wh-1",
            )
    assert resp.status_code == 401


def test_unknown_integration_id_returns_401(django_capture_on_commit_callbacks):
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override():
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.generic(
                "POST",
                _url("00000000-0000-0000-0000-000000000000"),
                data=body,
                content_type="application/json",
                HTTP_X_SHOPIFY_HMAC_SHA256=_sign(SECRET, body),
                HTTP_X_SHOPIFY_TOPIC="orders/paid",
                HTTP_X_SHOPIFY_SHOP_DOMAIN=SHOP_DOMAIN,
                HTTP_X_SHOPIFY_WEBHOOK_ID="wh-1",
            )
    assert resp.status_code == 401


def test_wrong_provider_id_returns_401_both_directions(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    shopify_integration = _connect_shopify(owner)
    with tenant_context(owner.merchant_id), tenant_atomic():
        csv_integration, _ = services.connect_integration(actor=owner, provider="csv")
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()

    # A csv integration id on the Shopify URL: csv's verify() always
    # returns False, so this is 401 regardless -- but it also proves the
    # provider mismatch, since the receiver checks provider == "shopify"
    # before ever calling verify().
    with _override():
        resp = _post(client, csv_integration.id, body, capture=django_capture_on_commit_callbacks)
        assert resp.status_code == 401

    # A shopify integration id on the generic webhook URL.
    with django_capture_on_commit_callbacks(execute=True):
        resp2 = client.generic(
            "POST",
            _generic_url(shopify_integration.id),
            data=b"{}",
            content_type="application/json",
            HTTP_X_REVIEWFLOW_SIGNATURE="sha256=doesnotmatter",
        )
    assert resp2.status_code == 401


def test_disconnected_integration_rejects_non_uninstall_topic(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.disconnect_integration(actor=owner, integration=integration)
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override():
        resp = _post(client, integration.id, body, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401


def test_missing_webhook_id_on_orders_paid_returns_401(make_merchant, django_capture_on_commit_callbacks):
    """O1: X-Shopify-Webhook-Id is required. A missing header gives the
    identical 401, not the generic webhook's 422."""
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override():
        resp = _post(client, integration.id, body, webhook_id=None, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not IntegrationEvent.objects.filter(integration=integration).exists()


# --- Non-sale topics and malformed bodies -----------------------------------


def test_other_verified_topic_returns_200_and_stores_nothing(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override():
        resp = _post(client, integration.id, body, topic="refunds/create", capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 200
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not IntegrationEvent.objects.filter(integration=integration).exists()


def test_non_json_body_returns_400(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = b"not json"
    with _override():
        resp = _post(client, integration.id, body, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 400


# --- Duplicate / cross-merchant / cross-integration (priority scenarios) ----


def test_duplicate_delivery_five_times_gives_one_event_and_transaction(
    make_merchant, make_location, django_capture_on_commit_callbacks
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = _connect_shopify(owner, location=location)
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override():
        for _ in range(5):
            resp = _post(client, integration.id, body, capture=django_capture_on_commit_callbacks)
            assert resp.status_code == 200
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.filter(integration=integration).count() == 1
        assert Transaction.objects.filter(location=location).count() == 1


def test_cross_merchant_same_dedupe_id_creates_two_events(make_merchant, django_capture_on_commit_callbacks):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_a = _connect_shopify(owner_a, shop_domain="shop-a.myshopify.com")
    integration_b = _connect_shopify(owner_b, shop_domain="shop-b.myshopify.com")
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override():
        resp_a = _post(client, integration_a.id, body, shop_domain="shop-a.myshopify.com", capture=django_capture_on_commit_callbacks)
        resp_b = _post(client, integration_b.id, body, shop_domain="shop-b.myshopify.com", capture=django_capture_on_commit_callbacks)
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.filter(integration=integration_a).count() == 1
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.filter(integration=integration_b).count() == 1


def test_two_integrations_same_merchant_same_dedupe_id_create_two_events(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration_1 = _connect_shopify(owner, shop_domain="shop-1.myshopify.com")
    integration_2 = _connect_shopify(owner, shop_domain="shop-2.myshopify.com")
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override():
        _post(client, integration_1.id, body, shop_domain="shop-1.myshopify.com", capture=django_capture_on_commit_callbacks)
        _post(client, integration_2.id, body, shop_domain="shop-2.myshopify.com", capture=django_capture_on_commit_callbacks)
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.filter(integration=integration_1).count() == 1
        assert IntegrationEvent.objects.filter(integration=integration_2).count() == 1


# --- Shop-domain binding and rotation ---------------------------------------


def test_shop_domain_mismatch_returns_401(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)  # config shop_domain == SHOP_DOMAIN
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override():
        resp = _post(client, integration.id, body, shop_domain=OTHER_SHOP_DOMAIN, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not IntegrationEvent.objects.filter(integration=integration).exists()


def test_rotation_accepts_either_secret_while_previous_is_set(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override(secret=SECRET, previous=PREVIOUS_SECRET):
        resp_new = _post(client, integration.id, body, secret=SECRET, webhook_id="wh-new", capture=django_capture_on_commit_callbacks)
        resp_old = _post(client, integration.id, body, secret=PREVIOUS_SECRET, webhook_id="wh-old", capture=django_capture_on_commit_callbacks)
    assert resp_new.status_code == 200
    assert resp_old.status_code == 200


def test_previous_secret_rejected_when_unset(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override(secret=SECRET, previous=""):
        resp = _post(client, integration.id, body, secret=PREVIOUS_SECRET, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401


# --- Uninstall ---------------------------------------------------------------


def test_uninstall_connected_disconnects_with_one_audit_row(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = json.dumps(_uninstall_payload()).encode()
    with _override():
        resp = _post(client, integration.id, body, topic="app/uninstalled", capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 200
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration.refresh_from_db()
        assert integration.status == Integration.Status.DISCONNECTED
        assert integration.credentials_encrypted is None
        rows = AuditLog.objects.filter(action="integration.disconnected", target_id=str(integration.id))
        assert rows.count() == 1
        assert rows.first().actor_user_id is None
        assert rows.first().metadata_json.get("reason") == "app_uninstalled"


def test_uninstall_repeat_delivery_is_a_no_op(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = json.dumps(_uninstall_payload()).encode()
    with _override():
        _post(client, integration.id, body, topic="app/uninstalled", webhook_id="wh-1", capture=django_capture_on_commit_callbacks)
        resp2 = _post(client, integration.id, body, topic="app/uninstalled", webhook_id="wh-2", capture=django_capture_on_commit_callbacks)
    assert resp2.status_code == 200
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert AuditLog.objects.filter(action="integration.disconnected", target_id=str(integration.id)).count() == 1


def test_uninstall_concurrent_deliveries_give_one_audit_row(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    body = json.dumps(_uninstall_payload()).encode()
    results = []

    def _deliver():
        client = APIClient()
        with _override():
            resp = _post(client, integration.id, body, topic="app/uninstalled", webhook_id="wh-1", capture=django_capture_on_commit_callbacks)
        results.append(resp.status_code)

    threads = [threading.Thread(target=_deliver) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code == 200 for code in results)
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert AuditLog.objects.filter(action="integration.disconnected", target_id=str(integration.id)).count() == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h, td: h.__setitem__("HTTP_X_SHOPIFY_HMAC_SHA256", "bad"),
        lambda h, td: h.__setitem__("HTTP_X_SHOPIFY_SHOP_DOMAIN", OTHER_SHOP_DOMAIN),
    ],
)
def test_uninstall_still_fail_closed_on_disconnected(make_merchant, django_capture_on_commit_callbacks, mutate):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = json.dumps(_uninstall_payload()).encode()
    with _override():
        _post(client, integration.id, body, topic="app/uninstalled", webhook_id="wh-1", capture=django_capture_on_commit_callbacks)
        with tenant_context(owner.merchant_id), tenant_atomic():
            integration.refresh_from_db()
            assert integration.status == Integration.Status.DISCONNECTED
        headers = {"HTTP_X_SHOPIFY_TOPIC": "app/uninstalled", "HTTP_X_SHOPIFY_SHOP_DOMAIN": SHOP_DOMAIN}
        mutate(headers, None)
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.generic("POST", _url(integration.id), data=body, content_type="application/json", **headers)
    assert resp.status_code == 401


def test_orders_paid_to_disconnected_returns_401(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.disconnect_integration(actor=owner, integration=integration)
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override():
        resp = _post(client, integration.id, body, capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not IntegrationEvent.objects.filter(integration=integration).exists()


def test_topic_relabel_cannot_uninstall(make_merchant, django_capture_on_commit_callbacks):
    """Regression: the orders/paid fixture, validly signed, sent with
    X-Shopify-Topic: app/uninstalled must NOT disconnect. Proves the
    current-topic discriminator, not the topic header, gates the
    destructive action."""
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override():
        resp = _post(client, integration.id, body, topic="app/uninstalled", capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration.refresh_from_db()
        assert integration.status == Integration.Status.CONNECTED
        assert not AuditLog.objects.filter(action="integration.disconnected", target_id=str(integration.id)).exists()
        assert not IntegrationEvent.objects.filter(integration=integration).exists()


def test_uninstall_missing_webhook_id_returns_401_on_connected_and_disconnected(
    make_merchant, django_capture_on_commit_callbacks
):
    """O1 regression: the uninstall fast path can never bypass the
    webhook-id check."""
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = json.dumps(_uninstall_payload()).encode()

    with _override():
        resp1 = _post(client, integration.id, body, topic="app/uninstalled", webhook_id=None, capture=django_capture_on_commit_callbacks)
    assert resp1.status_code == 401
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration.refresh_from_db()
        assert integration.status == Integration.Status.CONNECTED
        assert not AuditLog.objects.filter(action="integration.disconnected", target_id=str(integration.id)).exists()

    # Now actually disconnect it (with the header present) and retry without one.
    with _override():
        _post(client, integration.id, body, topic="app/uninstalled", webhook_id="wh-1", capture=django_capture_on_commit_callbacks)
        resp2 = _post(client, integration.id, body, topic="app/uninstalled", webhook_id=None, capture=django_capture_on_commit_callbacks)
    assert resp2.status_code == 401
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert AuditLog.objects.filter(action="integration.disconnected", target_id=str(integration.id)).count() == 1


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"id": "not-an-int", "name": "x"},
        {"name": "x"},  # missing id
        {"id": True, "name": "x"},  # bool, not int
        {"id": int(UNINSTALL_SHOP_ID), "line_items": []},  # carries an Order-only key
    ],
)
def test_uninstall_binding_failures_return_401(make_merchant, django_capture_on_commit_callbacks, bad_payload):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = json.dumps(bad_payload).encode()
    with _override():
        resp = _post(client, integration.id, body, topic="app/uninstalled", capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration.refresh_from_db()
        assert integration.status == Integration.Status.CONNECTED


def test_uninstall_wrong_shop_id_returns_401(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration = _connect_shopify(owner, shop_id="999999")
    client = APIClient()
    body = json.dumps(_uninstall_payload(shop_id="111111")).encode()
    with _override():
        resp = _post(client, integration.id, body, topic="app/uninstalled", capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401


def test_uninstall_non_object_body_returns_401(make_merchant, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    client = APIClient()
    body = b"[1, 2, 3]"
    with _override():
        resp = _post(client, integration.id, body, topic="app/uninstalled", capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401


# --- Adapter contract --------------------------------------------------------


def _adapter_for(config_json=None):
    integration = Integration(provider="shopify", config_json=config_json or {})
    return get_adapter(integration)


def test_contract_fixture_normalizes_to_expected_sale():
    adapter = _adapter_for()
    sale = adapter.normalize(adapter.parse(_load_fixture()))
    assert sale.external_transaction_id == "820982911946154508"
    assert sale.amount == Decimal("404.95")
    assert sale.currency == "USD"
    assert sale.occurred_at.isoformat() == "2021-12-31T19:00:00-05:00"
    assert sale.customer_phone is None  # both order-level and customer.phone are null in the sample
    assert sale.customer_name == "John Smith"
    assert sale.payment_method == "visa"
    assert sale.external_location_id is None


def test_contract_missing_id_raises_invalid_external_transaction_id():
    from integrations.core.schemas import PayloadValidationError

    payload = copy.deepcopy(_load_fixture())
    del payload["id"]
    adapter = _adapter_for()
    with pytest.raises(PayloadValidationError) as exc:
        adapter.normalize(adapter.parse(payload))
    assert exc.value.code == "INVALID_EXTERNAL_TRANSACTION_ID"


def test_contract_missing_total_price_raises_invalid_amount():
    from integrations.core.schemas import PayloadValidationError

    payload = copy.deepcopy(_load_fixture())
    del payload["total_price"]
    adapter = _adapter_for()
    with pytest.raises(PayloadValidationError) as exc:
        adapter.normalize(adapter.parse(payload))
    assert exc.value.code == "INVALID_AMOUNT"


def test_contract_missing_currency_raises_invalid_currency():
    from integrations.core.schemas import PayloadValidationError

    payload = copy.deepcopy(_load_fixture())
    payload["currency"] = None
    adapter = _adapter_for()
    with pytest.raises(PayloadValidationError) as exc:
        adapter.normalize(adapter.parse(payload))
    assert exc.value.code == "INVALID_CURRENCY"


def test_contract_null_processed_at_falls_back_to_created_at():
    payload = copy.deepcopy(_load_fixture())
    payload["processed_at"] = None
    adapter = _adapter_for()
    sale = adapter.normalize(adapter.parse(payload))
    assert sale.occurred_at.isoformat() == payload["created_at"]


def test_contract_both_timestamps_null_raises_invalid_occurred_at():
    from integrations.core.schemas import PayloadValidationError

    payload = copy.deepcopy(_load_fixture())
    payload["processed_at"] = None
    payload["created_at"] = None
    adapter = _adapter_for()
    with pytest.raises(PayloadValidationError) as exc:
        adapter.normalize(adapter.parse(payload))
    assert exc.value.code == "INVALID_OCCURRED_AT"


def test_contract_order_level_phone_is_primary():
    payload = copy.deepcopy(_load_fixture())
    payload["phone"] = "+15005550006"
    adapter = _adapter_for()
    sale = adapter.normalize(adapter.parse(payload))
    assert sale.customer_phone == "+15005550006"


def test_contract_customer_phone_is_fallback_when_order_phone_blank():
    payload = copy.deepcopy(_load_fixture())
    payload["phone"] = None
    payload["customer"]["phone"] = "+15005550007"
    adapter = _adapter_for()
    sale = adapter.normalize(adapter.parse(payload))
    assert sale.customer_phone == "+15005550007"


def test_contract_non_e164_phone_fails_the_whole_sale():
    """[User decision 2026-09-29] Shopify does not guarantee E.164 on
    read -- a non-E.164 phone still fails INVALID_PHONE, unchanged."""
    from integrations.core.schemas import PayloadValidationError

    payload = copy.deepcopy(_load_fixture())
    payload["phone"] = "(613)555-1212"
    adapter = _adapter_for()
    with pytest.raises(PayloadValidationError) as exc:
        adapter.normalize(adapter.parse(payload))
    assert exc.value.code == "INVALID_PHONE"


def test_contract_null_customer_gives_no_phone_and_no_name():
    payload = copy.deepcopy(_load_fixture())
    payload["customer"] = None
    payload["phone"] = None
    adapter = _adapter_for()
    sale = adapter.normalize(adapter.parse(payload))
    assert sale.customer_phone is None
    assert sale.customer_name is None


def test_contract_empty_payment_gateway_names_gives_none():
    payload = copy.deepcopy(_load_fixture())
    payload["payment_gateway_names"] = []
    adapter = _adapter_for()
    sale = adapter.normalize(adapter.parse(payload))
    assert sale.payment_method is None


def test_contract_missing_location_id_gives_none():
    payload = copy.deepcopy(_load_fixture())
    payload["location_id"] = None
    adapter = _adapter_for()
    sale = adapter.normalize(adapter.parse(payload))
    assert sale.external_location_id is None


def test_contract_address_phone_never_used():
    payload = copy.deepcopy(_load_fixture())
    payload["phone"] = None
    payload["customer"]["phone"] = None
    payload["billing_address"] = {"phone": "+15005550008"}
    payload["shipping_address"] = {"phone": "+15005550009"}
    adapter = _adapter_for()
    sale = adapter.normalize(adapter.parse(payload))
    assert sale.customer_phone is None


def test_verify_uses_platform_secret_never_stored_credential(make_merchant, django_capture_on_commit_callbacks):
    """A delivery signed with a value placed in the integration's
    credentials (not the platform secret) must still fail."""
    owner = make_merchant("A")
    integration = _connect_shopify(owner)
    with tenant_context(owner.merchant_id), tenant_atomic():
        from core.crypto import encrypt

        integration.credentials_encrypted = encrypt(json.dumps({"access_token": "not-the-hmac-key"}))
        integration.save(update_fields=["credentials_encrypted"])
    client = APIClient()
    body = json.dumps(_load_fixture()).encode()
    with _override():
        resp = _post(client, integration.id, body, secret="not-the-hmac-key", capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 401


# --- Multi-location resolution -----------------------------------------------


def test_pos_location_id_resolves_mapping(make_merchant, make_location, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration = _connect_shopify(owner)
        services.add_location_mapping(
            integration, location_id=location.id, config_json={"external_location_id": "49202758"}
        )
    payload = copy.deepcopy(_load_fixture())
    payload["location_id"] = 49202758
    client = APIClient()
    with _override():
        resp = _post(client, integration.id, json.dumps(payload).encode(), capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 200
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Transaction.objects.filter(location=location).exists()


def test_unmapped_location_ends_failed_location_unresolved(make_merchant, make_location, django_capture_on_commit_callbacks):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration = _connect_shopify(owner)
        services.add_location_mapping(
            integration, location_id=location.id, config_json={"external_location_id": "some-other-location"}
        )
    payload = copy.deepcopy(_load_fixture())
    payload["location_id"] = 49202758
    client = APIClient()
    with _override():
        resp = _post(client, integration.id, json.dumps(payload).encode(), capture=django_capture_on_commit_callbacks)
    assert resp.status_code == 200  # webhook receipt itself still succeeds
    with tenant_context(owner.merchant_id), tenant_atomic():
        event = IntegrationEvent.objects.get(integration=integration)
        assert event.status == IntegrationEvent.Status.FAILED
        assert event.error_code == "LOCATION_UNRESOLVED"
