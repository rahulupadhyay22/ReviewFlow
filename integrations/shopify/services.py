"""Shopify install/OAuth/link services (spec .claude/specs/06-shopify-app.md
Decision 3, merchant flow steps 1-8). Session access lives ONLY here --
views pass request.session in and never interpret its contents.

No merchant is ever read from a Shopify parameter (shop, code, hmac,
state). The merchant always comes from link_shopify_installation()'s
caller, i.e. the authenticated OWNER/ADMIN session that calls
POST /integrations/shopify/link.
"""
import hashlib
import hmac
import http.client
import json
import logging
import re
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone as dj_timezone

from auditlog.services import record
from core.crypto import decrypt, encrypt
from core.tenancy import get_current_merchant_id
from integrations.exceptions import ShopifyInstallExpired, ShopifyInstallInvalid, ShopifyUnavailable
from integrations.models import Integration

logger = logging.getLogger(__name__)

_SHOP_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")
_SHOP_GID_RE = re.compile(r"^gid://shopify/Shop/(\d+)$")
_STATE_MAX_AGE = timedelta(minutes=10)
_PENDING_MAX_AGE = timedelta(minutes=15)
_HTTP_TIMEOUT = 10  # seconds; Shopify allows up to a 5s delivery timeout on webhooks (F14), but this is an outbound admin-api call, not a delivery response

AUDIT_CONNECTED = "integration.connected"


def _now_iso() -> str:
    return dj_timezone.now().isoformat()


def _age(issued_at_iso: str) -> timedelta:
    return dj_timezone.now() - datetime.fromisoformat(issued_at_iso)


def _validate_shop(shop: str) -> None:
    if not shop or not _SHOP_RE.match(shop):
        raise ShopifyInstallInvalid()


def _shopify_post(url: str, data: bytes, headers: dict, *, timeout: int = _HTTP_TIMEOUT) -> dict:
    """One small outbound-call helper (stdlib urllib.request, explicit
    timeout -- no new dependency). Never logs the request body, the
    response body, tokens, codes or the client secret -- only the URL's
    host on failure, and the exception class."""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise ShopifyUnavailable()
            body = resp.read()
    # http.client.HTTPException (e.g. IncompleteRead on a truncated response)
    # is not an OSError. Left unconverted it would skip register_webhooks'
    # created_ids bookkeeping, and an already-created subscription would
    # never be cleaned up (Decision 3, step 8).
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        logger.warning("Shopify request to %s failed with %s", urlsplit(url).netloc, type(exc).__name__)
        raise ShopifyUnavailable() from None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        raise ShopifyUnavailable() from None


def _graphql(shop: str, access_token: str, query: str, variables: dict | None = None) -> dict:
    url = f"https://{shop}/admin/api/{settings.SHOPIFY_API_VERSION}/graphql.json"
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    result = _shopify_post(url, body, headers)
    if result.get("errors"):
        raise ShopifyUnavailable()
    return result.get("data") or {}


def begin_shopify_install(*, session, shop: str) -> str:
    """GET /integrations/shopify/install (spec Decision 3, merchant flow
    step 1). Unauthenticated, pre-tenant, and touches no tenant data."""
    _validate_shop(shop)
    state = secrets.token_urlsafe(32)
    session["shopify_oauth"] = {"state": state, "shop": shop, "issued_at": _now_iso()}
    query = urlencode(
        {
            "client_id": settings.SHOPIFY_CLIENT_ID,
            "scope": "read_orders",
            "redirect_uri": settings.SHOPIFY_REDIRECT_URI,
            "state": state,
        }
    )
    return f"https://{shop}/admin/oauth/authorize?{query}"


def verify_shopify_query_hmac(query: dict) -> bool:
    """The documented callback hmac check (F8; docs/05-integrations/Shopify.md
    "V-a"): remove hmac, sort the rest, join as k=v with &, HMAC-SHA256
    (hex) with the client secret, constant-time compare. Accepts
    SHOPIFY_CLIENT_SECRET_PREVIOUS too during a rotation (F11)."""
    provided = query.get("hmac", "")
    if not provided:
        return False
    pairs = sorted((k, v) for k, v in query.items() if k != "hmac")
    message = "&".join(f"{k}={v}" for k, v in pairs).encode()
    for secret in (settings.SHOPIFY_CLIENT_SECRET, settings.SHOPIFY_CLIENT_SECRET_PREVIOUS):
        if not secret:
            continue
        digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
        try:
            if hmac.compare_digest(digest, provided):
                return True
        except TypeError:
            continue
    return False


