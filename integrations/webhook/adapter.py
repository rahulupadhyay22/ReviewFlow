"""GenericWebhookAdapter: any system, with a merchant-defined field mapping
in Integration.config_json (spec 06 Decisions 5, 6, 16 -- "connect almost
any business system" without writing a new adapter per merchant).

Signing: a server-generated whsec_ secret (never merchant-supplied), HMAC
over the raw request body in X-ReviewFlow-Signature: sha256=<hex>.
"""
import json
import secrets

from django.core.exceptions import ValidationError

from core.crypto import decrypt
from integrations.core.adapters import BaseAdapter
from integrations.core.events import SaleCreated
from integrations.core.schemas import (
    PayloadValidationError,
    parse_amount,
    parse_occurred_at,
    resolve_path,
    sale_event_key,
    validate_currency,
    verify_hmac_sha256,
)

_SIG_PREFIX = "sha256="
_REQUIRED_FIELDS = ("external_transaction_id", "amount", "occurred_at")
_OPTIONAL_FIELDS = (
    "currency",
    "customer_phone",
    "customer_name",
    "payment_method",
    "external_location_id",
)


class GenericWebhookAdapter(BaseAdapter):
    def verify(self, request) -> bool:
        signature = request.headers.get("X-ReviewFlow-Signature", "")
        if not signature.startswith(_SIG_PREFIX):
            return False
        secret = self._decrypted_secret()
        if not secret:
            return False
        return verify_hmac_sha256(
            secret, request.body, signature[len(_SIG_PREFIX) :], encoding="hex"
        )

    def _decrypted_secret(self) -> str | None:
        if not self.integration.credentials_encrypted:
            return None
        try:
            creds = json.loads(decrypt(self.integration.credentials_encrypted))
        except Exception:
            return None
        secret = creds.get("webhook_secret") if isinstance(creds, dict) else None
        return secret or None

    @classmethod
    def issue_credentials(cls) -> dict:
        # Server-generated only -- client-supplied credentials are rejected
        # in validate_connection() below (spec Decision 5).
        return {"webhook_secret": "whsec_" + secrets.token_urlsafe(32)}

    @classmethod
    def validate_connection(cls, credentials: dict | None, config_json: dict | None) -> None:
        if credentials:
            raise ValidationError(
                {"credentials": ["This provider does not accept client-supplied credentials."]}
            )
        if not isinstance(config_json, dict):
            raise ValidationError({"config_json": ["field_map is required."]})
        field_map = config_json.get("field_map")
        if not isinstance(field_map, dict):
            raise ValidationError({"config_json": ["field_map is required."]})
        for key, value in field_map.items():
            if not isinstance(value, str) or not value:
                raise ValidationError({"config_json": [f"field_map.{key} must be a non-empty string path."]})
        for key in _REQUIRED_FIELDS:
            if key not in field_map:
                raise ValidationError({"config_json": [f"field_map.{key} is required."]})
        default_currency = config_json.get("default_currency")
        if default_currency is not None and not isinstance(default_currency, str):
            raise ValidationError({"config_json": ["default_currency must be a string."]})
        if "currency" not in field_map and not default_currency:
            raise ValidationError(
                {"config_json": ["field_map.currency or default_currency is required."]}
            )

    def _field_map(self) -> dict:
        return (self.integration.config_json or {}).get("field_map") or {}

    def get_external_event_id(self, request, payload: dict) -> str:
        field_map = self._field_map()
        txn_id = resolve_path(payload, field_map.get("external_transaction_id", ""))
        if txn_id is None or str(txn_id) == "":
            raise PayloadValidationError("INVALID_EXTERNAL_TRANSACTION_ID")
        location_path = field_map.get("external_location_id")
        location_id = resolve_path(payload, location_path) if location_path else None
        return sale_event_key(str(location_id) if location_id is not None else None, str(txn_id))

    def parse(self, payload: dict) -> dict:
        field_map = self._field_map()
        resolved = {
            key: resolve_path(payload, field_map[key]) if key in field_map else None
            for key in (*_REQUIRED_FIELDS, *_OPTIONAL_FIELDS)
        }
        if resolved["currency"] is None:
            resolved["currency"] = (self.integration.config_json or {}).get("default_currency")
        return resolved

    def normalize(self, parsed: dict) -> SaleCreated:
        txn_id = parsed.get("external_transaction_id")
        if txn_id is None or str(txn_id) == "":
            raise PayloadValidationError("INVALID_EXTERNAL_TRANSACTION_ID")
        phone = parsed.get("customer_phone")
        name = parsed.get("customer_name")
        payment_method = parsed.get("payment_method")
        location_id = parsed.get("external_location_id")
        return SaleCreated(
            source="webhook",
            external_transaction_id=str(txn_id),
            amount=parse_amount(parsed.get("amount")),
            currency=validate_currency(str(parsed.get("currency") or "")),
            occurred_at=parse_occurred_at(parsed.get("occurred_at")),
            customer_phone=str(phone) if phone is not None else None,
            customer_name=str(name) if name is not None else None,
            payment_method=str(payment_method) if payment_method is not None else None,
            external_location_id=str(location_id) if location_id is not None else None,
        )
