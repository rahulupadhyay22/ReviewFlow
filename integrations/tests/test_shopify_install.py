"""Shopify OAuth install, callback, pending and link (spec
.claude/specs/06-shopify-app.md Decision 3, merchant flow steps 1-8; O2).
Shopify's own HTTP endpoints are replaced by a fake
integrations.shopify.services._shopify_post -- no network calls."""
import base64
import hashlib
import hmac
import json
import threading
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone as dj_timezone
from rest_framework.test import APIClient

from auditlog.models import AuditLog
from core.crypto import decrypt
from core.tenancy import tenant_atomic, tenant_context
from integrations.models import Integration
from integrations.shopify import services as shopify_services

pytestmark = pytest.mark.django_db(transaction=True)

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-shopify-client-secret"
SHOP = "test-shop.myshopify.com"
REDIRECT_URI = "https://api.reviewflow.example/api/v1/integrations/shopify/callback"
LINK_PAGE_URL = "https://app.reviewflow.example/integrations/shopify/link"

INSTALL_URL = "/api/v1/integrations/shopify/install"
CALLBACK_URL = "/api/v1/integrations/shopify/callback"
PENDING_URL = "/api/v1/integrations/shopify/pending"
LINK_URL = "/api/v1/integrations/shopify/link"


def _override(**extra):
    return override_settings(
        SHOPIFY_CLIENT_ID=CLIENT_ID,
        SHOPIFY_CLIENT_SECRET=CLIENT_SECRET,
        SHOPIFY_CLIENT_SECRET_PREVIOUS="",
        SHOPIFY_API_VERSION="2026-07",
        SHOPIFY_REDIRECT_URI=REDIRECT_URI,
        SHOPIFY_LINK_PAGE_URL=LINK_PAGE_URL,
        SHOPIFY_WEBHOOK_BASE="https://api.reviewflow.example",
        **extra,
    )


def _query_hmac(query: dict, secret=CLIENT_SECRET) -> str:
    pairs = sorted((k, v) for k, v in query.items() if k != "hmac")
    message = "&".join(f"{k}={v}" for k, v in pairs).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _callback_query(state: str, *, shop=SHOP, code="test-code", secret=CLIENT_SECRET) -> dict:
    query = {"code": code, "shop": shop, "state": state, "timestamp": "1700000000"}
    query["hmac"] = _query_hmac(query, secret=secret)
    return query


class FakeShopify:
    """Replaces integrations.shopify.services._shopify_post. `fail_on`
    selects which call to break: "second_create", "token_exchange",
    "shop_identity"."""

    def __init__(self, fail_on: str | None = None, fail_delete: bool = False):
        self.fail_on = fail_on
        self.fail_delete = fail_delete
        self.create_calls = []
        self.delete_calls = []

    def __call__(self, url: str, data: bytes, headers: dict, *, timeout=10) -> dict:
        parts = urlsplit(url)
        if parts.path.endswith("/admin/oauth/access_token"):
            if self.fail_on == "token_exchange":
                raise Exception("simulated transport error")  # noqa: TRY002 -- should be caught & converted
            return {
                "access_token": "shpat_test_token",
                "refresh_token": "shprt_test_token",
                "expires_in": 3600,
                "refresh_token_expires_in": 7776000,
                "scope": "read_orders",
            }
        assert parts.path.endswith("/graphql.json")
        body = json.loads(data)
        query = body["query"]
        if "myshopifyDomain" in query:
            if self.fail_on == "shop_identity":
                return {"errors": [{"message": "boom"}]}
            return {"data": {"shop": {"id": "gid://shopify/Shop/820982911946154508", "myshopifyDomain": SHOP}}}
        if "webhookSubscriptionCreate" in query:
            topic = body["variables"]["topic"]
            self.create_calls.append((topic, body["variables"]["webhookSubscription"]["uri"], headers.get("X-Shopify-Access-Token")))
            if self.fail_on == "second_create" and topic == "APP_UNINSTALLED":
                return {
                    "data": {
                        "webhookSubscriptionCreate": {
                            "webhookSubscription": None,
                            "userErrors": [{"field": ["topic"], "message": "boom"}],
                        }
                    }
                }
            return {
                "data": {
                    "webhookSubscriptionCreate": {
                        "webhookSubscription": {"id": f"gid://shopify/WebhookSubscription/{len(self.create_calls)}"},
                        "userErrors": [],
                    }
                }
            }
        if "webhookSubscriptionDelete" in query:
            self.delete_calls.append(body["variables"]["id"])
            if self.fail_delete:
                raise Exception("simulated delete failure")  # noqa: TRY002
            return {
                "data": {
                    "webhookSubscriptionDelete": {
                        "deletedWebhookSubscriptionId": body["variables"]["id"],
                        "userErrors": [],
                    }
                }
            }
        raise AssertionError(f"unexpected graphql query: {query}")


