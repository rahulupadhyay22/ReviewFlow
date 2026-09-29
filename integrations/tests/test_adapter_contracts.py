"""Fixture-based adapter contract tests (Testing-Strategy.md §Contract Tests
for Adapters; spec 06 Definition of done, "Adapter contract tests"). Each
anonymized fixture in tests/fixtures/ must normalize, through the adapter
the registry resolves, to an exact hand-written SaleCreated."""
import csv
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from django.test import RequestFactory

from integrations.core.events import SaleCreated
from integrations.core.registry import get_adapter
from integrations.core.schemas import sale_event_key
from integrations.models import Integration

FIXTURES = Path(__file__).parent / "fixtures"
IST = timezone(timedelta(hours=5, minutes=30))

WEBHOOK_CONFIG = {
    "field_map": {
        "external_transaction_id": "order.id",
        "amount": "order.total",
        "currency": "order.currency",
        "occurred_at": "order.completed_at",
        "customer_phone": "customer.phone",
        "customer_name": "customer.name",
        "payment_method": "payment.method",
        "external_location_id": "store.id",
    }
}


def _adapter(provider, config_json=None):
    return get_adapter(Integration(provider=provider, config_json=config_json))


def _normalize(adapter, payload):
    return adapter.normalize(adapter.parse(payload))


def test_generic_webhook_fixture_normalizes_to_exact_sale_created():
    payload = json.loads((FIXTURES / "generic_webhook.json").read_text(encoding="utf-8"))
    adapter = _adapter("webhook", WEBHOOK_CONFIG)

    assert _normalize(adapter, payload) == SaleCreated(
        source="webhook",
        external_transaction_id="POS-100245",
        amount=Decimal("1249.50"),
        currency="INR",
        occurred_at=datetime(2024, 3, 15, 14, 32, 5, tzinfo=IST),
        customer_phone="+919800000001",
        customer_name="Test Customer",
        payment_method="UPI",
        external_location_id="store-blr-01",
    )
    assert adapter.get_external_event_id(None, payload) == sale_event_key("store-blr-01", "POS-100245")


def test_csv_fixture_rows_normalize_to_exact_sale_created():
    with (FIXTURES / "sales.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    adapter = _adapter("csv")

    assert [_normalize(adapter, row) for row in rows] == [
        SaleCreated(
            source="csv",
            external_transaction_id="INV-2024-0001",
            amount=Decimal("899.00"),
            currency="INR",
            occurred_at=datetime(2024, 3, 15, 10, 15, tzinfo=IST),
            customer_phone="+919800000002",
            customer_name="Test Customer",
            payment_method="CARD",
            external_location_id="store-blr-01",
        ),
        # Blank optional cells normalize to None, never "".
        SaleCreated(
            source="csv",
            external_transaction_id="INV-2024-0002",
            amount=Decimal("150.00"),
            currency="INR",
            occurred_at=datetime(2024, 3, 15, 11, 40, tzinfo=IST),
            external_location_id="store-blr-01",
        ),
    ]
    assert adapter.get_external_event_id(None, rows[0]) == sale_event_key("store-blr-01", "INV-2024-0001")


def test_api_fixture_normalizes_to_exact_sale_created_ignoring_tenant_fields():
    payload = json.loads((FIXTURES / "api_sale.json").read_text(encoding="utf-8"))
    adapter = _adapter("api")

    sale = _normalize(adapter, payload)
    assert sale == SaleCreated(
        source="api",
        external_transaction_id="ORD-55012",
        amount=Decimal("2300.00"),
        currency="INR",
        occurred_at=datetime(2024, 3, 15, 18, 5, tzinfo=IST),
        customer_phone="+919800000003",
        customer_name="Test Customer",
        payment_method="CASH",
        external_location_id="store-blr-01",
    )
    # The body's merchant_id/location_id never reach the normalized event.
    assert not hasattr(sale, "merchant_id") and not hasattr(sale, "location_id")
    assert adapter.get_external_event_id(None, payload) == sale_event_key("store-blr-01", "ORD-55012")


@pytest.mark.parametrize("provider", ["csv", "api"])
def test_verify_returns_false_for_providers_without_a_webhook(provider):
    request = RequestFactory().post(
        "/api/v1/webhooks/generic/x",
        data=b"{}",
        content_type="application/json",
        HTTP_X_REVIEWFLOW_SIGNATURE="sha256=" + "0" * 64,
    )
    assert _adapter(provider).verify(request) is False