def exchange_code(shop: str, code: str) -> dict:
    """POST https://{shop}/admin/oauth/access_token, form-urlencoded (F9;
    Shopify.md "V-b"). Returns the raw token bundle: access_token,
    refresh_token, expires_in, refresh_token_expires_in, scope."""
    body = urlencode(
        {
            "client_id": settings.SHOPIFY_CLIENT_ID,
            "client_secret": settings.SHOPIFY_CLIENT_SECRET,
            "code": code,
            "expiring": "1",
        }
    ).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    result = _shopify_post(f"https://{shop}/admin/oauth/access_token", body, headers)
    if not result.get("access_token"):
        raise ShopifyUnavailable()
    return result


def complete_shopify_oauth(*, session, query: dict) -> None:
    """GET /integrations/shopify/callback (spec Decision 3, merchant flow
    step 3). Never reads request.merchant_id, the session merchant or any
    callback parameter to decide a merchant, and creates NO Integration."""
    # [Shopify] documented checks first (F8) -- hmac uses only the query,
    # not the session.
    if not verify_shopify_query_hmac(query):
        raise ShopifyInstallInvalid()

    # [ReviewFlow] the nonce is popped here, so it is used at most once
    # whatever the outcome below.
    nonce = session.pop("shopify_oauth", None)
    if nonce is None:
        raise ShopifyInstallInvalid()
    if _age(nonce["issued_at"]) > _STATE_MAX_AGE:
        raise ShopifyInstallInvalid()
    if not hmac.compare_digest(nonce["state"], query.get("state", "")):
        raise ShopifyInstallInvalid()
    shop = query.get("shop", "")
    if nonce["shop"] != shop:
        raise ShopifyInstallInvalid()
    _validate_shop(shop)

    # [Shopify] token exchange (F9).
    bundle = exchange_code(shop, query.get("code", ""))
    scope = bundle.get("scope") or ""
    if "read_orders" not in scope.split(","):
        raise ShopifyInstallInvalid()

    now = dj_timezone.now()
    credentials = {
        "access_token": bundle["access_token"],
        "access_token_expires_at": (now + timedelta(seconds=int(bundle.get("expires_in", 0)))).isoformat(),
        "refresh_token": bundle.get("refresh_token"),
        "refresh_token_expires_at": (
            now + timedelta(seconds=int(bundle["refresh_token_expires_in"]))
        ).isoformat()
        if bundle.get("refresh_token_expires_in")
        else None,
        "scope": scope,
    }
    session["shopify_pending"] = {
        "shop": shop,
        "credentials_fernet": encrypt(json.dumps(credentials)),
        "issued_at": _now_iso(),
    }


def flush_session_keeping_pending(session, *, is_authenticated: bool) -> None:
    """Flushes the session, but carries shopify_pending across the flush
    when the session had NO authenticated user (implementation-plan
    decision, 2026-09-29). This mirrors login()'s own behavior for an
    anonymous session, which keeps data and only rotates the key. A session
    logged in as a different user gets the full flush, so the merchant must
    reinstall (Decision 3, merchant flow step 4)."""
    pending = None if is_authenticated else session.get("shopify_pending")
    session.flush()
    if pending is not None:
        session["shopify_pending"] = pending


def pending_shopify_shop(*, session) -> str | None:
    """GET /integrations/shopify/pending. Read-only -- does not pop, so a
    reload of the link page still shows the confirmation."""
    pending = session.get("shopify_pending")
    if not pending or _age(pending["issued_at"]) > _PENDING_MAX_AGE:
        return None
    return pending["shop"]


def fetch_shop_identity(shop: str, access_token: str) -> str:
    """Decision 3, step 5a. Runs BEFORE the provisional insert. Returns
    the numeric shop_id (digit string), which equals the REST resource id
    (F26) carried in the app/uninstalled Shop payload's id (F25)."""
    data = _graphql(shop, access_token, "{ shop { id myshopifyDomain } }")
    shop_data = data.get("shop") or {}
    if shop_data.get("myshopifyDomain") != shop:
        raise ShopifyInstallInvalid()
    match = _SHOP_GID_RE.match(shop_data.get("id") or "")
    if not match:
        raise ShopifyInstallInvalid()
    return match.group(1)


_CREATE_SUBSCRIPTION = """
mutation webhookSubscriptionCreate($topic: WebhookSubscriptionTopic!, $webhookSubscription: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $webhookSubscription) {
    webhookSubscription { id }
    userErrors { field message }
  }
}
"""

