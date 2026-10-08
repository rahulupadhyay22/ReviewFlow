"""Location sender mapping (spec 08 Definition of done "Sender mapping" and the
application-layer half of "Tenant isolation"). Endpoints:
GET /whatsapp/senders, GET|PUT|DELETE /locations/{id}/whatsapp-sender."""
import threading

import pytest
from django.db import connection
from rest_framework.test import APIClient

from auditlog.models import AuditLog
from core.tenancy import tenant_atomic, tenant_context
from whatsapp import services
from whatsapp.models import WhatsAppLocationMapping

pytestmark = pytest.mark.django_db

SENDERS = "/api/v1/whatsapp/senders"


def sender_url(location):
    return f"/api/v1/locations/{location.id}/whatsapp-sender"


def put(client, location, account_id):
    return client.put(
        sender_url(location), {"whatsapp_account_id": str(account_id)}, format="json", HTTP_X_CSRFTOKEN=client.csrf
    )


def delete(client, location):
    return client.delete(sender_url(location), HTTP_X_CSRFTOKEN=client.csrf)


def audit_rows(merchant, action="whatsapp.sender_changed"):
    with tenant_context(merchant.id), tenant_atomic():
        return list(AuditLog.objects.filter(action=action).order_by("created_at"))


def mapping_count(merchant):
    with tenant_context(merchant.id), tenant_atomic():
        return WhatsAppLocationMapping.objects.count()


@pytest.fixture
def setup(make_merchant, make_location, session_client, shared_account):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    return owner, location, session_client(owner.user.email), shared_account


def test_put_shared_account_maps_the_location_and_audits_the_change_once(setup):
    owner, location, client, shared = setup

    resp = put(client, location, shared.id)

    assert resp.status_code == 200
    with tenant_context(owner.merchant_id):
        assert services.get_location_sender(location).id == shared.id
    rows = audit_rows(owner.merchant)
    assert len(rows) == 1
    assert rows[0].metadata_json == {"from_sender_type": None, "to_sender_type": "SHARED_POOL"}
    assert rows[0].target_id == str(location.id)
    blob = str(rows[0].metadata_json) + str(rows[0].target_type)
    assert shared.phone_number_id not in blob and shared.business_account_id not in blob


def test_repeating_the_same_put_is_ok_with_one_mapping_and_no_second_audit_row(setup):
    owner, location, client, shared = setup

    assert put(client, location, shared.id).status_code == 200
    assert put(client, location, shared.id).status_code == 200

    assert mapping_count(owner.merchant) == 1
    assert len(audit_rows(owner.merchant)) == 1


def test_repointing_to_another_sender_audits_the_from_and_to_types(setup, make_own_account):
    owner, location, client, shared = setup
    own = make_own_account(owner.merchant)
    put(client, location, shared.id)

    assert put(client, location, own.id).status_code == 200

    rows = audit_rows(owner.merchant)
    assert len(rows) == 2
    assert rows[1].metadata_json == {"from_sender_type": "SHARED_POOL", "to_sender_type": "OWN_NUMBER"}
    assert mapping_count(owner.merchant) == 1


@pytest.mark.parametrize("status", ["SUSPENDED", "PENDING"])
def test_mapping_to_an_inactive_own_number_is_rejected_with_422(setup, make_own_account, status):
    owner, location, client, _shared = setup
    own = make_own_account(owner.merchant, status=status)

    resp = put(client, location, own.id)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert mapping_count(owner.merchant) == 0


@pytest.mark.parametrize("status", ["SUSPENDED", "PENDING"])
def test_mapping_to_a_non_active_shared_account_is_rejected_with_422(
    make_merchant, make_location, session_client, make_shared_account, status
):
    # DoD (amended 2026-10-07): a SUSPENDED/PENDING account the caller can see
    # (the shared account) -> 422, not 404.
    shared = make_shared_account(status)
    owner = make_merchant("A")
    location = make_location(owner.merchant)

    resp = put(session_client(owner.user.email), location, shared.id)

    assert resp.status_code == 422
    assert mapping_count(owner.merchant) == 0


def test_mapping_to_another_merchants_own_number_answers_404(
    setup, make_merchant, make_own_account
):
    owner, location, client, _shared = setup
    foreign = make_own_account(make_merchant("B").merchant)

    resp = put(client, location, foreign.id)

    assert resp.status_code == 404
    assert mapping_count(owner.merchant) == 0


def test_mapping_to_an_unknown_account_id_answers_404(setup):
    import uuid

    owner, location, client, _shared = setup
    assert put(client, location, uuid.uuid4()).status_code == 404


def test_get_senders_lists_own_and_active_shared_never_foreign_and_never_provider_ids(
    setup, make_merchant, make_own_account
):
    owner, _location, client, shared = setup
    own = make_own_account(owner.merchant)
    foreign = make_own_account(make_merchant("B").merchant)

    resp = client.get(SENDERS)

    assert resp.status_code == 200
    text = resp.content.decode()
    assert str(shared.id) in text and str(own.id) in text
    assert str(foreign.id) not in text
    for secret in ("phone_number_id", "business_account_id", own.phone_number_id, shared.phone_number_id):
        assert secret not in text


def test_get_senders_omits_a_non_active_shared_account(
    make_merchant, session_client, make_shared_account
):
    shared = make_shared_account("SUSPENDED")
    owner = make_merchant("A")

    resp = session_client(owner.user.email).get(SENDERS)

    assert resp.status_code == 200
    assert str(shared.id) not in resp.content.decode()


