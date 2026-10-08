"""configure_shared_pool and the seed_dev WhatsApp step (spec 08 Definition of
done "Seed, config, docs"; Change 1: only these two write the shared row, both
through the audited platform path)."""
import io

import pytest
from django.core.management import CommandError, call_command
from django.db import IntegrityError

from accounts.models import Merchant
from core.tenancy import tenant_atomic, tenant_context
from locations.models import Location
from whatsapp import services
from whatsapp.models import WhatsAppAccount, WhatsAppAccountManager, WhatsAppLocationMapping
from whatsapp.tests.helpers import ACCESS_TOKEN, clear_platform_write_setting, platform_audit_rows, rls_off

pytestmark = pytest.mark.django_db

ARGS = ["--phone-number-id", "109999999999001", "--business-account-id", "TEST-WABA-ID", "--status", "ACTIVE"]


def configure(*args):
    out = io.StringIO()
    call_command("configure_shared_pool", *(args or ARGS), stdout=out)
    return out.getvalue()


def shared():
    return list(WhatsAppAccount.objects.shared_pool())


def shared_audit_rows():
    return [r for r in platform_audit_rows() if r["action"] == "whatsapp.shared_pool_configured"]


def test_configure_creates_the_single_shared_row_and_one_platform_audit_row():
    configure()

    [account] = shared()
    assert (account.phone_number_id, account.business_account_id, account.status) == (
        "109999999999001",
        "TEST-WABA-ID",
        "ACTIVE",
    )
    assert account.merchant_id is None and account.sender_type == "SHARED_POOL"
    [audit] = shared_audit_rows()
    assert audit["metadata"] == {"status": "ACTIVE"}  # status only, never a provider id
    assert "109999999999001" not in str(audit) and "TEST-WABA-ID" not in str(audit)


def test_rerunning_with_the_same_values_is_a_no_op_with_no_second_audit_row():
    configure()
    [first] = shared()

    configure()

    [second] = shared()
    assert second.id == first.id and second.updated_at == first.updated_at
    assert len(shared_audit_rows()) == 1


def test_changing_the_status_updates_the_row_and_writes_one_more_audit_row():
    configure()

    configure("--phone-number-id", "109999999999001", "--business-account-id", "TEST-WABA-ID", "--status", "SUSPENDED")

    [account] = shared()
    assert account.status == "SUSPENDED"
    assert [r["metadata"] for r in shared_audit_rows()] == [{"status": "ACTIVE"}, {"status": "SUSPENDED"}]


def test_the_command_refuses_to_run_inside_a_merchant_context_and_writes_nothing(make_merchant):
    owner = make_merchant("A")

    with tenant_context(owner.merchant_id):
        with pytest.raises(CommandError):
            configure()

    assert shared() == []
    assert shared_audit_rows() == []


def test_the_command_requires_its_ids():
    with pytest.raises(CommandError):
        call_command("configure_shared_pool", "--status", "ACTIVE")


def test_the_command_prints_no_secret(settings):
    settings.META_SHARED_POOL_ACCESS_TOKEN = ACCESS_TOKEN
    settings.META_APP_SECRET = "test-app-secret"

    output = configure()

    assert ACCESS_TOKEN not in output and "test-app-secret" not in output


def test_the_platform_token_is_never_stored_in_the_database(settings):
    settings.META_SHARED_POOL_ACCESS_TOKEN = ACCESS_TOKEN
    configure()

    [account] = shared()
    assert ACCESS_TOKEN not in str(vars(account))
    assert ACCESS_TOKEN not in str(shared_audit_rows())


# --- seed_dev ----------------------------------------------------------------------------------------------


PASSWORD = "Tr1cky-Horse-Battery-Staple!"


@pytest.fixture
def seeded(settings, local_cache):
    settings.DEBUG = True
    call_command("seed_dev", "--password", PASSWORD, stdout=io.StringIO())
    return Merchant.objects.get()


def mapped_locations(merchant):
    with tenant_context(merchant.id), tenant_atomic():
        return {
            m.location_id: m.whatsapp_account.sender_type
            for m in WhatsAppLocationMapping.objects.select_related("whatsapp_account")
        }, set(Location.objects.values_list("id", flat=True))


def test_seed_dev_creates_the_shared_account_and_maps_both_seed_locations_to_it(seeded):
    [account] = shared()
    assert account.status == "ACTIVE"
    assert account.phone_number_id == "DEV-SHARED-PHONE-ID"
    assert account.business_account_id == "DEV-SHARED-WABA-ID"
    mapped, locations = mapped_locations(seeded)
    assert len(locations) == 2
    assert set(mapped) == locations
    assert set(mapped.values()) == {"SHARED_POOL"}


