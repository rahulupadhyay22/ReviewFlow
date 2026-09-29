"""CsvImportAdapter: a bulk file of past/offline transactions, one row
normalized the same way a single webhook event would be (spec 06
Decision 10; Integration-Architecture.md §"CSV Import Adapter").

Fixed columns, no field mapping. A blank string is treated as missing
(mirrors SaleCreated.__post_init__'s blank-phone rule).
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

REQUIRED_COLUMNS = ("external_transaction_id", "amount", "currency", "occurred_at")
OPTIONAL_COLUMNS = ("customer_phone", "customer_name", "payment_method", "external_location_id")


def _blank_to_none(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


class CsvImportAdapter(BaseAdapter):
    # Exposed as a class attribute (not a module-level import in
    # integrations/services.py) so the required-column check reads it off
    # the adapter the registry resolves, never a second lookup path.
    REQUIRED_COLUMNS = REQUIRED_COLUMNS

    def verify(self, request) -> bool:
        # CSV has no webhook endpoint -- fail closed (spec Decision 6).
        return False

    @classmethod
    def validate_connection(cls, credentials: dict | None, config_json: dict | None) -> None:
        if credentials:
            raise ValidationError(
                {"credentials": ["This provider does not accept client-supplied credentials."]}
            )

    def get_external_event_id(self, request, payload: dict) -> str:
        txn_id = _blank_to_none(payload.get("external_transaction_id"))
        if txn_id is None:
            raise PayloadValidationError("INVALID_EXTERNAL_TRANSACTION_ID")
        location_id = _blank_to_none(payload.get("external_location_id"))
        return sale_event_key(location_id, txn_id)

    def parse(self, payload: dict) -> dict:
        return {
            **{key: _blank_to_none(payload.get(key)) for key in REQUIRED_COLUMNS},
            **{key: _blank_to_none(payload.get(key)) for key in OPTIONAL_COLUMNS},
        }

    def normalize(self, parsed: dict) -> SaleCreated:
        txn_id = parsed.get("external_transaction_id")
        if txn_id is None:
            raise PayloadValidationError("INVALID_EXTERNAL_TRANSACTION_ID")
        return SaleCreated(
            source="csv",
            external_transaction_id=str(txn_id),
            amount=parse_amount(parsed.get("amount")),
            currency=validate_currency(str(parsed.get("currency") or "")),
            occurred_at=parse_occurred_at(parsed.get("occurred_at")),
            customer_phone=parsed.get("customer_phone"),
            customer_name=parsed.get("customer_name"),
            payment_method=parsed.get("payment_method"),
            external_location_id=parsed.get("external_location_id"),
        )
