"""Customer opt-out (spec 08 Definition of done "Inbound webhook & opt-out":
opt_out_customer, POST /customers/{id}/opt-out; Business-Rules.md §3:
opt-out never deletes history; OD-7: manual opt-out roles, no AuditLog row)."""
import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from auditlog.models import AuditLog
from core.tenancy import tenant_atomic, tenant_context
from customers import services
from customers.models import Customer
from locations import services as location_services
from transactions.models import Transaction

pytestmark = pytest.mark.django_db

PHONE = "+919999990001"
PASSWORD = "Tr1cky-Horse-Battery-Staple!"


def login(email):
    client = APIClient(enforce_csrf_checks=True)
    client.get("/api/v1/auth/login")
    token = client.cookies["csrftoken"].value
    resp = client.post(
        "/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json", HTTP_X_CSRFTOKEN=token
    )
    assert resp.status_code == 200, resp.content
    client.csrf = client.cookies["csrftoken"].value
    return client


def new_customer(merchant, phone=PHONE):
    with tenant_context(merchant.id), tenant_atomic():
        return Customer.objects.create(merchant_id=merchant.id, phone=phone, name="Test Customer")


def state(merchant, phone=PHONE):
    with tenant_context(merchant.id), tenant_atomic():
        c = Customer.objects.get(phone=phone)
        return c.opted_out, c.opted_out_at


def opt_out_url(customer):
    return f"/api/v1/customers/{customer.id}/opt-out"


def post(client, customer):
    return client.post(opt_out_url(customer), format="json", HTTP_X_CSRFTOKEN=client.csrf)


# --- opt_out_customer service ----------------------------------------------------------------


def test_opt_out_sets_the_flag_and_timestamp_once_and_a_second_call_keeps_the_first_time(make_merchant):
    owner = make_merchant("A")
    new_customer(owner.merchant)

    with tenant_context(owner.merchant_id):
        returned = services.opt_out_customer(phone=PHONE, source="INBOUND_KEYWORD")
    flag, first_at = state(owner.merchant)
    with tenant_context(owner.merchant_id):
        services.opt_out_customer(phone=PHONE, source="INBOUND_KEYWORD")

    assert returned is not None and returned.phone == PHONE
    assert flag is True and first_at is not None
    assert state(owner.merchant) == (True, first_at)


def test_opt_out_by_customer_object_for_a_manual_opt_out(make_merchant):
    owner = make_merchant("A")
    customer = new_customer(owner.merchant)

    with tenant_context(owner.merchant_id):
        services.opt_out_customer(customer=customer, source="MANUAL", actor=owner.user)

    assert state(owner.merchant)[0] is True


def test_opt_out_of_an_unknown_phone_returns_none_and_creates_no_customer(make_merchant):
    owner = make_merchant("A")

    with tenant_context(owner.merchant_id):
        assert services.opt_out_customer(phone=PHONE, source="INBOUND_KEYWORD") is None

    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not Customer.objects.exists()


def test_opt_out_leaves_existing_transactions_untouched(make_merchant):
    owner = make_merchant("A")
    customer = new_customer(owner.merchant)
    with tenant_context(owner.merchant_id):
        location = location_services.create_location(name="Cafe")
    occurred = timezone.now() - timedelta(days=1)
    with tenant_context(owner.merchant_id), tenant_atomic():
        txn = Transaction.objects.create(
            merchant_id=owner.merchant_id,
            location=location,
            customer=customer,
            external_transaction_id="T-1",
            amount="100.00",
            currency="INR",
            occurred_at=occurred,
        )

    with tenant_context(owner.merchant_id):
        services.opt_out_customer(phone=PHONE, source="INBOUND_KEYWORD")

    with tenant_context(owner.merchant_id), tenant_atomic():
        kept = Transaction.objects.get(pk=txn.pk)
        assert (kept.customer_id, str(kept.amount), kept.status, kept.occurred_at) == (
            customer.id,
            "100.00",
            txn.status,
            occurred,
        )
        assert Customer.objects.filter(pk=customer.pk).exists()


