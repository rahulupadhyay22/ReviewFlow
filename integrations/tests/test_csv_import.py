"""CSV Import: POST /integrations/{id}/csv-imports and the import_csv task
(spec .claude/specs/06-priority-integrations.md Decision 10). R2 is
replaced by an in-memory fake -- no network calls."""
import io

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from botocore.exceptions import ClientError
from core.tenancy import tenant_atomic, tenant_context
from events.models import IntegrationEvent
from integrations import services
from transactions.models import Transaction

pytestmark = pytest.mark.django_db(transaction=True)

CSV_HEADER = "external_transaction_id,amount,currency,occurred_at,customer_phone,customer_name,external_location_id\n"


def _row(txn_id="INV-1", amount="10.00", currency="INR", occurred_at="2024-01-01T10:00:00+00:00", phone="", name="", loc=""):
    return f"{txn_id},{amount},{currency},{occurred_at},{phone},{name},{loc}\n"


def _url(integration_id):
    return f"/api/v1/integrations/{integration_id}/csv-imports"


def _connect_csv(owner):
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration, _ = services.connect_integration(actor=owner, provider="csv")
    return integration


class _FakeStorage:
    def __init__(self):
        self.objects = {}
        self.fail_get_after = None  # object key that should raise NoSuchKey

    def put_object(self, key, data):
        self.objects[key] = data

    def get_object(self, key):
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject")
        return self.objects[key]

    def delete_object(self, key):
        self.objects.pop(key, None)


