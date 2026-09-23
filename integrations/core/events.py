"""SaleCreated: the normalized event every adapter's normalize() produces
(Webhook-Specification.md §"Normalized Event Schema"; spec Decision 4).

It never carries a ReviewFlow merchant_id or location_id -- those always
come from the receiving Integration and from IntegrationLocationMapping,
never from a provider payload (Multi-Tenancy.md; CLAUDE.md "Never trust a
client-supplied merchant_id/location_id").
"""
import dataclasses
from datetime import datetime
from decimal import Decimal

from integrations.core.schemas import PayloadValidationError, validate_currency, validate_e164

_MAX_DIGITS = 12
_DECIMAL_PLACES = 2
_MAX_EXTERNAL_ID_LEN = 255
_MAX_NAME_LEN = 255
_MAX_PAYMENT_METHOD_LEN = 32


def _fits_decimal(value: Decimal, *, max_digits: int, decimal_places: int) -> bool:
    """Mirrors DecimalField(max_digits, decimal_places): at most
    decimal_places digits after the point, and at most
    (max_digits - decimal_places) digits before it."""
    sign, digits, exponent = value.as_tuple()
    if exponent > 0:
        # Trailing zero integer, e.g. 1E+2 == 100 -- no fractional digits.
        int_digits = len(digits) + exponent
        frac_digits = 0
    else:
        frac_digits = -exponent
        int_digits = max(len(digits) - frac_digits, 1)
    if frac_digits > decimal_places:
        return False
    return int_digits <= (max_digits - decimal_places)


@dataclasses.dataclass(frozen=True)
class SaleCreated:
    """The only shape any downstream consumer (location resolution, sale
    recording) ever sees. Every adapter's normalize() returns this."""

    source: str
    external_transaction_id: str
    amount: Decimal
    currency: str
    occurred_at: datetime
    customer_phone: str | None = None
    customer_name: str | None = None
    payment_method: str | None = None
    external_location_id: str | None = None

    def __post_init__(self):
        if not self.external_transaction_id or len(self.external_transaction_id) > _MAX_EXTERNAL_ID_LEN:
            raise PayloadValidationError("INVALID_EXTERNAL_TRANSACTION_ID")

        # Missing phone is not a failure (Decision 17): blank/whitespace-only
        # normalizes to None before E.164 validation ever runs.
        phone = self.customer_phone
        if phone is not None and not phone.strip():
            phone = None
        if phone is not None:
            validate_e164(phone)
        object.__setattr__(self, "customer_phone", phone)

        if self.customer_name is not None and len(self.customer_name) > _MAX_NAME_LEN:
            raise PayloadValidationError("PAYLOAD_INVALID")

        if self.payment_method is not None and len(self.payment_method) > _MAX_PAYMENT_METHOD_LEN:
            raise PayloadValidationError("PAYLOAD_INVALID")

        if self.amount is None or self.amount < 0:
            raise PayloadValidationError("INVALID_AMOUNT")
        if not _fits_decimal(self.amount, max_digits=_MAX_DIGITS, decimal_places=_DECIMAL_PLACES):
            raise PayloadValidationError("PAYLOAD_INVALID")

        validate_currency(self.currency)

        if self.occurred_at is None or self.occurred_at.tzinfo is None:
            raise PayloadValidationError("INVALID_OCCURRED_AT")
