"""API tests for /transactions (API-Specification.md §Transactions; spec
Definition of done "Transactions API")."""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

import pytest

from core.tenancy import tenant_atomic, tenant_context
from integrations.core.events import SaleCreated
from transactions import services

pytestmark = pytest.mark.django_db

TRANSACTIONS = "/api/v1/transactions"


def _detail(pk):
    return f"{TRANSACTIONS}/{pk}"


def _sale(**overrides):
    fields = dict(
        source="webhook",
        external_transaction_id="INV-1",
        amount=Decimal("100.00"),
        currency="INR",
        occurred_at=datetime(2024, 1, 1, 10, 0, tzinfo=dt_timezone.utc),
    )
    fields.update(overrides)
    return SaleCreated(**fields)


def _record(merchant, integration, location, **sale_kwargs):
    with tenant_context(merchant.id), tenant_atomic():
        txn, _ = services.record_sale(integration=integration, location=location, sale=_sale(**sale_kwargs))
        return txn


def test_list_returns_only_current_merchants_transactions(
    make_merchant, make_location, make_integration, session_client
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    loc_a = make_location(owner_a.merchant)
    loc_b = make_location(owner_b.merchant)
    int_a = make_integration(owner_a.merchant)
    int_b = make_integration(owner_b.merchant)
    txn_a = _record(owner_a.merchant, int_a, loc_a)
    _record(owner_b.merchant, int_b, loc_b)

    client = session_client(owner_a.user.email)
    resp = client.get(TRANSACTIONS)
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()["results"]}
    assert ids == {str(txn_a.id)}


def test_transaction_body_has_null_customer_when_no_phone_supplied(
    make_merchant, make_location, make_integration, session_client
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    txn = _record(owner.merchant, integration, location)

    client = session_client(owner.user.email)
    resp = client.get(_detail(txn.id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["customer"] is None
    assert body["amount"] == "100.00"


def test_transaction_body_has_customer_when_phone_supplied(
    make_merchant, make_location, make_integration, session_client
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    txn = _record(owner.merchant, integration, location, customer_phone="+919999999999", customer_name="Rahul")

    client = session_client(owner.user.email)
    resp = client.get(_detail(txn.id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["customer"]["phone"] == "+919999999999"
    assert body["customer"]["name"] == "Rahul"


def test_location_id_and_status_filters(make_merchant, make_location, make_integration, session_client):
    owner = make_merchant("A")
    loc1 = make_location(owner.merchant, name="L1")
    loc2 = make_location(owner.merchant, name="L2")
    integration = make_integration(owner.merchant)
    txn1 = _record(owner.merchant, integration, loc1, external_transaction_id="INV-1")
    _record(owner.merchant, integration, loc2, external_transaction_id="INV-2")

    client = session_client(owner.user.email)
    resp = client.get(TRANSACTIONS, {"location_id": str(loc1.id)})
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()["results"]}
    assert ids == {str(txn1.id)}

    resp = client.get(TRANSACTIONS, {"status": "REFUNDED"})
    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_malformed_filter_returns_422(make_merchant, session_client):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    resp = client.get(TRANSACTIONS, {"location_id": "not-a-uuid"})
    assert resp.status_code == 422

    resp = client.get(TRANSACTIONS, {"status": "NOT_A_STATUS"})
    assert resp.status_code == 422


def test_manager_only_sees_assigned_locations(
    make_merchant, add_member, assign_locations, make_location, make_integration, session_client
):
    owner = make_merchant("A")
    loc1 = make_location(owner.merchant, name="L1")
    loc2 = make_location(owner.merchant, name="L2")
    integration = make_integration(owner.merchant)
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    assign_locations(owner, manager, [loc1])

    txn1 = _record(owner.merchant, integration, loc1, external_transaction_id="INV-1")
    txn2 = _record(owner.merchant, integration, loc2, external_transaction_id="INV-2")

    client = session_client("manager@example.com")
    resp = client.get(TRANSACTIONS)
    ids = {row["id"] for row in resp.json()["results"]}
    assert ids == {str(txn1.id)}

    assert client.get(_detail(txn1.id)).status_code == 200
    assert client.get(_detail(txn2.id)).status_code == 404


def test_viewer_can_read_transactions(make_merchant, add_member, make_location, make_integration, session_client):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    add_member(owner.merchant, "VIEWER", "viewer@example.com")
    txn = _record(owner.merchant, integration, location)

    client = session_client("viewer@example.com")
    assert client.get(TRANSACTIONS).status_code == 200
    assert client.get(_detail(txn.id)).status_code == 200


def test_cross_merchant_detail_returns_404(make_merchant, make_location, make_integration, session_client):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    loc_b = make_location(owner_b.merchant)
    int_b = make_integration(owner_b.merchant)
    txn_b = _record(owner_b.merchant, int_b, loc_b)

    client = session_client(owner_a.user.email)
    resp = client.get(_detail(txn_b.id))
    assert resp.status_code == 404


def test_unauthenticated_request_is_forbidden():
    from rest_framework.test import APIClient

    client = APIClient()
    resp = client.get(TRANSACTIONS)
    assert resp.status_code == 403


def test_pagination_next_cursor(make_merchant, make_location, make_integration, session_client):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    integration = make_integration(owner.merchant)
    for i in range(30):
        _record(owner.merchant, integration, location, external_transaction_id=f"INV-{i}")

    client = session_client(owner.user.email)
    resp = client.get(TRANSACTIONS)
    body = resp.json()
    assert len(body["results"]) == 25
    assert body["next_cursor"] is not None

    resp2 = client.get(TRANSACTIONS, {"cursor": body["next_cursor"]})
    body2 = resp2.json()
    assert len(body2["results"]) == 5
    assert body2["next_cursor"] is None
