"""ShopifyAdapter: the ReviewFlow Shopify app's orders/paid ingestion and
app/uninstalled routing (spec .claude/specs/06-shopify-app.md Decisions 2,
3, 6, 17; parent spec .claude/specs/06-priority-integrations.md Decision
3). See docs/05-integrations/Shopify.md for the findings (F1-F27), the
field-guarantee table and the E.164 resolution this adapter implements.

HMAC verification uses the PLATFORM app client secret
(settings.SHOPIFY_CLIENT_SECRET[_PREVIOUS]) -- never a per-merchant
credential. Integration.credentials_encrypted holds only the OAuth token
bundle and is never read by verify().
"""
from django.conf import settings
from django.core.exceptions import ValidationError

from integrations.core.adapters import BaseAdapter
from integrations.core.events import SaleCreated
from integrations.core.schemas import (
    PayloadValidationError,
    parse_amount,
    parse_occurred_at,
    verify_hmac_sha256,
)
from integrations.exceptions import WebhookRejected

_TOPIC_ORDERS_PAID = "orders/paid"
_TOPIC_APP_UNINSTALLED = "app/uninstalled"

# The Order-resource keys that can never appear on a genuine app/uninstalled
# (Shop-resource) body -- the Phase 06 current-topic discriminator (Decision
# 3, step 2b; docs/05-integrations/Shopify.md F25).
_ORDER_ONLY_KEYS = ("line_items", "order_number", "financial_status", "total_price")


class ShopifyAdapter(BaseAdapter):
    def verify(self, request) -> bool:
        signature = request.headers.get("X-Shopify-Hmac-Sha256", "")
        if verify_hmac_sha256(settings.SHOPIFY_CLIENT_SECRET, request.body, signature, encoding="base64"):
            pass_hmac = True
        elif settings.SHOPIFY_CLIENT_SECRET_PREVIOUS and verify_hmac_sha256(
            settings.SHOPIFY_CLIENT_SECRET_PREVIOUS, request.body, signature, encoding="base64"
        ):
            pass_hmac = True
        else:
            pass_hmac = False
        if not pass_hmac:
            return False
        # Defense in depth only -- X-Shopify-Shop-Domain is NOT covered by
        # the HMAC (it signs the raw body, not headers). Every installed
        # shop's deliveries share the same app secret, so this is what
        # actually binds a delivery to THIS integration's shop.
        shop_domain = request.headers.get("X-Shopify-Shop-Domain", "")
        return shop_domain == (self.integration.config_json or {}).get("shop_domain")

    def is_sale_event(self, request) -> bool:
        return request.headers.get("X-Shopify-Topic") == _TOPIC_ORDERS_PAID

    def is_uninstall_event(self, request) -> bool:
        # Routes only -- an unsigned header can never by itself authorize
        # the destructive disconnect. See uninstall_payload_matches().
        return request.headers.get("X-Shopify-Topic") == _TOPIC_APP_UNINSTALLED

    def uninstall_payload_matches(self, payload: dict) -> bool:
        """The Phase 06 current-topic discriminator (spec Decision 3, step
        2b). It separates the subscribed orders/paid Order payload from
        the app/uninstalled Shop payload and binds the result to this
        integration's stored shop_id. It is NOT a general proof of an
        app/uninstalled payload and does NOT authenticate X-Shopify-Topic
        -- see docs/05-integrations/Shopify.md "Re-evaluation rule": this
        must be re-checked before any new Shopify topic is subscribed."""
        if not isinstance(payload, dict):
            return False
        payload_id = payload.get("id")
        # type(...) is int, not isinstance: isinstance(True, int) is True
        # in Python, so a JSON `true` would otherwise pass this check.
        if type(payload_id) is not int:
            return False
        if any(key in payload for key in _ORDER_ONLY_KEYS):
            return False
        shop_id = (self.integration.config_json or {}).get("shop_id")
        return shop_id is not None and str(payload_id) == shop_id

    def get_external_event_id(self, request, payload: dict) -> str:
        """[User decision 2026-09-29, O1] X-Shopify-Webhook-Id is required
        on BOTH orders/paid and app/uninstalled. A missing/empty header
        raises WebhookRejected directly (401 invalid_signature) -- NOT
        PayloadValidationError -- because a missing dedupe header on a
        Shopify delivery is a verification failure (spec Decision 3
        endpoint block), not a 422 payload defect. It is never
        X-Shopify-Event-Id (F2, F14: that correlates same-action
        deliveries, not delivery-level dedupe)."""
        webhook_id = request.headers.get("X-Shopify-Webhook-Id", "")
        if not webhook_id:
            raise WebhookRejected()
        return webhook_id

    @classmethod
    def validate_connection(cls, credentials: dict | None, config_json: dict | None) -> None:
        """Every PATCH for shopify returns 422: config_json (shop_domain,
        shop_id, webhook_subscription_ids) is entirely server-managed, so
        the stored shop identity the uninstall binding depends on can
        never be edited (spec Decision 6). The generic credentials-based
        connect path never reaches this -- see
        integrations.services.assert_generic_connect_allowed (O2)."""
        raise ValidationError(
            {"config_json": ["The Shopify integration's config is server-managed and cannot be edited."]}
        )

    def parse(self, payload: dict) -> dict:
        """Extracts exactly the Decision 17 field-table fields. No other
        field is read -- in particular, never an address phone
        (billing_address.phone, shipping_address.phone)."""
        customer = payload.get("customer") or {}
        first_name = (customer.get("first_name") or "").strip()
        last_name = (customer.get("last_name") or "").strip()
        gateways = payload.get("payment_gateway_names") or []
        location_id = payload.get("location_id")
        return {
            "id": payload.get("id"),
            "total_price": payload.get("total_price"),
            "currency": payload.get("currency"),
            "processed_at": payload.get("processed_at"),
            "created_at": payload.get("created_at"),
            "phone": (payload.get("phone") or "").strip() or None,
            "customer_phone": (customer.get("phone") or "").strip() or None,
            "customer_name": (f"{first_name} {last_name}".strip() or None),
            "payment_method": (str(gateways[0])[:32] if gateways else None),
            "external_location_id": (str(location_id) if location_id is not None else None),
        }

    def normalize(self, parsed: dict) -> SaleCreated:
        txn_id = parsed.get("id")
        if txn_id is None or str(txn_id) == "":
            raise PayloadValidationError("INVALID_EXTERNAL_TRANSACTION_ID")

        occurred_at = parsed.get("processed_at")
        if occurred_at is None:
            occurred_at = parsed.get("created_at")  # fallback, Decision 17

        return SaleCreated(
            source="shopify",
            external_transaction_id=str(txn_id),
            amount=parse_amount(parsed.get("total_price")),
            currency=str(parsed.get("currency") or ""),
            occurred_at=parse_occurred_at(occurred_at),
            # Order-level phone is primary; customer.phone is the fallback,
            # used only when the order-level value is blank/absent (Decision
            # 17). Neither is reformatted -- see Shopify.md "Phone format":
            # Shopify does not guarantee E.164 on read, so a non-E.164
            # value fails the whole sale (INVALID_PHONE) exactly like any
            # other provider, unchanged.
            customer_phone=parsed.get("phone") or parsed.get("customer_phone"),
            customer_name=parsed.get("customer_name"),
            payment_method=parsed.get("payment_method"),
            external_location_id=parsed.get("external_location_id"),
        )
