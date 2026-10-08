"""core.tenancy.platform_write_atomic (spec 08 Change 1, Definition of done
"Change 1, platform path" and "Pooled-connection safety").

transaction=True throughout: the properties under test (SET LOCAL ends with
its own outermost transaction; a durable block cannot nest) only exist when
the test is not itself wrapped in a transaction."""
import pytest
from django.db import DatabaseError, connection, transaction

from accounts import services as account_services
from auditlog.services import record_platform
from core.exceptions import TenantContextError
from core.tenancy import get_current_platform_write, platform_write_atomic, tenant_context
from whatsapp.models import WhatsAppAccount
from whatsapp.tests.helpers import platform_audit_rows

pytestmark = pytest.mark.django_db(transaction=True)

SCOPE = "whatsapp_shared_pool"


def create_shared(phone="PW-TEST-PHONE"):
    return WhatsAppAccount._base_manager.create(
        merchant=None,
        sender_type="SHARED_POOL",
        provider="meta_cloud",
        phone_number_id=phone,
        business_account_id="PW-TEST-WABA",
        status="ACTIVE",
    )


def raw(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount


def shared_count():
    return WhatsAppAccount.objects.shared_pool().count()


@pytest.fixture
def owner():
    return account_services.create_merchant_with_owner(
        name="PW Merchant",
        timezone="Asia/Kolkata",
        owner_email="pw-owner@example.com",
        owner_password="Tr1cky-Horse-Battery-Staple!",
    )


def test_outside_platform_write_atomic_the_shared_row_cannot_be_inserted():
    with pytest.raises(DatabaseError), transaction.atomic():
        create_shared()

    assert shared_count() == 0


def test_inside_platform_write_atomic_the_shared_row_can_be_inserted():
    with platform_write_atomic(SCOPE):
        create_shared()

    assert shared_count() == 1


def test_the_platform_write_context_is_visible_inside_and_gone_after_the_block():
    assert get_current_platform_write() is None
    with platform_write_atomic(SCOPE):
        assert get_current_platform_write() == SCOPE
    assert get_current_platform_write() is None


def test_calling_it_while_a_merchant_context_is_active_raises_and_writes_nothing(owner):
    with tenant_context(owner.merchant_id):
        with pytest.raises(TenantContextError):
            with platform_write_atomic(SCOPE):
                create_shared()

    assert shared_count() == 0


def test_an_unknown_scope_raises_and_writes_nothing():
    with pytest.raises(Exception):
        with platform_write_atomic("some_other_scope"):
            create_shared()

    assert shared_count() == 0


def test_it_is_always_its_own_outermost_transaction():
    with transaction.atomic():
        with pytest.raises(Exception):
            with platform_write_atomic(SCOPE):
                create_shared()

    assert shared_count() == 0


def test_the_setting_is_transaction_local_and_does_not_leak_to_the_next_transaction():
    with platform_write_atomic(SCOPE):
        create_shared()

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.platform_write', true)")
            assert cursor.fetchone()[0] in (None, "")
        # a following transaction on the same connection cannot write the shared row
        assert raw("UPDATE whatsapp_whatsappaccount SET status = 'SUSPENDED' WHERE sender_type = 'SHARED_POOL'") == 0
        with pytest.raises(DatabaseError), transaction.atomic():
            create_shared("PW-TEST-PHONE-2")

    assert WhatsAppAccount.objects.shared_pool().get().status == "ACTIVE"


def test_the_platform_update_policy_works_inside_the_block_only():
    with platform_write_atomic(SCOPE):
        create_shared()

    assert raw("UPDATE whatsapp_whatsappaccount SET status = 'PENDING' WHERE sender_type = 'SHARED_POOL'") == 0
    with platform_write_atomic(SCOPE):
        assert raw("UPDATE whatsapp_whatsappaccount SET status = 'PENDING' WHERE sender_type = 'SHARED_POOL'") == 1

    assert WhatsAppAccount.objects.shared_pool().get().status == "PENDING"


def test_nobody_can_delete_the_shared_row_even_inside_the_block():
    with platform_write_atomic(SCOPE):
        create_shared()

    with platform_write_atomic(SCOPE):
        assert raw("DELETE FROM whatsapp_whatsappaccount WHERE sender_type = 'SHARED_POOL'") == 0

    assert shared_count() == 1


def test_inside_the_block_only_a_shared_pool_row_with_no_merchant_is_accepted(owner):
    with platform_write_atomic(SCOPE):
        with pytest.raises(DatabaseError), transaction.atomic():
            WhatsAppAccount._base_manager.create(
                merchant=owner.merchant,
                sender_type="OWN_NUMBER",
                provider="meta_cloud",
                phone_number_id="PW-FORGED-OWN",
                status="ACTIVE",
            )

    assert not WhatsAppAccount._base_manager.filter(phone_number_id="PW-FORGED-OWN").exists()


def test_each_platform_write_produces_exactly_one_platform_audit_row():
    with platform_write_atomic(SCOPE):
        create_shared()
        record_platform("whatsapp.shared_pool_configured", metadata={"status": "ACTIVE"})

    rows = platform_audit_rows()
    assert [(r["action"], r["metadata"]) for r in rows] == [
        ("whatsapp.shared_pool_configured", {"status": "ACTIVE"})
    ]


def test_a_failed_platform_write_rolls_back_its_audit_row_too():
    with pytest.raises(RuntimeError):
        with platform_write_atomic(SCOPE):
            record_platform("whatsapp.shared_pool_configured", metadata={"status": "ACTIVE"})
            raise RuntimeError("boom")

    assert platform_audit_rows() == []
