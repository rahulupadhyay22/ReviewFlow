"""Unit tests for integrations/core: SaleCreated validation and the
provider registry (no database needed)."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from integrations.core.adapters import BaseAdapter
from integrations.core.events import SaleCreated
from integrations.core.registry import ADAPTERS, AdapterNotFound, get_adapter, is_registered
from integrations.core.schemas import PayloadValidationError

OCCURRED_AT = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)


def _sale(**overrides):
    fields = {
        "source": "webhook",
        "external_transaction_id": "INV-1",
        "amount": Decimal("100.00"),
        "currency": "INR",
        "occurred_at": OCCURRED_AT,
    }
    fields.update(overrides)
    return SaleCreated(**fields)


def test_sale_created_with_valid_phone():
    sale = _sale(customer_phone="+919999999999")
    assert sale.customer_phone == "+919999999999"


def test_sale_created_blank_phone_becomes_none():
    assert _sale(customer_phone="").customer_phone is None


def test_sale_created_whitespace_phone_becomes_none():
    assert _sale(customer_phone="   ").customer_phone is None


def test_sale_created_missing_phone_is_none_by_default():
    assert _sale().customer_phone is None


def test_sale_created_invalid_phone_raises_invalid_phone():
    with pytest.raises(PayloadValidationError) as exc_info:
        _sale(customer_phone="98765")
    assert exc_info.value.code == "INVALID_PHONE"


def test_sale_created_missing_external_transaction_id_raises():
    with pytest.raises(PayloadValidationError) as exc_info:
        _sale(external_transaction_id="")
    assert exc_info.value.code == "INVALID_EXTERNAL_TRANSACTION_ID"


def test_sale_created_over_length_external_transaction_id_raises():
    with pytest.raises(PayloadValidationError) as exc_info:
        _sale(external_transaction_id="x" * 256)
    assert exc_info.value.code == "INVALID_EXTERNAL_TRANSACTION_ID"


def test_sale_created_negative_amount_raises_invalid_amount():
    with pytest.raises(PayloadValidationError) as exc_info:
        _sale(amount=Decimal("-1"))
    assert exc_info.value.code == "INVALID_AMOUNT"


def test_sale_created_missing_amount_raises_invalid_amount():
    with pytest.raises(PayloadValidationError) as exc_info:
        _sale(amount=None)
    assert exc_info.value.code == "INVALID_AMOUNT"


def test_sale_created_amount_too_many_decimal_places_raises_payload_invalid():
    with pytest.raises(PayloadValidationError) as exc_info:
        _sale(amount=Decimal("1.005"))
    assert exc_info.value.code == "PAYLOAD_INVALID"


def test_sale_created_amount_too_many_integer_digits_raises_payload_invalid():
    with pytest.raises(PayloadValidationError) as exc_info:
        _sale(amount=Decimal("12345678901.00"))  # 11 integer digits > 10 allowed
    assert exc_info.value.code == "PAYLOAD_INVALID"


def test_sale_created_amount_at_precision_limit_is_valid():
    sale = _sale(amount=Decimal("1234567890.12"))  # 10 int digits, 2 decimals
    assert sale.amount == Decimal("1234567890.12")


def test_sale_created_invalid_currency_raises():
    with pytest.raises(PayloadValidationError) as exc_info:
        _sale(currency="inr")
    assert exc_info.value.code == "INVALID_CURRENCY"


def test_sale_created_naive_occurred_at_raises():
    with pytest.raises(PayloadValidationError) as exc_info:
        _sale(occurred_at=datetime(2024, 1, 1, 10, 0))
    assert exc_info.value.code == "INVALID_OCCURRED_AT"


def test_sale_created_missing_occurred_at_raises():
    with pytest.raises(PayloadValidationError) as exc_info:
        _sale(occurred_at=None)
    assert exc_info.value.code == "INVALID_OCCURRED_AT"


def test_sale_created_over_length_customer_name_raises_payload_invalid():
    with pytest.raises(PayloadValidationError) as exc_info:
        _sale(customer_name="x" * 256)
    assert exc_info.value.code == "PAYLOAD_INVALID"


def test_sale_created_over_length_payment_method_raises_payload_invalid():
    with pytest.raises(PayloadValidationError) as exc_info:
        _sale(payment_method="x" * 33)
    assert exc_info.value.code == "PAYLOAD_INVALID"


def test_sale_created_never_carries_merchant_or_location_id():
    sale = _sale()
    assert not hasattr(sale, "merchant_id")
    assert not hasattr(sale, "location_id")
    assert hasattr(sale, "external_location_id")


def test_payload_validation_error_unrecognized_code_is_stored_verbatim():
    # events.services._safe_error is responsible for mapping an unrecognized
    # code to PAYLOAD_INVALID -- PayloadValidationError itself just carries
    # whatever code it was given.
    err = PayloadValidationError("SOMETHING_UNEXPECTED")
    assert err.code == "SOMETHING_UNEXPECTED"


class _NoOpAdapter(BaseAdapter):
    def verify(self, request):
        return True

    def parse(self, payload):
        return payload

    def normalize(self, parsed):
        return _sale()


def test_registry_is_registered_false_by_default():
    assert is_registered("shopify") is False


def test_registry_get_adapter_raises_when_unregistered():
    with pytest.raises(AdapterNotFound):
        get_adapter(type("FakeIntegration", (), {"provider": "shopify"})())


def test_registry_get_adapter_returns_instance_when_registered(monkeypatch):
    monkeypatch.setitem(ADAPTERS, "webhook", _NoOpAdapter)
    integration = type("FakeIntegration", (), {"provider": "webhook"})()
    adapter = get_adapter(integration)
    assert isinstance(adapter, _NoOpAdapter)
    assert adapter.integration is integration
