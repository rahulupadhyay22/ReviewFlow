"""ApiAdapter: the Generic REST API's normalized-shape adapter (spec 06
Decisions 4, 8, 13, 14). POST /sales body is already the SaleCreated shape
(customer nested as {name, phone}); this adapter is the single
normalization path integrations.services.ingest_api_sale and
events.services.process_event both go through -- there is no second,
duplicate parser.
"""
from django.core.exceptions import ValidationError

from integrations.core.adapters import BaseAdapter
from integrations.core.events import SaleCreated
from integrations.core.schemas import (
    PayloadValidationError,
    parse_amount,
    parse_occurred_at,
    sale_event_key,
    validate_currency,
)


def _blank_to_none(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class ApiAdapter(BaseAdapter):
    def verify(self, request) -> bool:
        # No webhook endpoint -- auth is the API key, checked upstream.
        return False

    @classmethod
    def validate_connection(cls, credentials: dict | None, config_json: dict | None) -> None:
        if credentials:
            raise ValidationError(
                {"credentials": ["This provider does not accept client-supplied credentials."]}
            )

    def get_external_event_id(self, request, payload: dict) -> str:
        txn_id = _blank_to_none(payload.get("external_transaction_id"))
        if txn_id is None or not isinstance(txn_id, str):
            raise PayloadValidationError("INVALID_EXTERNAL_TRANSACTION_ID")
        location_id = _blank_to_none(payload.get("external_location_id"))
        return sale_event_key(str(location_id) if location_id is not None else None, txn_id)

    def parse(self, payload: dict) -> dict:
        customer = payload.get("customer") or {}
        if not isinstance(customer, dict):
            customer = {}
        return {
            "external_transaction_id": _blank_to_none(payload.get("external_transaction_id")),
            "amount": payload.get("amount"),
            "currency": payload.get("currency"),
            "occurred_at": payload.get("occurred_at"),
            "customer_phone": _blank_to_none(customer.get("phone")),
            "customer_name": _blank_to_none(customer.get("name")),
            "payment_method": _blank_to_none(payload.get("payment_method")),
            "external_location_id": _blank_to_none(payload.get("external_location_id")),
        }

    def normalize(self, parsed: dict) -> SaleCreated:
        txn_id = parsed.get("external_transaction_id")
        if not txn_id:
            raise PayloadValidationError("INVALID_EXTERNAL_TRANSACTION_ID")
        return SaleCreated(
            source="api",
            external_transaction_id=str(txn_id),
            amount=parse_amount(parsed.get("amount")),
            currency=validate_currency(str(parsed.get("currency") or "")),
            occurred_at=parse_occurred_at(parsed.get("occurred_at")),
            customer_phone=parsed.get("customer_phone"),
            customer_name=parsed.get("customer_name"),
            payment_method=parsed.get("payment_method"),
            external_location_id=parsed.get("external_location_id"),
        )