@pytest.fixture(autouse=True)
def local_cache(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def fake_storage(monkeypatch):
    fake = _FakeStorage()
    monkeypatch.setattr(services.storage, "put_object", fake.put_object)
    monkeypatch.setattr(services.storage, "get_object", fake.get_object)
    monkeypatch.setattr(services.storage, "delete_object", fake.delete_object)
    return fake


def _upload(client, integration_id, csv_bytes, filename="sales.csv"):
    upload = io.BytesIO(csv_bytes)
    upload.name = filename
    return client.post(
        _url(integration_id), {"file": upload}, format="multipart", HTTP_X_CSRFTOKEN=client.csrf
    )


# --- happy path -------------------------------------------------------


def test_valid_csv_upload_returns_202_and_processes_all_rows(
    make_merchant, make_location, session_client, fake_storage, django_capture_on_commit_callbacks
):
    owner = make_merchant("A")
    integration = _connect_csv(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)

    client = session_client(owner.user.email)
    body = CSV_HEADER + _row("INV-1") + _row("INV-2") + _row("INV-3")
    with django_capture_on_commit_callbacks(execute=True):
        resp = _upload(client, integration.id, body.encode())
    assert resp.status_code == 202
    assert resp.json() == {"rows": 3}

    with tenant_context(owner.merchant_id), tenant_atomic():
        assert IntegrationEvent.objects.filter(integration=integration).count() == 3
        assert Transaction.objects.filter(location=location).count() == 3
    assert fake_storage.objects == {}  # deleted after all rows recorded


def test_reuploading_the_same_file_creates_no_new_rows(
    make_merchant, make_location, session_client, fake_storage, django_capture_on_commit_callbacks
):
    owner = make_merchant("A")
    integration = _connect_csv(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)

    client = session_client(owner.user.email)
    body = (CSV_HEADER + _row("INV-1")).encode()
    with django_capture_on_commit_callbacks(execute=True):
        _upload(client, integration.id, body)
    with django_capture_on_commit_callbacks(execute=True):
        resp = _upload(client, integration.id, body)
    assert resp.status_code == 202

    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Transaction.objects.filter(location=location).count() == 1


# --- validation ---------------------------------------------------------


def test_missing_required_column_returns_422_and_uploads_nothing(
    make_merchant, session_client, fake_storage
):
    owner = make_merchant("A")
    integration = _connect_csv(owner)
    client = session_client(owner.user.email)
    body = "external_transaction_id,amount\nINV-1,10.00\n"
    resp = _upload(client, integration.id, body.encode())
    assert resp.status_code == 422
    assert fake_storage.objects == {}


def test_bad_row_returns_422_with_field_errors_and_uploads_nothing(
    make_merchant, session_client, fake_storage
):
    owner = make_merchant("A")
    integration = _connect_csv(owner)
    client = session_client(owner.user.email)
    body = CSV_HEADER + _row("INV-1", amount="not-a-number")
    resp = _upload(client, integration.id, body.encode())
    assert resp.status_code == 422
    assert "row_1" in resp.json()["error"]["field_errors"]
    assert fake_storage.objects == {}


def test_non_csv_provider_integration_returns_422(make_merchant, session_client, fake_storage):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration, _ = services.connect_integration(
            actor=owner,
            provider="webhook",
            config_json={
                "field_map": {
                    "external_transaction_id": "id",
                    "amount": "amount",
                    "occurred_at": "occurred_at",
                },
                "default_currency": "INR",
            },
        )
    client = session_client(owner.user.email)
    body = CSV_HEADER + _row()
    resp = _upload(client, integration.id, body.encode())
    assert resp.status_code == 422


def test_disconnected_integration_returns_422(make_merchant, session_client, fake_storage):
    owner = make_merchant("A")
    integration = _connect_csv(owner)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.disconnect_integration(actor=owner, integration=integration)
    client = session_client(owner.user.email)
    body = CSV_HEADER + _row()
    resp = _upload(client, integration.id, body.encode())
    assert resp.status_code == 422


def test_oversized_and_over_row_limits_return_422(make_merchant, session_client, fake_storage, settings):
    owner = make_merchant("A")
    integration = _connect_csv(owner)
    client = session_client(owner.user.email)
    settings.CSV_IMPORT_MAX_ROWS = 2
    body = CSV_HEADER + _row("A") + _row("B") + _row("C")
    resp = _upload(client, integration.id, body.encode())
    assert resp.status_code == 422
    assert fake_storage.objects == {}


def test_file_over_the_configured_byte_size_limit_returns_422_and_uploads_nothing(
    make_merchant, session_client, fake_storage, settings
):
    owner = make_merchant("A")
    integration = _connect_csv(owner)
    client = session_client(owner.user.email)
    body = CSV_HEADER + _row("A")
    settings.CSV_IMPORT_MAX_BYTES = len(body.encode()) - 1
    resp = _upload(client, integration.id, body.encode())
    assert resp.status_code == 422
    assert fake_storage.objects == {}


def test_non_utf8_file_returns_422_and_uploads_nothing(make_merchant, session_client, fake_storage):
    owner = make_merchant("A")
    integration = _connect_csv(owner)
    client = session_client(owner.user.email)
    # "amount" column header written in latin-1 (0xE9 = 'é'), not valid UTF-8.
    body = b"external_transaction_id,am\xe9ount,currency,occurred_at\nINV-1,10.00,INR,2024-01-01T10:00:00+00:00\n"
    resp = _upload(client, integration.id, body)
    assert resp.status_code == 422
    assert fake_storage.objects == {}


# --- permissions --------------------------------------------------------


def test_manager_and_viewer_forbidden(make_merchant, add_member, session_client, fake_storage):
    from accounts.models import TeamMember

    owner = make_merchant("A")
    integration = _connect_csv(owner)
    add_member(owner.merchant, TeamMember.Role.MANAGER, "manager@example.com")
    add_member(owner.merchant, TeamMember.Role.VIEWER, "viewer@example.com")
    body = CSV_HEADER + _row()

    for email in ("manager@example.com", "viewer@example.com"):
        client = session_client(email)
        resp = _upload(client, integration.id, body.encode())
        assert resp.status_code == 403


def test_cross_merchant_integration_id_returns_404_and_uploads_nothing(
    make_merchant, session_client, fake_storage
):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_b = _connect_csv(owner_b)
    client = session_client(owner_a.user.email)
    resp = _upload(client, integration_b.id, (CSV_HEADER + _row()).encode())
    assert resp.status_code == 404
    assert fake_storage.objects == {}


def test_missing_csrf_token_returns_403_and_uploads_nothing(make_merchant, session_client, fake_storage):
    owner = make_merchant("A")
    integration = _connect_csv(owner)
    client = session_client(owner.user.email)
    upload = io.BytesIO((CSV_HEADER + _row()).encode())
    upload.name = "sales.csv"
    resp = client.post(_url(integration.id), {"file": upload}, format="multipart")  # no X-CSRFToken
    assert resp.status_code == 403
    assert fake_storage.objects == {}


# --- row outcomes -------------------------------------------------------


def test_blank_phone_row_creates_customerless_transaction_and_unmapped_location_fails(
    make_merchant, make_location, session_client, fake_storage, django_capture_on_commit_callbacks
):
    owner = make_merchant("A")
    integration = _connect_csv(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(
            integration, location_id=location.id, config_json={"external_location_id": "store-1"}
        )

    client = session_client(owner.user.email)
    body = CSV_HEADER + _row("INV-NOPHONE", phone="", loc="store-1") + _row("INV-UNMAPPED", loc="store-9")
    with django_capture_on_commit_callbacks(execute=True):
        resp = _upload(client, integration.id, body.encode())
    assert resp.status_code == 202

    with tenant_context(owner.merchant_id), tenant_atomic():
        txn = Transaction.objects.get(location=location, external_transaction_id="INV-NOPHONE")
        assert txn.customer_id is None
        assert not Transaction.objects.filter(external_transaction_id="INV-UNMAPPED").exists()
        failed = [
            e for e in IntegrationEvent.objects.filter(integration=integration)
            if e.payload.get("external_transaction_id") == "INV-UNMAPPED"
        ]
        assert len(failed) == 1
        assert failed[0].status == IntegrationEvent.Status.FAILED
        assert failed[0].error_code == "LOCATION_UNRESOLVED"


# --- task-level recovery -------------------------------------------------


def test_midway_task_failure_keeps_object_and_rerun_completes_without_duplicates(
    make_merchant, make_location, fake_storage, monkeypatch
):
    # Stub the auto-enqueue itself: Celery eager mode plus a real commit
    # (transaction=True) would otherwise run import_csv_rows immediately on
    # start_csv_import's own on_commit, before this test can inject the
    # mid-run failure.
    from integrations.tasks import import_csv

    monkeypatch.setattr(import_csv, "delay", lambda *a, **k: None)

    owner = make_merchant("A")
    integration = _connect_csv(owner)
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)
        rows = services.start_csv_import(
            integration=integration,
            file=io.BytesIO((CSV_HEADER + _row("INV-1") + _row("INV-2")).encode()),
        )
    assert rows == 2
    (key,) = fake_storage.objects.keys()

    call_count = {"n": 0}
    from events import services as events_services

    original_record_event = events_services.record_event

    def _flaky_record_event(*, integration, external_event_id, payload):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated mid-run failure")
        return original_record_event(integration=integration, external_event_id=external_event_id, payload=payload)

    # import_csv_rows does `from events.services import record_event` locally
    # (import-time cycle avoidance), so the source module must be patched.
    monkeypatch.setattr(events_services, "record_event", _flaky_record_event)

    with tenant_context(owner.merchant_id):
        with pytest.raises(RuntimeError):
            services.import_csv_rows(integration_id=integration.id, object_key=key)

    # Object kept; first row recorded, second row not (exception raised).
    assert key in fake_storage.objects
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Transaction.objects.filter(location=location).count() == 1

    monkeypatch.setattr(events_services, "record_event", original_record_event)
    with tenant_context(owner.merchant_id):
        services.import_csv_rows(integration_id=integration.id, object_key=key)

    with tenant_context(owner.merchant_id), tenant_atomic():
        assert Transaction.objects.filter(location=location).count() == 2
    assert fake_storage.objects == {}


def test_import_csv_rows_on_already_purged_object_is_a_noop(make_merchant, fake_storage):
    owner = make_merchant("A")
    integration = _connect_csv(owner)
    with tenant_context(owner.merchant_id):
        services.import_csv_rows(integration_id=integration.id, object_key="csv-imports/nowhere/none.csv")
    # No exception, no rows.


def test_import_csv_tenant_isolation(make_merchant, make_location, fake_storage, monkeypatch):
    """A correct, matching-owner CSV import (merchant A's task, merchant A's
    own integration and location) must produce a Transaction that RLS makes
    invisible to merchant B -- not just "B's location has 0 rows" (true
    trivially, since the row is created under location_a), but genuinely
    inaccessible from B's tenant context, including via raw SQL that
    bypasses TenantScopedManager entirely. This is distinct from the
    mismatched-merchant/integration regression tests below, which prove the
    task's *input* (a wrong integration_id) fails closed; this proves the
    task's *output* (a real, successfully-written Transaction) stays
    RLS-isolated from every other tenant."""
    from integrations.tasks import import_csv

    monkeypatch.setattr(import_csv, "delay", lambda *a, **k: None)

    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_a = _connect_csv(owner_a)
    location_a = make_location(owner_a.merchant)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        services.add_location_mapping(integration_a, location_id=location_a.id)
        rows = services.start_csv_import(
            integration=integration_a, file=io.BytesIO((CSV_HEADER + _row("INV-ISO")).encode())
        )
    assert rows == 1
    (key,) = fake_storage.objects.keys()

    import_csv(owner_a.merchant_id, integration_a.id, key)

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        created = Transaction.objects.get(location=location_a, external_transaction_id="INV-ISO")

    # TenantScopedManager: B's context sees none of A's transactions at all.
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        assert Transaction.objects.count() == 0

    # RLS backstop: even a raw query under B's SET LOCAL context -- which
    # bypasses TenantScopedManager's app-layer filter entirely -- cannot see
    # the row the CSV import task just wrote for A.
    from django.db import connection

    with tenant_context(owner_b.merchant_id), tenant_atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT id FROM transactions_transaction WHERE id = %s", [str(created.id)])
            assert cur.fetchone() is None


def test_import_csv_rows_with_mismatched_merchant_and_integration_raises_and_writes_nothing(
    make_merchant, make_location, fake_storage, monkeypatch
):
    """Regression for the bug class found and fixed in import_csv_rows: the
    task must set the tenant context from ITS OWN merchant_id argument
    (@tenant_task) and then read the Integration through THAT context's
    RLS, not trust the integration_id argument on its own. A merchant-A
    task pointed at merchant-B's Integration id must never read or write
    B's rows -- it must fail to find the Integration at all, because RLS
    hides B's row from A's tenant context."""
    from django.core.exceptions import ObjectDoesNotExist

    from integrations.tasks import import_csv

    monkeypatch.setattr(import_csv, "delay", lambda *a, **k: None)

    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_b = _connect_csv(owner_b)
    location_b = make_location(owner_b.merchant)
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        services.add_location_mapping(integration_b, location_id=location_b.id)
        rows = services.start_csv_import(
            integration=integration_b, file=io.BytesIO((CSV_HEADER + _row("INV-CROSS")).encode())
        )
    assert rows == 1
    (key,) = fake_storage.objects.keys()

    # merchant_a's tenant context, but merchant_b's integration id and
    # staged object key -- RLS on integrations_integration must hide B's
    # row from A's context, so the read fails closed.
    with tenant_context(owner_a.merchant_id):
        with pytest.raises(ObjectDoesNotExist):
            services.import_csv_rows(integration_id=integration_b.id, object_key=key)

    # Nothing was recorded under either merchant, and the staged object
    # (B's, not A's to delete) is untouched by the failed A-context read.
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        assert Transaction.objects.count() == 0
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        assert Transaction.objects.filter(location=location_b).count() == 0
    assert key in fake_storage.objects


def test_import_csv_task_end_to_end_with_mismatched_merchant_writes_nothing(
    make_merchant, make_location, fake_storage, monkeypatch
):
    """Same regression, exercised through the actual Celery task entrypoint
    (import_csv, not the bare service function), which is what a real
    worker invokes. @tenant_task sets the tenant context from ITS OWN
    merchant_id argument and does not catch exceptions (no autoretry, spec
    06 "Rules for implementation"), so a mismatched integration_id must
    surface as a DoesNotExist from inside merchant_a's tenant context --
    never a silent read of merchant_b's row."""
    from django.core.exceptions import ObjectDoesNotExist

    from integrations.tasks import import_csv

    monkeypatch.setattr(import_csv, "delay", lambda *a, **k: None)

    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_b = _connect_csv(owner_b)
    location_b = make_location(owner_b.merchant)
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        services.add_location_mapping(integration_b, location_id=location_b.id)
        rows = services.start_csv_import(
            integration=integration_b, file=io.BytesIO((CSV_HEADER + _row("INV-CROSS2")).encode())
        )
    assert rows == 1
    (key,) = fake_storage.objects.keys()

    # Calling the task with merchant_a's id but merchant_b's integration/key
    # must fail closed (DoesNotExist under A's RLS-scoped context), and
    # must write nothing under either merchant.
    with pytest.raises(ObjectDoesNotExist):
        import_csv(owner_a.merchant_id, integration_b.id, key)

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        assert Transaction.objects.count() == 0
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        assert Transaction.objects.filter(location=location_b).count() == 0


# --- log hygiene ----------------------------------------------------------


def test_csv_import_task_logs_never_contain_phone_or_csv_content(
    make_merchant, make_location, fake_storage, caplog, monkeypatch
):
    from integrations.tasks import import_csv

    monkeypatch.setattr(import_csv, "delay", lambda *a, **k: None)

    owner = make_merchant("A")
    integration = _connect_csv(owner)
    location = make_location(owner.merchant)
    phone = "+919888877766"
    with tenant_context(owner.merchant_id), tenant_atomic():
        services.add_location_mapping(integration, location_id=location.id)
        rows = services.start_csv_import(
            integration=integration,
            file=io.BytesIO((CSV_HEADER + _row("INV-LOG", phone=phone, name="Secret Name")).encode()),
        )
    assert rows == 1
    (key,) = fake_storage.objects.keys()

    with caplog.at_level("WARNING"):
        with tenant_context(owner.merchant_id):
            services.import_csv_rows(integration_id=integration.id, object_key=key)
    for record in caplog.records:
        message = record.getMessage()
        assert phone not in message
        assert "Secret Name" not in message
        assert "INV-LOG" not in message
