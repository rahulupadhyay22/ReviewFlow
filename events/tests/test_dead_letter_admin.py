"""Dead-letter IntegrationEvent review (spec 16 D1/D3; plan step 6).

Service: only DEAD_LETTER rows, only the safe fields, never the payload.
View: audited staff read of one merchant, read-only, rendered rows checked on
content (not just status)."""
import uuid

import pytest
from django.utils import timezone as dj_timezone

from accounts.models import User
from auditlog.models import AuditLog
from auditlog.services import AUDIT_STAFF_DATA_VIEWED
from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context
from events.models import IntegrationEvent
from events.services import DEAD_LETTER_REVIEW_FIELDS, list_dead_letter_events

pytestmark = pytest.mark.django_db

PAYLOAD_PHONE = "+919812345678"
PAYLOAD_NAME = "Secret Customer Name"


@pytest.fixture
def staff():
    return User.objects.create_user("staff@example.com", "x", is_staff=True)


@pytest.fixture
def make_event(make_integration):
    integrations = {}

    def _make(merchant, status, external_event_id=None):
        if merchant.id not in integrations:
            integrations[merchant.id] = make_integration(merchant)
        with tenant_context(merchant.id), tenant_atomic():
            return IntegrationEvent.objects.create(
                merchant=merchant,
                integration=integrations[merchant.id],
                source="webhook",
                external_event_id=external_event_id or f"ext-{uuid.uuid4().hex[:8]}",
                payload={"customer": {"phone": PAYLOAD_PHONE, "name": PAYLOAD_NAME}},
                status=status,
                received_at=dj_timezone.now(),
                attempt_count=5,
                error_code="PAYLOAD_INVALID",
                error_message="The event payload failed validation.",
            )

    return _make


@pytest.fixture
def two_merchants(make_merchant, make_event):
    a, b = make_merchant("A").merchant, make_merchant("B").merchant
    for status in IntegrationEvent.Status.values:
        make_event(a, status, external_event_id=f"A-{status}")
        make_event(b, status, external_event_id=f"B-{status}")
    return a, b


def dl_url(merchant_id):
    return f"/admin/events/dead-letter/{merchant_id}/"


def staff_audit_count(merchant):
    with tenant_context(merchant.id), tenant_atomic():
        return AuditLog.objects.filter(action=AUDIT_STAFF_DATA_VIEWED).count()


# --- service ---------------------------------------------------------------------


def test_the_service_returns_only_dead_letter_rows_with_exactly_the_safe_fields(two_merchants):
    a, _b = two_merchants
    with tenant_context(a.id), tenant_atomic():
        total, rows = list_dead_letter_events()
    assert total == 1
    assert [r["external_event_id"] for r in rows] == ["A-DEAD_LETTER"]
    assert set(rows[0]) == set(DEAD_LETTER_REVIEW_FIELDS)
    assert "payload" not in DEAD_LETTER_REVIEW_FIELDS and "location_id" not in DEAD_LETTER_REVIEW_FIELDS
    assert PAYLOAD_PHONE not in str(rows)


def test_the_service_caps_rows_but_reports_the_total(make_merchant, make_event):
    a = make_merchant().merchant
    for _ in range(3):
        make_event(a, IntegrationEvent.Status.DEAD_LETTER)
    with tenant_context(a.id), tenant_atomic():
        total, rows = list_dead_letter_events(limit=2)
    assert (total, len(rows)) == (3, 2)


def test_the_service_needs_a_tenant_context():
    with pytest.raises(TenantContextError):
        list_dead_letter_events()


# --- view ------------------------------------------------------------------------


def test_the_page_renders_this_merchants_dead_letter_rows_and_nothing_else(
    two_merchants, staff, admin_session_client
):
    a, _b = two_merchants
    resp = admin_session_client(staff).get(dl_url(a.id))
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "A-DEAD_LETTER" in html and "PAYLOAD_INVALID" in html
    for status in ("RECEIVED", "PROCESSED", "FAILED", "CANCELLED"):
        assert f"A-{status}" not in html
    assert "B-" not in html  # merchant isolation
    assert PAYLOAD_PHONE not in html and PAYLOAD_NAME not in html  # never the payload