_DELETE_SUBSCRIPTION = """
mutation webhookSubscriptionDelete($id: ID!) {
  webhookSubscriptionDelete(id: $id) {
    deletedWebhookSubscriptionId
    userErrors { field message }
  }
}
"""


def register_webhooks(shop: str, access_token: str, integration_id) -> list[str]:
    """Shop-specific ORDERS_PAID and APP_UNINSTALLED subscriptions (F12,
    F16; Decision 3 step 6). On failure, raises ShopifyUnavailable with
    .created_ids set to whatever subscriptions WERE created, so the
    caller can best-effort clean them up (step 8)."""
    uri = f"{settings.SHOPIFY_WEBHOOK_BASE}/api/v1/webhooks/shopify/{integration_id}"
    created_ids: list[str] = []
    for topic in ("ORDERS_PAID", "APP_UNINSTALLED"):
        try:
            data = _graphql(
                shop,
                access_token,
                _CREATE_SUBSCRIPTION,
                {"topic": topic, "webhookSubscription": {"uri": uri}},
            )
            payload = data.get("webhookSubscriptionCreate") or {}
            if payload.get("userErrors"):
                raise ShopifyUnavailable()
            subscription = payload.get("webhookSubscription") or {}
            subscription_id = subscription.get("id")
            if not subscription_id:
                raise ShopifyUnavailable()
        except ShopifyUnavailable as exc:
            exc.created_ids = created_ids
            raise
        created_ids.append(subscription_id)
    return created_ids


def delete_webhooks(shop: str, access_token: str, subscription_ids: list[str]) -> None:
    """Best-effort cleanup (F23) on a registration failure. Each delete
    gets its own try/except; a failure is logged with the subscription id
    and the exception class only (never the token or a response body)."""
    for subscription_id in subscription_ids:
        try:
            data = _graphql(shop, access_token, _DELETE_SUBSCRIPTION, {"id": subscription_id})
            if (data.get("webhookSubscriptionDelete") or {}).get("userErrors"):
                logger.warning("Shopify webhookSubscriptionDelete userErrors for %s", subscription_id)
        except Exception as exc:  # noqa: BLE001 -- best-effort, never propagates
            logger.warning(
                "Shopify webhookSubscriptionDelete failed for %s with %s",
                subscription_id,
                type(exc).__name__,
            )


def link_shopify_installation(*, actor, session) -> Integration:
    """POST /integrations/shopify/link (spec Decision 3, merchant flow
    steps 5-8). Runs under the session merchant's tenant context, already
    opened by TenantMiddleware for this request -- this function opens
    only its own inner SAVEPOINT via transaction.atomic()."""
    pending = session.pop("shopify_pending", None)
    if pending is None or _age(pending["issued_at"]) > _PENDING_MAX_AGE:
        raise ShopifyInstallExpired()

    shop = pending["shop"]
    credentials = json.loads(decrypt(pending["credentials_fernet"]))
    access_token = credentials["access_token"]

    # Step 5a: resolve the shop identity BEFORE any insert -- no
    # subscription exists yet, and nothing is stored on failure.
    shop_id = fetch_shop_identity(shop, access_token)

    try:
        with transaction.atomic():
            # Provisional: uncommitted until this whole savepoint block
            # exits normally, so a receiver lookup on another connection
            # sees nothing and returns 401 (Decision 1) while this is open.
            integration = Integration.objects.create(
                merchant_id=get_current_merchant_id(),
                provider=Integration.Provider.SHOPIFY,
                status=Integration.Status.CONNECTED,
                credentials_encrypted=encrypt(json.dumps(credentials)),
                config_json={"shop_domain": shop, "shop_id": shop_id},
            )
            subscription_ids = register_webhooks(shop, access_token, integration.id)
            integration.config_json["webhook_subscription_ids"] = subscription_ids
            integration.save(update_fields=["config_json", "updated_at"])
            record(
                AUDIT_CONNECTED,
                actor=actor.user,
                target=integration,
                metadata={"provider": "shopify", "shop_domain": shop},
            )
    except Exception as exc:
        # The `with transaction.atomic():` block above has already rolled
        # back the savepoint on this exception -- neither the provisional
        # Integration nor its audit row exists. Cleanup below is a
        # best-effort HTTP side effect, independent of that rollback.
        created_ids = getattr(exc, "created_ids", [])
        delete_webhooks(shop, access_token, created_ids)
        raise ShopifyUnavailable() from exc

    return integration