def test_opting_out_a_phone_under_merchant_a_never_changes_merchant_bs_customer(make_merchant):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    new_customer(owner_a.merchant)
    new_customer(owner_b.merchant)

    with tenant_context(owner_a.merchant_id):
        services.opt_out_customer(phone=PHONE, source="INBOUND_KEYWORD")

    assert state(owner_a.merchant)[0] is True
    assert state(owner_b.merchant) == (False, None)


def test_opt_out_rejects_an_unknown_source(make_merchant):
    owner = make_merchant("A")
    new_customer(owner.merchant)

    with tenant_context(owner.merchant_id):
        with pytest.raises(Exception):
            services.opt_out_customer(phone=PHONE, source="SOMETHING_ELSE")

    assert state(owner.merchant) == (False, None)


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_stops_leave_one_consistent_timestamp(make_merchant):
    owner = make_merchant("A")
    new_customer(owner.merchant)
    barrier = threading.Barrier(2)
    errors = []

    def _run():
        try:
            barrier.wait(timeout=10)
            with tenant_context(owner.merchant_id):
                services.opt_out_customer(phone=PHONE, source="INBOUND_KEYWORD")
        except Exception as exc:  # captured for the assertion below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    flag, at = state(owner.merchant)
    assert flag is True and at is not None
    with tenant_context(owner.merchant_id):
        services.opt_out_customer(phone=PHONE, source="INBOUND_KEYWORD")
    assert state(owner.merchant) == (True, at)


# --- POST /customers/{id}/opt-out --------------------------------------------------------------------


def test_manual_opt_out_answers_200_with_the_opt_out_and_is_idempotent(make_merchant):
    owner = make_merchant("A")
    customer = new_customer(owner.merchant)
    client = login(owner.user.email)

    first = post(client, customer)
    second = post(client, customer)

    assert first.status_code == 200 and second.status_code == 200
    body = first.json()
    assert body["id"] == str(customer.id)
    assert body["opted_out"] is True
    assert body["opted_out_at"] is not None
    assert second.json()["opted_out_at"] == body["opted_out_at"]
    assert state(owner.merchant)[0] is True


def test_a_manual_opt_out_writes_no_audit_log_row(make_merchant):
    owner = make_merchant("A")
    customer = new_customer(owner.merchant)

    assert post(login(owner.user.email), customer).status_code == 200

    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not AuditLog.objects.filter(action__icontains="opt").exists()


def test_another_merchants_customer_answers_404_and_is_not_changed(make_merchant):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    foreign = new_customer(owner_b.merchant)

    resp = post(login(owner_a.user.email), foreign)

    assert resp.status_code == 404
    assert state(owner_b.merchant) == (False, None)


def test_an_unknown_customer_id_answers_404(make_merchant):
    import uuid

    owner = make_merchant("A")
    client = login(owner.user.email)
    resp = client.post(f"/api/v1/customers/{uuid.uuid4()}/opt-out", format="json", HTTP_X_CSRFTOKEN=client.csrf)
    assert resp.status_code == 404


def test_a_viewer_cannot_opt_a_customer_out(make_merchant, add_member):
    owner = make_merchant("A")
    customer = new_customer(owner.merchant)
    add_member(owner.merchant, "VIEWER", "viewer@example.com")

    resp = post(login("viewer@example.com"), customer)

    assert resp.status_code == 403
    assert state(owner.merchant) == (False, None)


@pytest.mark.parametrize("role", ["ADMIN", "MANAGER"])
def test_admin_and_manager_can_opt_a_customer_out(make_merchant, add_member, role):
    owner = make_merchant("A")
    customer = new_customer(owner.merchant)
    add_member(owner.merchant, role, "member@example.com")

    resp = post(login("member@example.com"), customer)

    assert resp.status_code == 200
    assert state(owner.merchant)[0] is True


def test_unauthenticated_and_csrf_less_requests_get_403(make_merchant):
    owner = make_merchant("A")
    customer = new_customer(owner.merchant)
    client = login(owner.user.email)

    assert APIClient().post(opt_out_url(customer), format="json").status_code == 403
    assert client.post(opt_out_url(customer), format="json").status_code == 403
    assert state(owner.merchant) == (False, None)