def test_get_location_sender_returns_the_sender_or_null(setup):
    owner, location, client, shared = setup

    assert client.get(sender_url(location)).json() == {"sender": None}
    put(client, location, shared.id)
    body = client.get(sender_url(location)).json()

    assert body["sender"]["id"] == str(shared.id)
    assert body["sender"]["sender_type"] == "SHARED_POOL"
    assert "phone_number_id" not in body["sender"]


def test_get_location_sender_of_another_merchants_location_answers_404(
    setup, make_merchant, make_location
):
    _owner, _location, client, _shared = setup
    foreign_location = make_location(make_merchant("B").merchant)

    assert client.get(sender_url(foreign_location)).status_code == 404


def test_put_and_delete_on_another_merchants_location_answer_404(
    setup, make_merchant, make_location
):
    _owner, _location, client, shared = setup
    foreign_location = make_location(make_merchant("B").merchant)

    assert put(client, foreign_location, shared.id).status_code == 404
    assert delete(client, foreign_location).status_code == 404


def test_delete_unmaps_and_repeating_it_is_204_without_an_extra_audit_row(setup):
    owner, location, client, shared = setup
    put(client, location, shared.id)

    assert delete(client, location).status_code == 204
    assert delete(client, location).status_code == 204

    assert mapping_count(owner.merchant) == 0
    rows = audit_rows(owner.merchant)
    assert len(rows) == 2  # one map, one unmap
    assert rows[1].metadata_json == {"from_sender_type": "SHARED_POOL", "to_sender_type": None}
    with tenant_context(owner.merchant_id):
        assert services.get_location_sender(location) is None


def test_delete_when_never_mapped_is_204_and_writes_no_audit_row(setup):
    owner, location, client, _shared = setup
    assert delete(client, location).status_code == 204
    assert audit_rows(owner.merchant) == []


def test_get_location_sender_service_returns_none_once_the_account_is_not_active(
    setup, make_shared_account
):
    owner, location, client, shared = setup
    put(client, location, shared.id)

    make_shared_account("SUSPENDED")

    with tenant_context(owner.merchant_id):
        assert services.get_location_sender(location) is None


@pytest.mark.parametrize("method", ["put", "delete"])
def test_viewer_cannot_change_a_location_sender(setup, add_member, session_client, method):
    owner, location, _client, shared = setup
    add_member(owner.merchant, "VIEWER", "viewer@example.com")
    viewer = session_client("viewer@example.com")

    resp = put(viewer, location, shared.id) if method == "put" else delete(viewer, location)

    assert resp.status_code == 403
    assert mapping_count(owner.merchant) == 0


def test_viewer_can_read_senders_and_a_location_sender(setup, add_member, session_client):
    owner, location, _client, _shared = setup
    add_member(owner.merchant, "VIEWER", "viewer@example.com")
    viewer = session_client("viewer@example.com")

    assert viewer.get(SENDERS).status_code == 200
    assert viewer.get(sender_url(location)).status_code == 200


def test_admin_can_map_a_location(setup, add_member, session_client):
    owner, location, _client, shared = setup
    add_member(owner.merchant, "ADMIN", "admin@example.com")

    assert put(session_client("admin@example.com"), location, shared.id).status_code == 200


def test_manager_gets_404_for_an_unassigned_location_and_succeeds_for_an_assigned_one(
    setup, add_member, assign_manager, make_location, session_client
):
    owner, assigned, _client, shared = setup
    unassigned = make_location(owner.merchant, name="Other")
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    assign_manager(owner, manager, assigned)
    client = session_client("manager@example.com")

    assert put(client, unassigned, shared.id).status_code == 404
    assert delete(client, unassigned).status_code == 404
    assert client.get(sender_url(unassigned)).status_code == 404
    assert put(client, assigned, shared.id).status_code == 200
    assert delete(client, assigned).status_code == 204


def test_unauthenticated_requests_get_403(setup):
    _owner, location, _client, shared = setup
    anon = APIClient()

    assert anon.get(SENDERS).status_code == 403
    assert anon.get(sender_url(location)).status_code == 403
    assert anon.put(sender_url(location), {"whatsapp_account_id": str(shared.id)}, format="json").status_code == 403
    assert anon.delete(sender_url(location)).status_code == 403


def test_put_and_delete_without_a_csrf_token_get_403(setup):
    owner, location, client, shared = setup

    no_csrf_put = client.put(sender_url(location), {"whatsapp_account_id": str(shared.id)}, format="json")
    no_csrf_delete = client.delete(sender_url(location))

    assert no_csrf_put.status_code == 403
    assert no_csrf_delete.status_code == 403
    assert mapping_count(owner.merchant) == 0


def test_put_requires_whatsapp_account_id(setup):
    _owner, location, client, _shared = setup
    resp = client.put(sender_url(location), {}, format="json", HTTP_X_CSRFTOKEN=client.csrf)
    assert resp.status_code == 422
    assert "whatsapp_account_id" in resp.json()["error"]["field_errors"]


@pytest.mark.django_db(transaction=True)
def test_concurrent_set_location_sender_calls_leave_exactly_one_mapping(
    make_merchant, make_location, shared_account, make_own_account
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    own = make_own_account(owner.merchant)
    barrier = threading.Barrier(2)
    errors = []

    def _run(account):
        try:
            barrier.wait(timeout=10)
            with tenant_context(owner.merchant_id):
                services.set_location_sender(location=location, account=account, actor=owner.user)
        except Exception as exc:  # captured for the assertion below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_run, args=(a,)) for a in (shared_account, own)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert mapping_count(owner.merchant) == 1
