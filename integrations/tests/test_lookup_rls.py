"""integration_lookup RLS policy and core.tenancy.integration_lookup_atomic
(spec .claude/specs/06-priority-integrations.md Decision 1 -- LOCKED
DECISION CHANGE, approved). Mirrors apikeys/tests/test_key_lookup_rls.py's
pattern for the api_key_lookup policy."""
import pytest
from django.db import Error, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from core.exceptions import TenantContextError
from core.tenancy import integration_lookup_atomic, tenant_atomic, tenant_context
from integrations import services
from integrations.models import Integration

pytestmark = pytest.mark.django_db(transaction=True)


def _ids(sql, params=()):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return {str(r[0]) for r in cur.fetchall()}


def _setting(name):
    with connection.cursor() as cur:
        cur.execute("SELECT current_setting(%s, true)", [name])
        return cur.fetchone()[0]


def _connect(owner):
    with tenant_context(owner.merchant_id), tenant_atomic():
        integration, _ = services.connect_integration(actor=owner, provider="webhook")
        return integration


# --- Migrations ---------------------------------------------------------


def test_integrations_migrations_0003_to_0005_apply_and_reverse_cleanly():
    """spec Definition of done §"Migrations and RLS": `migrate integrations
    0002` reverses 0003-0005 cleanly, and re-applying restores the `api`
    provider choice, the partial unique constraint and the
    integration_lookup policy. Mirrors
    apikeys/tests/test_key_lookup_rls.py's migration-reversibility test."""
    executor = MigrationExecutor(connection)
    leaf = executor.loader.graph.leaf_nodes("integrations")
    try:
        executor.migrate([("integrations", "0002_integrations_rls")])
        with connection.cursor() as cur:
            cur.execute(
                "SELECT policyname FROM pg_policies WHERE tablename = 'integrations_integration'"
            )
            policies = {r[0] for r in cur.fetchall()}
        assert "integration_lookup" not in policies

        executor = MigrationExecutor(connection)
        executor.migrate(leaf)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT policyname FROM pg_policies WHERE tablename = 'integrations_integration'"
            )
            policies = {r[0] for r in cur.fetchall()}
        assert policies == {"tenant_isolation", "integration_lookup"}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf)


# --- RLS policy shape -------------------------------------------------


def test_integrations_integration_rls_enabled_forced_with_exactly_two_policies():
    with connection.cursor() as cur:
        cur.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'integrations_integration'"
        )
        row_security, force_security = cur.fetchone()
        assert row_security is True
        assert force_security is True

        cur.execute(
            "SELECT policyname, cmd FROM pg_policies WHERE tablename = 'integrations_integration'"
        )
        policies = dict(cur.fetchall())
    assert policies == {"tenant_isolation": "ALL", "integration_lookup": "SELECT"}


# --- integration_lookup policy -----------------------------------------


def test_lookup_atomic_select_returns_only_the_matching_id_row(make_merchant, register_webhook_provider):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_a = _connect(owner_a)
    integration_b = _connect(owner_b)

    with integration_lookup_atomic(integration_a.id):
        ids = _ids("SELECT id FROM integrations_integration")
    assert ids == {str(integration_a.id)}
    assert str(integration_b.id) not in ids


def test_lookup_atomic_update_and_delete_affect_zero_rows(make_merchant, register_webhook_provider):
    owner_a = make_merchant("A")
    integration_a = _connect(owner_a)

    with integration_lookup_atomic(integration_a.id):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE integrations_integration SET status = 'DISCONNECTED' WHERE id = %s",
                [str(integration_a.id)],
            )
            assert cur.rowcount == 0
            cur.execute("DELETE FROM integrations_integration WHERE id = %s", [str(integration_a.id)])
            assert cur.rowcount == 0

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        assert Integration.objects.get(id=integration_a.id).status == Integration.Status.CONNECTED


def test_lookup_atomic_mappings_and_events_are_invisible(make_merchant, make_location, register_webhook_provider):
    owner_a = make_merchant("A")
    integration_a = _connect(owner_a)
    loc = make_location(owner_a.merchant)
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        services.add_location_mapping(integration_a, location_id=loc.id)

    with integration_lookup_atomic(integration_a.id):
        assert _ids("SELECT id FROM integrations_integrationlocationmapping") == set()
        assert _ids("SELECT id FROM events_integrationevent") == set()