@pytest.fixture(autouse=True)
def local_cache(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def fake_shopify(monkeypatch):
    fake = FakeShopify()
    monkeypatch.setattr(shopify_services, "_shopify_post", fake)
    return fake


def _deliver(client, path, body: bytes, *, webhook_id, topic="orders/paid", shop=SHOP):
    """A Shopify webhook delivery signed with the test platform secret."""
    signature = base64.b64encode(hmac.new(CLIENT_SECRET.encode(), body, hashlib.sha256).digest()).decode()
    return client.generic(
        "POST",
        path,
        data=body,
        content_type="application/json",
        HTTP_X_SHOPIFY_HMAC_SHA256=signature,
        HTTP_X_SHOPIFY_TOPIC=topic,
        HTTP_X_SHOPIFY_SHOP_DOMAIN=shop,
        HTTP_X_SHOPIFY_WEBHOOK_ID=webhook_id,
    )


def _link_as_owner(client, owner_client):
    """Moves the anonymous install's pending installation into the logged-in
    OWNER's session, then links."""
    session = owner_client.session
    session["shopify_pending"] = client.session["shopify_pending"]
    session.save()
    return owner_client.post(LINK_URL, HTTP_X_CSRFTOKEN=owner_client.csrf)


def _install_then_callback(client, *, shop=SHOP, secret=CLIENT_SECRET):
    """Runs /install then /callback on the SAME client (one cookie jar),
    returning the callback response."""
    resp = client.get(INSTALL_URL, {"shop": shop})
    assert resp.status_code == 302, resp.content
    parsed = urlsplit(resp["Location"])
    state = parse_qs(parsed.query)["state"][0]
    return client.get(CALLBACK_URL, _callback_query(state, shop=shop, secret=secret))


# --- Install -----------------------------------------------------------------


def test_install_redirects_with_exact_params_and_stores_state(client):
    with _override():
        resp = client.get(INSTALL_URL, {"shop": SHOP})
    assert resp.status_code == 302
    parsed = urlsplit(resp["Location"])
    assert parsed.netloc == SHOP
    assert parsed.path == "/admin/oauth/authorize"
    params = parse_qs(parsed.query)
    assert params["client_id"] == [CLIENT_ID]
    assert params["scope"] == ["read_orders"]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert "state" in params
    assert client.session["shopify_oauth"]["state"] == params["state"][0]


def test_install_invalid_shop_returns_400(client):
    with _override():
        resp = client.get(INSTALL_URL, {"shop": "not-a-shop"})
    assert resp.status_code == 400


# --- Callback ------------------------------------------------------------


def test_callback_success_creates_no_integration_and_stores_encrypted_pending(client, fake_shopify, make_merchant):
    owner = make_merchant("A")
    with _override():
        resp = _install_then_callback(client)
    assert resp.status_code == 302
    assert resp["Location"] == LINK_PAGE_URL
    assert urlsplit(resp["Location"]).query == ""
    assert urlsplit(resp["Location"]).fragment == ""
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not Integration.objects.filter(provider="shopify").exists()
    pending = client.session["shopify_pending"]
    assert pending["shop"] == SHOP
    # The token is never plaintext in the session store.
    assert "shpat_test_token" not in json.dumps(client.session.get("shopify_pending"))
    decrypted = json.loads(decrypt(pending["credentials_fernet"]))
    assert decrypted["access_token"] == "shpat_test_token"


def test_callback_bad_hmac_returns_400_and_stores_nothing(client):
    with _override():
        resp = client.get(INSTALL_URL, {"shop": SHOP})
        parsed = urlsplit(resp["Location"])
        state = parse_qs(parsed.query)["state"][0]
        query = _callback_query(state)
        query["hmac"] = "0" * 64
        resp2 = client.get(CALLBACK_URL, query)
    assert resp2.status_code == 400
    assert "shopify_pending" not in client.session


def test_callback_missing_state_returns_400(client):
    with _override():
        resp2 = client.get(CALLBACK_URL, _callback_query("never-issued-state"))
    assert resp2.status_code == 400


def test_callback_reused_state_fails_on_second_attempt(client, fake_shopify):
    with _override():
        resp = client.get(INSTALL_URL, {"shop": SHOP})
        parsed = urlsplit(resp["Location"])
        state = parse_qs(parsed.query)["state"][0]
        query = _callback_query(state)
        resp2 = client.get(CALLBACK_URL, query)
        assert resp2.status_code == 302
        resp3 = client.get(CALLBACK_URL, query)  # same state again
    assert resp3.status_code == 400


def test_callback_expired_state_returns_400(client):
    with _override():
        resp = client.get(INSTALL_URL, {"shop": SHOP})
        parsed = urlsplit(resp["Location"])
        state = parse_qs(parsed.query)["state"][0]
        session = client.session
        session["shopify_oauth"]["issued_at"] = (dj_timezone.now() - timedelta(minutes=11)).isoformat()
        session.save()
        resp2 = client.get(CALLBACK_URL, _callback_query(state))
    assert resp2.status_code == 400


def test_callback_shop_mismatch_returns_400(client):
    with _override():
        resp = client.get(INSTALL_URL, {"shop": SHOP})
        parsed = urlsplit(resp["Location"])
        state = parse_qs(parsed.query)["state"][0]
        resp2 = client.get(CALLBACK_URL, _callback_query(state, shop="different-shop.myshopify.com"))
    assert resp2.status_code == 400


def test_callback_different_session_returns_400(client):
    from django.test import Client as DjangoClient

    other_client = DjangoClient()
    with _override():
        resp = client.get(INSTALL_URL, {"shop": SHOP})
        parsed = urlsplit(resp["Location"])
        state = parse_qs(parsed.query)["state"][0]
        resp2 = other_client.get(CALLBACK_URL, _callback_query(state))
    assert resp2.status_code == 400


def test_callback_missing_scope_returns_400(client, monkeypatch):
    def _no_read_orders(url, data, headers, *, timeout=10):
        return {"access_token": "x", "refresh_token": "y", "expires_in": 3600, "scope": "read_products"}

    monkeypatch.setattr(shopify_services, "_shopify_post", _no_read_orders)
    with _override():
        resp = client.get(INSTALL_URL, {"shop": SHOP})
        parsed = urlsplit(resp["Location"])
        state = parse_qs(parsed.query)["state"][0]
        resp2 = client.get(CALLBACK_URL, _callback_query(state))
    assert resp2.status_code == 400


# --- Account linking -------------------------------------------------------


def test_link_without_session_returns_403(client, fake_shopify):
    with _override():
        resp = client.post(LINK_URL)
    assert resp.status_code == 403


def test_link_manager_gets_403_and_pending_remains(client, fake_shopify, make_merchant, add_member, session_client):
    owner = make_merchant("A")
    manager_email = "manager@example.com"
    add_member(owner.merchant, "MANAGER", manager_email)
    with _override():
        _install_then_callback(client)
        manager_client = session_client(manager_email)
        # Transplant the anonymous client's pending cookie/session onto the
        # manager's authenticated session by directly copying the value --
        # the manager's own login flow never touched Shopify.
        session = manager_client.session
        session["shopify_pending"] = client.session["shopify_pending"]
        session.save()
        pending_before = dict(manager_client.session["shopify_pending"])
        resp = manager_client.post(LINK_URL, HTTP_X_CSRFTOKEN=manager_client.csrf)
    assert resp.status_code == 403
    # Independent proof: re-load the session from the store (a fresh
    # SessionStore, not the object this test mutated) -- the pending
    # installation was NOT popped by the 403, and is still valid.
    fresh = manager_client.session
    assert fresh["shopify_pending"] == pending_before
    with _override():
        assert shopify_services.pending_shopify_shop(session=fresh) == SHOP
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not Integration.objects.filter(provider="shopify").exists()


def test_link_missing_csrf_returns_403(client, fake_shopify, make_merchant, session_client):
    owner = make_merchant("A")
    with _override():
        _install_then_callback(client)
        owner_client = session_client(owner.user.email)
        session = owner_client.session
        session["shopify_pending"] = client.session["shopify_pending"]
        session.save()
        owner_client.enforce_csrf_checks = True
        resp = owner_client.post(LINK_URL)  # no X-CSRFToken
    assert resp.status_code == 403


def test_full_flow_pending_then_link_returns_201_for_session_merchant(client, fake_shopify, make_merchant, session_client):
    owner = make_merchant("A")
    with _override():
        _install_then_callback(client)
        owner_client = session_client(owner.user.email)
        session = owner_client.session
        session["shopify_pending"] = client.session["shopify_pending"]
        session.save()

        pending_resp = owner_client.get(PENDING_URL)
        assert pending_resp.status_code == 200
        assert pending_resp.json() == {"shop": SHOP}

        link_resp = owner_client.post(LINK_URL, HTTP_X_CSRFTOKEN=owner_client.csrf)
    assert link_resp.status_code == 201, link_resp.content
    body = link_resp.json()
    integration_id = body["id"]

    with tenant_context(owner.merchant_id), tenant_atomic():
        integration = Integration.objects.get(pk=integration_id)
        assert integration.merchant_id == owner.merchant_id
        assert integration.status == Integration.Status.CONNECTED
        creds = json.loads(decrypt(integration.credentials_encrypted))
        assert set(creds.keys()) == {
            "access_token",
            "access_token_expires_at",
            "refresh_token",
            "refresh_token_expires_at",
            "scope",
        }
        assert integration.config_json["shop_domain"] == SHOP
        assert integration.config_json["shop_id"] == "820982911946154508"
        assert len(integration.config_json["webhook_subscription_ids"]) == 2
        assert AuditLog.objects.filter(action="integration.connected", target_id=str(integration.id)).count() == 1

    assert "shpat_test_token" not in json.dumps(body)
    assert fake_shopify.create_calls[0][0] == "ORDERS_PAID"
    assert fake_shopify.create_calls[1][0] == "APP_UNINSTALLED"
    for _, uri, token in fake_shopify.create_calls:
        assert uri == f"https://api.reviewflow.example/api/v1/webhooks/shopify/{integration_id}"
        assert token == "shpat_test_token"


def test_second_link_returns_409(client, fake_shopify, make_merchant, session_client):
    owner = make_merchant("A")
    with _override():
        _install_then_callback(client)
        owner_client = session_client(owner.user.email)
        session = owner_client.session
        session["shopify_pending"] = client.session["shopify_pending"]
        session.save()
        owner_client.post(LINK_URL, HTTP_X_CSRFTOKEN=owner_client.csrf)
        resp2 = owner_client.post(LINK_URL, HTTP_X_CSRFTOKEN=owner_client.csrf)
    assert resp2.status_code == 409


def test_expired_pending_returns_409(client, fake_shopify, make_merchant, session_client):
    owner = make_merchant("A")
    with _override():
        _install_then_callback(client)
        owner_client = session_client(owner.user.email)
        pending = dict(client.session["shopify_pending"])
        pending["issued_at"] = (dj_timezone.now() - timedelta(minutes=16)).isoformat()
        session = owner_client.session
        session["shopify_pending"] = pending
        session.save()
        resp = owner_client.post(LINK_URL, HTTP_X_CSRFTOKEN=owner_client.csrf)
    assert resp.status_code == 409


def test_link_body_fields_are_ignored(client, fake_shopify, make_merchant, session_client):
    owner = make_merchant("A")
    other = make_merchant("B")
    with _override():
        _install_then_callback(client)
        owner_client = session_client(owner.user.email)
        session = owner_client.session
        session["shopify_pending"] = client.session["shopify_pending"]
        session.save()
        resp = owner_client.post(
            LINK_URL,
            {"shop": "attacker.myshopify.com", "merchant_id": str(other.merchant_id), "credentials": {"x": 1}},
            format="json",
            HTTP_X_CSRFTOKEN=owner_client.csrf,
        )
    assert resp.status_code == 201
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration = Integration.objects.get(config_json__shop_domain=SHOP)
        assert integration.merchant_id == owner.merchant_id


def test_attach_to_arbitrary_merchant_fails(client, fake_shopify, make_merchant, session_client):
    """Merchant B's OWNER, in a different session, cannot link A's
    pending installation."""
    make_merchant("A")
    owner_b = make_merchant("B")
    with _override():
        _install_then_callback(client)  # pending is in `client`'s (anonymous) session only
        b_client = session_client(owner_b.user.email)
        pending_resp = b_client.get(PENDING_URL)
        assert pending_resp.status_code == 404
        link_resp = b_client.post(LINK_URL, HTTP_X_CSRFTOKEN=b_client.csrf)
    assert link_resp.status_code == 409
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        assert not Integration.objects.filter(provider="shopify").exists()


def test_myshopify_domain_mismatch_returns_400_and_stores_nothing(client, make_merchant, session_client, monkeypatch):
    def _wrong_domain(url, data, headers, *, timeout=10):
        if url.endswith("/graphql.json") and "myshopifyDomain" in json.loads(data)["query"]:
            return {"data": {"shop": {"id": "gid://shopify/Shop/1", "myshopifyDomain": "different.myshopify.com"}}}
        return {"access_token": "x", "refresh_token": "y", "expires_in": 3600, "scope": "read_orders"}

    monkeypatch.setattr(shopify_services, "_shopify_post", _wrong_domain)
    owner = make_merchant("A")
    with _override():
        _install_then_callback(client)
        owner_client = session_client(owner.user.email)
        session = owner_client.session
        session["shopify_pending"] = client.session["shopify_pending"]
        session.save()
        resp = owner_client.post(LINK_URL, HTTP_X_CSRFTOKEN=owner_client.csrf)
    assert resp.status_code == 400
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not Integration.objects.filter(provider="shopify").exists()


def test_patch_on_shopify_integration_always_422(client, fake_shopify, make_merchant, session_client):
    owner = make_merchant("A")
    with _override():
        _install_then_callback(client)
        owner_client = session_client(owner.user.email)
        session = owner_client.session
        session["shopify_pending"] = client.session["shopify_pending"]
        session.save()
        link_resp = owner_client.post(LINK_URL, HTTP_X_CSRFTOKEN=owner_client.csrf)
        integration_id = link_resp.json()["id"]
        patch_resp = owner_client.patch(
            f"/api/v1/integrations/{integration_id}",
            {"config_json": {"shop_domain": "hacked.myshopify.com"}},
            format="json",
            HTTP_X_CSRFTOKEN=owner_client.csrf,
        )
    assert patch_resp.status_code == 422
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration = Integration.objects.get(pk=integration_id)
        assert integration.config_json["shop_domain"] == SHOP


# --- Registration failure / cleanup -----------------------------------------


def test_registration_failure_leaves_nothing_behind(client, make_merchant, session_client, monkeypatch):
    fake = FakeShopify(fail_on="second_create")
    monkeypatch.setattr(shopify_services, "_shopify_post", fake)
    owner = make_merchant("A")
    with _override():
        _install_then_callback(client)
        owner_client = session_client(owner.user.email)
        session = owner_client.session
        session["shopify_pending"] = client.session["shopify_pending"]
        session.save()
        resp = owner_client.post(LINK_URL, HTTP_X_CSRFTOKEN=owner_client.csrf)
        assert resp.status_code == 502
        # The first subscription's would-be uri: a delivery to it gets 401,
        # because its Integration never committed.
        would_be_path = urlsplit(fake.create_calls[0][1]).path
        delivery = _deliver(APIClient(), would_be_path, json.dumps({"id": 1}).encode(), webhook_id="wh-orphan")
    assert delivery.status_code == 401
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not Integration.objects.filter(provider="shopify").exists()
        assert not AuditLog.objects.filter(action="integration.connected").exists()
    assert fake.delete_calls == ["gid://shopify/WebhookSubscription/1"]


def test_registration_failure_with_delete_also_failing(client, make_merchant, session_client, monkeypatch, caplog):
    fake = FakeShopify(fail_on="second_create", fail_delete=True)
    monkeypatch.setattr(shopify_services, "_shopify_post", fake)
    owner = make_merchant("A")
    with _override():
        _install_then_callback(client)
        owner_client = session_client(owner.user.email)
        session = owner_client.session
        session["shopify_pending"] = client.session["shopify_pending"]
        session.save()
        with caplog.at_level("DEBUG"):
            resp = owner_client.post(LINK_URL, HTTP_X_CSRFTOKEN=owner_client.csrf)
            would_be_path = urlsplit(fake.create_calls[0][1]).path
            delivery = _deliver(APIClient(), would_be_path, json.dumps({"id": 1}).encode(), webhook_id="wh-orphan")
    assert resp.status_code == 502
    assert delivery.status_code == 401
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not Integration.objects.filter(provider="shopify").exists()

    # Exactly one failed-delete record, carrying ONLY the subscription id
    # and the exception class name (Decision 3, step 8).
    delete_records = [
        r for r in caplog.records
        if r.name == "integrations.shopify.services" and "webhookSubscriptionDelete failed" in r.msg
    ]
    assert len(delete_records) == 1
    assert delete_records[0].args == ("gid://shopify/WebhookSubscription/1", "Exception")
    # Neither the failed delete nor the orphan delivery logged anything
    # sensitive.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for value in (CLIENT_SECRET, "shpat_test_token", "shprt_test_token", "test-code", "simulated delete failure"):
        assert value not in logged, f"{value!r} leaked into logs"


def test_shop_identity_failure_returns_502_and_stores_nothing(client, make_merchant, session_client, monkeypatch):
    fake = FakeShopify(fail_on="shop_identity")
    monkeypatch.setattr(shopify_services, "_shopify_post", fake)
    owner = make_merchant("A")
    with _override():
        _install_then_callback(client)
        owner_client = session_client(owner.user.email)
        session = owner_client.session
        session["shopify_pending"] = client.session["shopify_pending"]
        session.save()
        resp = owner_client.post(LINK_URL, HTTP_X_CSRFTOKEN=owner_client.csrf)
    assert resp.status_code == 502
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not Integration.objects.filter(provider="shopify").exists()


# --- TOTP carry-over (implementation-plan decision, 2026-09-29) ------------


def test_shopify_pending_survives_totp_login(client, fake_shopify, make_merchant):
    """LoginView's totp_required branch flushes the session (accounts/views.py)
    -- the [implementation-plan decision, 2026-09-29] carry-over must keep
    shopify_pending across that flush for an anonymous session."""
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        owner.user.totp_confirmed_at = dj_timezone.now()  # enrolled, bypassing real setup -- only is_totp_enabled matters here
        owner.user.save(update_fields=["totp_confirmed_at"])

    with _override():
        _install_then_callback(client)
        assert "shopify_pending" in client.session
        client.get("/api/v1/auth/login")
        token = client.cookies["csrftoken"].value
        resp = client.post(
            "/api/v1/auth/login",
            {"email": owner.user.email, "password": "Tr1cky-Horse-Battery-Staple!"},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
    assert resp.json() == {"totp_required": True}
    assert client.session.get("shopify_pending") is not None


# --- Code-review follow-ups ---------------------------------------------------


def test_callback_is_throttled_per_ip(client, monkeypatch):
    """The callback reuses WebhookIpRateThrottle, like /install."""
    from rest_framework.settings import api_settings as drf_api_settings

    from integrations.throttling import WebhookIpRateThrottle

    merged = {**drf_api_settings.DEFAULT_THROTTLE_RATES, "webhook_ip": "1/min"}
    monkeypatch.setattr(WebhookIpRateThrottle, "THROTTLE_RATES", merged)
    with _override():
        first = client.get(CALLBACK_URL, _callback_query("no-such-state"))
        second = client.get(CALLBACK_URL, _callback_query("no-such-state"))
    assert first.status_code == 400  # allowed through, then rejected
    assert second.status_code == 429
    assert "Retry-After" in second


def test_flush_keeps_pending_for_anonymous_session(client):
    session = client.session
    session["shopify_pending"] = {"shop": SHOP, "credentials_fernet": "x", "issued_at": "t"}
    session["other"] = "gone"
    session.save()
    shopify_services.flush_session_keeping_pending(session, is_authenticated=False)
    assert session["shopify_pending"]["shop"] == SHOP
    assert "other" not in session


def test_flush_drops_pending_for_authenticated_session(client):
    session = client.session
    session["shopify_pending"] = {"shop": SHOP, "credentials_fernet": "x", "issued_at": "t"}
    session.save()
    shopify_services.flush_session_keeping_pending(session, is_authenticated=True)
    assert "shopify_pending" not in session


def test_connect_not_supported_field_errors_are_per_instance():
    from integrations.exceptions import ShopifyConnectNotSupported

    first, second = ShopifyConnectNotSupported(), ShopifyConnectNotSupported()
    assert first.field_errors == {
        "provider": ["Shopify must be connected through the Shopify OAuth installation/linking flow."]
    }
    assert first.field_errors is not second.field_errors
    first.field_errors["provider"].append("mutated")
    assert second.field_errors["provider"] == [
        "Shopify must be connected through the Shopify OAuth installation/linking flow."
    ]
    assert "field_errors" not in ShopifyConnectNotSupported.__dict__


# --- Final pre-PR audit: previously unproven DoD items ------------------------


def test_install_is_throttled_per_ip(client, monkeypatch):
    """/install reuses WebhookIpRateThrottle: the (N+1)th request gets 429
    with Retry-After, and no new OAuth state is issued for it."""
    from rest_framework.settings import api_settings as drf_api_settings

    from integrations.throttling import WebhookIpRateThrottle

    merged = {**drf_api_settings.DEFAULT_THROTTLE_RATES, "webhook_ip": "1/min"}
    monkeypatch.setattr(WebhookIpRateThrottle, "THROTTLE_RATES", merged)
    with _override():
        first = client.get(INSTALL_URL, {"shop": SHOP})
        state_after_first = client.session["shopify_oauth"]["state"]
        second = client.get(INSTALL_URL, {"shop": SHOP})
    assert first.status_code == 302
    assert second.status_code == 429
    assert "Retry-After" in second
    assert client.session["shopify_oauth"]["state"] == state_after_first


class _ProbeDuringRegistration(FakeShopify):
    """While the link request's provisional Integration is inserted but not
    yet committed (inside the first webhookSubscriptionCreate call),
    deliver a signed webhook to its uri from ANOTHER thread -- which uses
    another database connection -- and record the response."""

    def __init__(self):
        super().__init__()
        self.probe_status = None
        self.probe_path = None

    def __call__(self, url, data, headers, *, timeout=10):
        if self.probe_path is None and b"webhookSubscriptionCreate" in data:
            uri = json.loads(data)["variables"]["webhookSubscription"]["uri"]
            self.probe_path = urlsplit(uri).path

            def _probe():
                from django.db import connection

                try:
                    body = json.dumps({"id": 1}).encode()
                    self.probe_status = _deliver(APIClient(), self.probe_path, body, webhook_id="wh-probe").status_code
                finally:
                    connection.close()

            thread = threading.Thread(target=_probe)
            thread.start()
            thread.join()
        return super().__call__(url, data, headers, timeout=timeout)


def test_provisional_integration_is_invisible_until_link_commits(client, make_merchant, session_client, monkeypatch):
    fake = _ProbeDuringRegistration()
    monkeypatch.setattr(shopify_services, "_shopify_post", fake)
    owner = make_merchant("A")
    with _override():
        _install_then_callback(client)
        link = _link_as_owner(client, session_client(owner.user.email))
        assert link.status_code == 201, link.content
        # The same uri, after the link request committed. A verified
        # non-orders/paid topic returns 200 and stores nothing.
        after = _deliver(
            APIClient(), fake.probe_path, json.dumps({"id": 1}).encode(), webhook_id="wh-after", topic="refunds/create"
        )

    # On a second connection, while the link transaction was still open,
    # the Integration did not exist yet: the identical 401.
    assert fake.probe_status == 401
    assert fake.probe_path == f"/api/v1/webhooks/shopify/{link.json()['id']}"
    # Once committed, the same Integration is found and verifies.
    assert after.status_code == 200


def test_merchant_a_cannot_use_merchant_b_integration_for_same_shop(
    client, fake_shopify, make_merchant, session_client, django_capture_on_commit_callbacks
):
    """Merchant B already has this shop connected. Merchant A installs and
    links the same shop: A gets its OWN Integration, B's is untouched, A's
    session cannot read, edit or disconnect B's, and a delivery to A's URL
    never writes under B."""
    from events.models import IntegrationEvent

    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        b_integration = Integration.objects.create(
            merchant_id=owner_b.merchant_id,
            provider=Integration.Provider.SHOPIFY,
            status=Integration.Status.CONNECTED,
            config_json={"shop_domain": SHOP, "shop_id": "820982911946154508", "webhook_subscription_ids": []},
        )
        b_before = (b_integration.status, b_integration.config_json, b_integration.updated_at)
        b_audit_before = AuditLog.objects.count()

    with open("integrations/tests/fixtures/shopify_orders_paid_2026-07.json", encoding="utf-8") as f:
        order_body = f.read().encode()

    with _override():
        _install_then_callback(client)
        a_client = session_client(owner_a.user.email)
        link = _link_as_owner(client, a_client)
        assert link.status_code == 201, link.content
        a_integration_id = link.json()["id"]

        # A's session cannot see or reach B's Integration.
        listed = [row["id"] for row in a_client.get("/api/v1/integrations").json()["results"]]
        assert listed == [a_integration_id]
        b_url = f"/api/v1/integrations/{b_integration.id}"
        assert (
            a_client.patch(b_url, {"config_json": {}}, format="json", HTTP_X_CSRFTOKEN=a_client.csrf).status_code
            == 404
        )
        assert a_client.delete(b_url, HTTP_X_CSRFTOKEN=a_client.csrf).status_code == 404

        # A delivery to A's URL is recorded under A only.
        with django_capture_on_commit_callbacks(execute=True):
            delivery = _deliver(APIClient(), f"/api/v1/webhooks/shopify/{a_integration_id}", order_body, webhook_id="wh-a")
    assert delivery.status_code == 200

    assert a_integration_id != str(b_integration.id)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        a_integration = Integration.objects.get(pk=a_integration_id)
        assert a_integration.merchant_id == owner_a.merchant_id
        assert IntegrationEvent.objects.filter(integration_id=a_integration_id).count() == 1
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        b_integration.refresh_from_db()
        assert (b_integration.status, b_integration.config_json, b_integration.updated_at) == b_before
        assert not IntegrationEvent.objects.exists()
        assert AuditLog.objects.count() == b_audit_before


def test_install_flow_logs_contain_no_secret_token_code_or_body(client, make_merchant, session_client, monkeypatch, caplog):
    """caplog across install, a rejected callback, a successful callback,
    link, and a registration failure with a failing delete: no record
    carries the client secret, a token, the OAuth code, the state, an
    hmac, or a Shopify request/response body."""
    owner = make_merchant("A")
    owner2 = make_merchant("B")
    captured_bodies = []

    class _Recording(FakeShopify):
        def __call__(self, url, data, headers, *, timeout=10):
            captured_bodies.append(data.decode())
            result = super().__call__(url, data, headers, timeout=timeout)
            captured_bodies.append(json.dumps(result))
            return result

    monkeypatch.setattr(shopify_services, "_shopify_post", _Recording())
    with caplog.at_level("DEBUG"):
        with _override():
            install = client.get(INSTALL_URL, {"shop": SHOP})
            state = parse_qs(urlsplit(install["Location"]).query)["state"][0]
            bad_query = _callback_query(state, code="bad-code")
            bad_query["hmac"] = "0" * 64
            rejected = client.get(CALLBACK_URL, bad_query)
            # The hmac check runs before the state is popped, so the real
            # callback with the same state still succeeds.
            good_query = _callback_query(state)
            callback = client.get(CALLBACK_URL, good_query)
            link = _link_as_owner(client, session_client(owner.user.email))

            monkeypatch.setattr(shopify_services, "_shopify_post", _Recording(fail_on="second_create", fail_delete=True))
            client2 = type(client)()
            _install_then_callback(client2)
            failed_link = _link_as_owner(client2, session_client(owner2.user.email))

    assert (rejected.status_code, callback.status_code, link.status_code, failed_link.status_code) == (400, 302, 201, 502)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    forbidden = [
        CLIENT_SECRET,
        "shpat_test_token",
        "shprt_test_token",
        "test-code",
        "bad-code",
        state,
        good_query["hmac"],
    ]
    for value in forbidden:
        assert value not in logged, f"{value!r} leaked into logs"
    # No Shopify request or response body was logged.
    assert captured_bodies
    for body in captured_bodies:
        assert body not in logged
    assert "webhookSubscriptionCreate" not in logged
    assert "myshopifyDomain" not in logged
