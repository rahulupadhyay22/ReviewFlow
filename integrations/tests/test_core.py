"""Unit tests for integrations/core: SaleCreated validation and the
provider registry (no database needed)."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from integrations.core.adapters import BaseAdapter
from integrations.core.events import SaleCreated
from integrations.core.registry import ADAPTERS, AdapterNotFound, get_adapter, is_registered
from integrations.core.schemas import (
    PayloadValidationError,
    parse_amount,
    parse_occurred_at,
    resolve_path,
    sale_event_key,
    verify_hmac_sha256,
)

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

    def get_external_event_id(self, request, payload):
        return "1"

    def parse(self, payload):
        return payload

    def normalize(self, parsed):
        return _sale()


def test_registry_is_registered_false_by_default():
    # woocommerce is Phase 17 scope (spec 06 "The nine V1 integrations, and
    # which ship here") -- it is never registered by Phase 06.
    assert is_registered("woocommerce") is False


def test_registry_get_adapter_raises_when_unregistered():
    with pytest.raises(AdapterNotFound):
        get_adapter(type("FakeIntegration", (), {"provider": "woocommerce"})())


def test_registry_get_adapter_returns_instance_when_registered(monkeypatch):
    monkeypatch.setitem(ADAPTERS, "webhook", _NoOpAdapter)
    integration = type("FakeIntegration", (), {"provider": "webhook"})()
    adapter = get_adapter(integration)
    assert isinstance(adapter, _NoOpAdapter)
    assert adapter.integration is integration


def test_base_adapter_uninstall_payload_matches_defaults_false():
    """Fail-closed default (spec 06 Decision 6): an adapter that never
    handles an uninstall topic can never authorize one."""
    assert _NoOpAdapter.__mro__  # sanity
    adapter = _NoOpAdapter(integration=None)
    assert adapter.uninstall_payload_matches({"id": 1}) is False


def test_base_adapter_is_uninstall_event_defaults_false():
    adapter = _NoOpAdapter(integration=None)
    assert adapter.is_uninstall_event(request=None) is False


# --- schemas.py Phase 06 helpers ------------------------------------------


def test_parse_amount_accepts_string_and_numeric():
    assert parse_amount("10.50") == Decimal("10.50")
    assert parse_amount(10) == Decimal("10")


@pytest.mark.parametrize("bad", [None, "", "not-a-number", "  "])
def test_parse_amount_rejects_bad_values(bad):
    with pytest.raises(PayloadValidationError) as exc_info:
        parse_amount(bad)
    assert exc_info.value.code == "INVALID_AMOUNT"


def test_parse_occurred_at_accepts_iso_with_z_suffix():
    dt = parse_occurred_at("2024-01-01T10:00:00Z")
    assert dt.tzinfo is not None


def test_parse_occurred_at_accepts_iso_with_offset():
    dt = parse_occurred_at("2024-01-01T10:00:00+05:30")
    assert dt.tzinfo is not None


@pytest.mark.parametrize("bad", [None, "not-a-date", ""])
def test_parse_occurred_at_rejects_bad_values(bad):
    with pytest.raises(PayloadValidationError) as exc_info:
        parse_occurred_at(bad)
    assert exc_info.value.code == "INVALID_OCCURRED_AT"


def test_sale_event_key_is_deterministic_64_hex():
    key1 = sale_event_key("loc-1", "INV-1")
    key2 = sale_event_key("loc-1", "INV-1")
    assert key1 == key2
    assert len(key1) == 64
    assert all(c in "0123456789abcdef" for c in key1)


def test_sale_event_key_differs_by_location():
    key_a = sale_event_key("loc-1", "INV-1")
    key_b = sale_event_key("loc-2", "INV-1")
    assert key_a != key_b


def test_sale_event_key_handles_none_location():
    key1 = sale_event_key(None, "INV-1")
    key2 = sale_event_key(None, "INV-1")
    assert key1 == key2


def test_resolve_path_resolves_nested_dict_and_list():
    obj = {"order": {"items": [{"sku": "A"}, {"sku": "B"}]}}
    assert resolve_path(obj, "order.items.1.sku") == "B"


def test_resolve_path_returns_none_for_missing_segment():
    assert resolve_path({"a": {"b": 1}}, "a.c") is None
    assert resolve_path({"a": [1, 2]}, "a.5") is None
    assert resolve_path({"a": 1}, "a.b") is None


def test_verify_hmac_sha256_hex_roundtrip():
    import hashlib
    import hmac as hmac_mod

    secret = "s3cr3t"
    body = b'{"a":1}'
    sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_hmac_sha256(secret, body, sig, encoding="hex") is True
    assert verify_hmac_sha256(secret, body, "wrong", encoding="hex") is False


def test_verify_hmac_sha256_base64_roundtrip():
    import base64
    import hashlib
    import hmac as hmac_mod

    secret = "s3cr3t"
    body = b'{"a":1}'
    sig = base64.b64encode(hmac_mod.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    assert verify_hmac_sha256(secret, body, sig, encoding="base64") is True


def test_verify_hmac_sha256_fails_closed_on_missing_signature():
    assert verify_hmac_sha256("secret", b"body", "") is False
    assert verify_hmac_sha256("secret", b"body", None) is False
    assert verify_hmac_sha256("", b"body", "sig") is False