def test_running_seed_dev_again_creates_nothing_new(seeded, settings):
    [before] = shared()
    audits_before = len(platform_audit_rows())
    with tenant_context(seeded.id), tenant_atomic():
        mappings_before = sorted(WhatsAppLocationMapping.objects.values_list("id", "updated_at"))

    call_command("seed_dev", "--password", PASSWORD, stdout=io.StringIO())

    [after] = shared()
    assert (after.id, after.updated_at) == (before.id, before.updated_at)
    assert len(platform_audit_rows()) == audits_before
    with tenant_context(seeded.id), tenant_atomic():
        assert sorted(WhatsAppLocationMapping.objects.values_list("id", "updated_at")) == mappings_before


def test_seed_dev_adds_the_whatsapp_fixtures_to_a_database_seeded_before_phase_08(seeded):
    # Simulate a pre-Phase-08 database: no shared account, no mappings.
    with rls_off("whatsapp_whatsapplocationmapping", "whatsapp_whatsappaccount"):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM whatsapp_whatsapplocationmapping")
            cursor.execute("DELETE FROM whatsapp_whatsappaccount")
    assert shared() == []

    call_command("seed_dev", "--password", PASSWORD, stdout=io.StringIO())

    assert len(shared()) == 1
    mapped, locations = mapped_locations(seeded)
    assert set(mapped) == locations


# --- the first-run race in upsert_shared_pool_account (spec 08 code review) --------------------------


def test_a_lost_first_run_race_continues_as_an_update_with_one_more_audit_row(make_shared_account, monkeypatch):
    make_shared_account("ACTIVE")  # the winner's row (and its audit row) exist
    real = WhatsAppAccountManager.shared_pool
    reads = []

    def first_read_sees_nothing(manager):
        # The loser's first read happened before the winner committed.
        reads.append(1)
        return WhatsAppAccount._base_manager.none() if len(reads) == 1 else real(manager)

    monkeypatch.setattr(WhatsAppAccountManager, "shared_pool", first_read_sees_nothing)

    account = services.upsert_shared_pool_account(
        phone_number_id="109999999999001", business_account_id="TEST-WABA-ID", status="SUSPENDED"
    )
    clear_platform_write_setting()

    monkeypatch.undo()
    [only] = shared()
    assert account.pk == only.pk and only.status == "SUSPENDED"  # an update of the winner's row
    assert [r["metadata"] for r in shared_audit_rows()] == [{"status": "ACTIVE"}, {"status": "SUSPENDED"}]


def test_a_lost_race_with_identical_values_is_a_no_op_with_no_extra_audit_row(make_shared_account, monkeypatch):
    make_shared_account("ACTIVE")
    real = WhatsAppAccountManager.shared_pool
    reads = []

    def first_read_sees_nothing(manager):
        reads.append(1)
        return WhatsAppAccount._base_manager.none() if len(reads) == 1 else real(manager)

    monkeypatch.setattr(WhatsAppAccountManager, "shared_pool", first_read_sees_nothing)

    services.upsert_shared_pool_account(
        phone_number_id="109999999999001", business_account_id="TEST-WABA-ID", status="ACTIVE"
    )
    clear_platform_write_setting()

    monkeypatch.undo()
    assert len(shared_audit_rows()) == 1


def test_a_phone_number_conflict_reraises_the_original_integrity_error_not_does_not_exist(
    make_merchant, make_own_account, meta_settings
):
    own = make_own_account(make_merchant("A").merchant)  # an OWN_NUMBER row already uses this phone_number_id

    with pytest.raises(IntegrityError) as conflict:
        services.upsert_shared_pool_account(
            phone_number_id=own.phone_number_id, business_account_id="TEST-WABA-ID", status="ACTIVE"
        )
    clear_platform_write_setting()

    assert "whatsapp_account_provider_phone_uniq" in str(conflict.value)
    assert not isinstance(conflict.value, WhatsAppAccount.DoesNotExist)
    assert shared() == []
    assert shared_audit_rows() == []


def test_the_command_surfaces_an_unexpected_database_error_instead_of_hiding_it(
    make_merchant, make_own_account, meta_settings
):
    own = make_own_account(make_merchant("A").merchant)

    with pytest.raises(IntegrityError):  # not a CommandError: only expected errors are translated
        configure("--phone-number-id", own.phone_number_id, "--business-account-id", "TEST-WABA-ID")
    clear_platform_write_setting()