def test_lookup_atomic_setting_is_reset_after_transaction(make_merchant, register_webhook_provider):
    owner_a = make_merchant("A")
    integration_a = _connect(owner_a)
    with integration_lookup_atomic(integration_a.id):
        assert _setting("app.current_integration_id") == str(integration_a.id)
    assert _setting("app.current_integration_id") in (None, "")


def test_lookup_atomic_setting_is_reset_after_failed_transaction():
    import uuid

    with pytest.raises(RuntimeError):
        with integration_lookup_atomic(uuid.uuid4()):
            raise RuntimeError("boom")
    assert _setting("app.current_integration_id") in (None, "")


def test_lookup_atomic_with_a_wrong_id_sees_no_rows(make_merchant, register_webhook_provider):
    import uuid

    owner_a = make_merchant("A")
    _connect(owner_a)
    with integration_lookup_atomic(uuid.uuid4()):
        assert _ids("SELECT id FROM integrations_integration") == set()


def test_lookup_atomic_insert_is_rejected(make_merchant, register_webhook_provider):
    """No write policy is keyed on app.current_integration_id."""
    owner_a = make_merchant("A")
    integration_a = _connect(owner_a)
    with integration_lookup_atomic(integration_a.id):
        with pytest.raises(Error), transaction.atomic():
            Integration(
                merchant_id=owner_a.merchant_id, provider="webhook", status="CONNECTED"
            ).save(force_insert=True)


def test_lookup_atomic_raises_tenant_context_error_inside_active_merchant_context(make_merchant):
    import uuid

    owner_a = make_merchant("A")
    with tenant_context(owner_a.merchant_id):
        with pytest.raises(TenantContextError):
            with integration_lookup_atomic(uuid.uuid4()):
                pass


def test_lookup_atomic_inside_tenant_atomic_raises_and_sets_nothing(make_merchant):
    import uuid

    owner_a = make_merchant("A")
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(TenantContextError):
            with integration_lookup_atomic(uuid.uuid4()):
                pass
        assert _setting("app.current_integration_id") in (None, "")


@pytest.mark.parametrize(
    "bad_id",
    ["not-a-uuid", "", None, "x'; DROP TABLE integrations_integration; --"],
)
def test_lookup_atomic_raises_valueerror_for_a_malformed_id(bad_id):
    with pytest.raises(ValueError):
        with integration_lookup_atomic(bad_id):
            pass


def test_lookup_atomic_raises_runtimeerror_when_nested_in_another_atomic():
    import uuid

    with transaction.atomic():
        with pytest.raises(RuntimeError):
            with integration_lookup_atomic(uuid.uuid4()):
                pass
        assert _setting("app.current_integration_id") in (None, "")


def test_for_lookup_id_raises_outside_lookup_atomic(make_merchant, register_webhook_provider):
    owner_a = make_merchant("A")
    integration_a = _connect(owner_a)
    with pytest.raises(TenantContextError):
        Integration.objects.for_lookup_id(integration_a.id)


def test_for_lookup_id_raises_for_a_different_id(make_merchant, register_webhook_provider):
    import uuid

    owner_a = make_merchant("A")
    integration_a = _connect(owner_a)
    with integration_lookup_atomic(uuid.uuid4()):
        with pytest.raises(TenantContextError):
            Integration.objects.for_lookup_id(integration_a.id)


def test_for_lookup_id_returns_only_that_ids_row(make_merchant, register_webhook_provider):
    owner_a = make_merchant("A")
    owner_b = make_merchant("B")
    integration_a = _connect(owner_a)
    _connect(owner_b)
    with integration_lookup_atomic(integration_a.id):
        rows = list(Integration.objects.for_lookup_id(integration_a.id))
    assert [r.id for r in rows] == [integration_a.id]


def test_integration_objects_all_without_tenant_context_raises():
    with pytest.raises(TenantContextError):
        list(Integration.objects.all())


# --- Pooled connection ----------------------------------------------------


def test_pooled_connection_sees_no_tenant_rows_after_lookup_atomic_block(make_merchant, register_webhook_provider):
    owner_a = make_merchant("A")
    integration_a = _connect(owner_a)
    with integration_lookup_atomic(integration_a.id):
        pass
    # A brand-new transaction on the same connection, with no context set.
    with transaction.atomic():
        assert _ids("SELECT id FROM integrations_integration") == set()
