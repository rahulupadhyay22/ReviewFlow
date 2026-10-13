"""Schema of the own-number additions (spec 08-whatsapp-embedded-signup,
Definition of done "Schema & RLS"): the access_token_enc column and CHECK, the
template-to-WABA binding with its two partial uniques, and tenant isolation of
the ciphertext. Migration round-trip is exercised by the migrate commands in
the phase log, plus config/tests/test_scaffold.py (no missing migrations)."""
import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction

from core.tenancy import platform_write_atomic, tenant_atomic, tenant_context
from whatsapp.models import MessageTemplate, WhatsAppAccount

pytestmark = pytest.mark.django_db


def raw(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount


def raw_rows(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def make_template(merchant, *, name="Ask", language="en", account=None):
    with tenant_context(merchant.id), tenant_atomic():
        return MessageTemplate.objects.create(
            merchant=merchant,
            name=name,
            language=language,
            body="Hi {{business_name}}",
            whatsapp_account=account,
        )


# --- access_token_enc and its CHECK ---------------------------------------------------------------


def test_an_own_number_can_hold_an_encrypted_token_that_round_trips(make_merchant, make_own_account, fernet_key):
    merchant = make_merchant("A").merchant

    account = make_own_account(merchant, token="EAA-secret-business-token")

    with tenant_context(merchant.id), tenant_atomic():
        stored = WhatsAppAccount.objects.get(pk=account.pk).access_token_enc
    assert stored and "EAA-secret-business-token" not in stored
    from core.crypto import decrypt

    assert decrypt(stored) == "EAA-secret-business-token"


def test_the_shared_row_can_never_hold_a_token(shared_account):
    with pytest.raises(IntegrityError):
        with platform_write_atomic("whatsapp_shared_pool"):
            WhatsAppAccount._base_manager.filter(pk=shared_account.pk).update(access_token_enc="x")


def test_a_new_shared_row_with_a_token_violates_the_check(meta_settings):
    with pytest.raises(IntegrityError):
        with platform_write_atomic("whatsapp_shared_pool"):
            WhatsAppAccount._base_manager.create(
                merchant=None,
                sender_type="SHARED_POOL",
                provider="meta_cloud",
                phone_number_id="SCHEMA-SHARED",
                business_account_id="SCHEMA-WABA",
                status="ACTIVE",
                access_token_enc="ciphertext",
            )


def test_merchant_b_cannot_read_update_or_delete_merchant_as_own_row_or_ciphertext(
    make_merchant, make_own_account, fernet_key
):
    a, b = make_merchant("A").merchant, make_merchant("B").merchant
    account = make_own_account(a, token="a-token")

    with tenant_context(b.id), tenant_atomic():
        assert raw_rows(
            "SELECT count(*) FROM whatsapp_whatsappaccount WHERE id = %s", [str(account.pk)]
        )[0][0] == 0
        assert raw_rows(
            "SELECT access_token_enc FROM whatsapp_whatsappaccount WHERE id = %s", [str(account.pk)]
        ) == []
        assert raw("UPDATE whatsapp_whatsappaccount SET access_token_enc = NULL WHERE id = %s", [str(account.pk)]) == 0
        assert raw("DELETE FROM whatsapp_whatsappaccount WHERE id = %s", [str(account.pk)]) == 0

    with tenant_context(a.id), tenant_atomic():
        assert WhatsAppAccount.objects.get(pk=account.pk).access_token_enc


def test_merchant_b_cannot_insert_an_own_number_for_merchant_a(make_merchant):
    a, b = make_merchant("A").merchant, make_merchant("B").merchant
    with tenant_context(b.id), tenant_atomic():
        with pytest.raises(DatabaseError), transaction.atomic():
            WhatsAppAccount.objects.create(
                merchant_id=a.id,
                sender_type="OWN_NUMBER",
                provider="meta_cloud",
                phone_number_id="200000000000123",
                status="ACTIVE",
            )


def test_platform_write_scope_allowlist_is_pinned():
    import core.tenancy as tenancy

    # Spec 08 added no scope. "staff_auth" is spec 16 LC-1 (LOCKED DECISION
    # CHANGE, approved 2026-10-13); any further scope needs fresh sign-off.
    assert tenancy._PLATFORM_WRITE_SCOPES == frozenset({"whatsapp_shared_pool", "staff_auth"})


# --- template <-> WABA binding and its partial uniques ------------------------------------------------


def test_every_existing_style_template_has_a_null_account_and_stays_unique(make_merchant):
    merchant = make_merchant("A").merchant
    make_template(merchant)

    assert make_template(merchant, name="Other").whatsapp_account_id is None
    with pytest.raises(IntegrityError), transaction.atomic():
        make_template(merchant)  # same (merchant, name, language), both shared


def test_the_same_name_can_exist_on_the_shared_waba_and_on_an_own_account(make_merchant, make_own_account):
    merchant = make_merchant("A").merchant
    own = make_own_account(merchant)

    make_template(merchant)  # shared
    on_own = make_template(merchant, account=own)  # same name and language

    assert on_own.whatsapp_account_id == own.pk


def test_a_duplicate_on_the_same_own_account_is_rejected(make_merchant, make_own_account):
    merchant = make_merchant("A").merchant
    own = make_own_account(merchant)
    make_template(merchant, account=own)

    with pytest.raises(IntegrityError), transaction.atomic():
        make_template(merchant, account=own)


def test_the_same_name_on_two_different_own_accounts_is_allowed(make_merchant, make_own_account):
    merchant = make_merchant("A").merchant
    first, second = make_own_account(merchant), make_own_account(merchant)

    make_template(merchant, account=first)
    make_template(merchant, account=second)


def test_the_old_single_unique_constraint_is_gone():
    names = {
        row[0]
        for row in raw_rows(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'whatsapp_messagetemplate'::regclass"
        )
    }
    indexes = {row[0] for row in raw_rows("SELECT indexname FROM pg_indexes WHERE tablename = 'whatsapp_messagetemplate'")}
    assert "whatsapp_template_merchant_name_lang_uniq" not in names | indexes
    assert {"whatsapp_template_shared_name_lang_uniq", "whatsapp_template_own_name_lang_uniq"} <= names | indexes