def test_every_get_writes_exactly_one_staff_audit_row(two_merchants, staff, admin_session_client):
    a, b = two_merchants
    client = admin_session_client(staff)
    client.get(dl_url(a.id))
    client.get(dl_url(a.id))
    assert staff_audit_count(a) == 2
    assert staff_audit_count(b) == 0


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_the_page_is_read_only(two_merchants, staff, admin_session_client, method):
    a, _b = two_merchants
    resp = getattr(admin_session_client(staff), method)(dl_url(a.id))
    assert resp.status_code == 405
    assert staff_audit_count(a) == 0


@pytest.mark.parametrize("extra", [{"is_staff": False}, {"is_active": False}], ids=["non-staff", "inactive"])
def test_a_non_staff_caller_of_the_view_gets_403_not_500_and_no_audit_row(two_merchants, extra):
    """Regression (code review 2026-10-13): StaffAccessDenied is a domain
    error raised below an admin view, which has no DRF exception handler, so
    it used to propagate as a 500. admin_view normally blocks such a user
    first, so the view is called directly."""
    from django.core.exceptions import PermissionDenied
    from django.test import RequestFactory

    from events.admin import dead_letter_events_view

    a, _b = two_merchants
    caller = User.objects.create_user("notstaff@example.com", "x", **{"is_staff": True, **extra})
    request = RequestFactory().get(dl_url(a.id))
    request.user = caller
    with pytest.raises(PermissionDenied):
        dead_letter_events_view(request, a.id)
    assert staff_audit_count(a) == 0


def test_the_page_names_the_merchant_without_a_second_lookup(two_merchants, staff, admin_session_client):
    """Item 4: staff_merchant_read yields the Merchant it already fetched, and
    the view renders its name from that."""
    a, _b = two_merchants
    html = admin_session_client(staff).get(dl_url(a.id)).content.decode()
    assert f"Dead-letter events: {a.name}" in html


def test_an_unknown_merchant_is_404_not_500(staff, admin_session_client):
    assert admin_session_client(staff).get(dl_url(uuid.uuid4())).status_code == 404


def test_a_staff_session_that_also_holds_a_dashboard_merchant_works_for_another_merchant(
    two_merchants, staff, admin_session_client
):
    a, b = two_merchants
    client = admin_session_client(staff)
    session = client.session
    session["merchant_id"] = str(b.id)
    session.save()
    resp = client.get(dl_url(a.id))
    assert resp.status_code == 200
    assert "A-DEAD_LETTER" in resp.content.decode()


def test_without_the_admin_2fa_flag_the_page_redirects_to_login_and_reads_nothing(two_merchants, staff, client, settings):
    a, _b = two_merchants
    settings.ADMIN_ALLOWED_IPS = ["127.0.0.1/32"]
    client.force_login(staff)
    resp = client.get(dl_url(a.id))
    assert resp.status_code == 302 and "/admin/login/" in resp["Location"]
    assert staff_audit_count(a) == 0


def test_a_non_allowlisted_ip_gets_404(two_merchants, staff, admin_session_client, settings):
    a, _b = two_merchants
    client = admin_session_client(staff)
    settings.ADMIN_ALLOWED_IPS = ["10.0.0.0/8"]
    assert client.get(dl_url(a.id)).status_code == 404
    assert staff_audit_count(a) == 0


def test_the_merchant_changelist_links_to_the_dead_letter_page(two_merchants, staff, admin_session_client):
    a, _b = two_merchants
    staff.is_superuser = True
    staff.save()
    html = admin_session_client(staff).get("/admin/accounts/merchant/").content.decode()
    assert dl_url(a.id) in html
