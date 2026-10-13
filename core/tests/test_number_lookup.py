"""core.tenancy.whatsapp_number_lookup_atomic and the SELECT-only
whatsapp_number_lookup policy (spec 08-whatsapp-embedded-signup ES-3, approved
2026-10-09; Definition of done "Schema & RLS").

transaction=True throughout, as test_platform_write.py: the properties under
test (SET LOCAL ends with its own outermost transaction; a durable block cannot
nest) only exist when the test is not itself wrapped in a transaction."""
import pytest
from django.db import DatabaseError, connection, transaction

from accounts import services as account_services
from core.exceptions import TenantContextError
from core.tenancy import (
    get_current_lookup_phone_number_id,
    platform_write_atomic,
    tenant_atomic,
    tenant_context,
    whatsapp_number_lookup_atomic,
)
from whatsapp import services
from whatsapp.models import WhatsAppAccount

pytestmark = pytest.mark.django_db(transaction=True)

PHONE_A = "200000000000001"
PHONE_B = "200000000000002"
SHARED_PHONE = "100000000000009"


def raw(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount


def rows(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def make_owner(n):
    return account_services.create_merchant_with_owner(
        name=f"Lookup Merchant {n}",
        timezone="Asia/Kolkata",
        owner_email=f"lookup-owner{n}@example.com",
        owner_password="Tr1cky-Horse-Battery-Staple!",
    )


def make_own(owner, phone, status="ACTIVE"):
    with tenant_context(owner.merchant_id), tenant_atomic():
        return WhatsAppAccount.objects.create(
            merchant_id=owner.merchant_id,
            sender_type="OWN_NUMBER",
            provider="meta_cloud",
            phone_number_id=phone,
            business_account_id=f"WABA-{phone}",
            status=status,
        )


@pytest.fixture
def world():
    a, b = make_owner(1), make_owner(2)
    own_a = make_own(a, PHONE_A)
    own_b = make_own(b, PHONE_B)
    with platform_write_atomic("whatsapp_shared_pool"):
        WhatsAppAccount._base_manager.create(
            merchant=None,
            sender_type="SHARED_POOL",
            provider="meta_cloud",
            phone_number_id=SHARED_PHONE,
            business_account_id="SHARED-WABA",
            status="ACTIVE",
        )
    return {"a": a, "b": b, "own_a": own_a, "own_b": own_b}


# --- the context manager -------------------------------------------------------------------------


def test_it_refuses_to_run_inside_a_merchant_context(world):
    with tenant_context(world["a"].merchant_id):
        with pytest.raises(TenantContextError):
            with whatsapp_number_lookup_atomic(PHONE_A):
                pass
    assert get_current_lookup_phone_number_id() is None


@pytest.mark.parametrize(
    "bad",
    ["", "abc", "12 3", "12-3", "1" * 65, "123\n", "١٢٣", None, 123],
)
def test_it_rejects_anything_that_is_not_one_to_sixty_four_ascii_digits(bad):
    with pytest.raises(ValueError):
        with whatsapp_number_lookup_atomic(bad):
            pass
    assert get_current_lookup_phone_number_id() is None


def test_it_is_durable_so_it_cannot_nest_inside_another_transaction():
    with transaction.atomic():
        with pytest.raises(RuntimeError):
            with whatsapp_number_lookup_atomic(PHONE_A):
                pass


def test_the_context_is_visible_inside_and_reset_after_the_block_and_on_error():
    assert get_current_lookup_phone_number_id() is None
    with whatsapp_number_lookup_atomic(PHONE_A):
        assert get_current_lookup_phone_number_id() == PHONE_A
    assert get_current_lookup_phone_number_id() is None

    with pytest.raises(RuntimeError, match="boom"):
        with whatsapp_number_lookup_atomic(PHONE_A):
            raise RuntimeError("boom")
    assert get_current_lookup_phone_number_id() is None


def test_set_local_does_not_leak_to_the_next_transaction_on_the_same_connection(world):
    with whatsapp_number_lookup_atomic(PHONE_A):
        assert rows("SELECT current_setting('app.current_whatsapp_phone_number_id', true)")[0][0] == PHONE_A

    # A following transaction on the same (pooled) connection sees no setting
    # and, with no tenant context, no own-number row at all.
    assert rows("SELECT current_setting('app.current_whatsapp_phone_number_id', true)")[0][0] in (None, "")
    assert rows("SELECT count(*) FROM whatsapp_whatsappaccount WHERE sender_type = 'OWN_NUMBER'")[0][0] == 0


# --- what the policy lets through ----------------------------------------------------------------


def test_inside_the_lookup_exactly_the_one_matching_own_number_is_visible(world):
    with whatsapp_number_lookup_atomic(PHONE_A):
        own = rows("SELECT phone_number_id FROM whatsapp_whatsappaccount WHERE sender_type = 'OWN_NUMBER'")
        everything = rows("SELECT phone_number_id, sender_type FROM whatsapp_whatsappaccount")

    # The new policy adds only the matching OWN_NUMBER row: never the other
    # merchant's number. The shared row is also readable here, but through the
    # foundation's tenant_read policy (merchant_id IS NULL), exactly as it is
    # with no lookup at all; the new policy adds nothing to it.
    assert own == [(PHONE_A,)]
    assert sorted(everything) == sorted([(PHONE_A, "OWN_NUMBER"), (SHARED_PHONE, "SHARED_POOL")])


def test_a_number_that_matches_nothing_reveals_no_own_number(world):
    with whatsapp_number_lookup_atomic("999999999999999"):
        own = rows("SELECT count(*) FROM whatsapp_whatsappaccount WHERE sender_type = 'OWN_NUMBER'")
    assert own[0][0] == 0


def test_the_lookup_does_not_widen_what_the_shared_row_already_exposes(world):
    # With no context at all the shared row is readable (tenant_read) and no
    # own number is. The lookup changes only the second part.
    without = rows("SELECT phone_number_id FROM whatsapp_whatsappaccount")
    assert without == [(SHARED_PHONE,)]


def test_the_shared_pool_phone_number_id_never_matches_as_an_own_number(world):
    with whatsapp_number_lookup_atomic(SHARED_PHONE):
        assert services_visible_merchant_ids(SHARED_PHONE) == []
        assert rows(
            "SELECT count(*) FROM whatsapp_whatsappaccount WHERE sender_type = 'OWN_NUMBER'"
        )[0][0] == 0


def services_visible_merchant_ids(phone):
    return list(
        WhatsAppAccount.objects.for_lookup_phone_number_id(phone).values_list("merchant_id", flat=True)
    )


def test_the_lookup_never_exposes_the_ciphertext_to_the_callers_select_list(world):
    """RLS cannot hide columns, so the application read selects merchant_id
    only: the values_list shape is the contract."""
    with whatsapp_number_lookup_atomic(PHONE_A):
        assert services_visible_merchant_ids(PHONE_A) == [world["a"].merchant_id]


# --- it can never write or enumerate -------------------------------------------------------------


def test_inside_the_lookup_every_write_is_refused_or_matches_nothing(world):
    with whatsapp_number_lookup_atomic(PHONE_A):
        # No UPDATE or DELETE policy matches: 0 rows, never the visible row.
        assert raw("UPDATE whatsapp_whatsappaccount SET status = 'SUSPENDED'") == 0
        assert raw("DELETE FROM whatsapp_whatsappaccount") == 0
        # An INSERT fails every WITH CHECK (no merchant context, no platform flag).
        with pytest.raises(DatabaseError), transaction.atomic():
            raw(
                "INSERT INTO whatsapp_whatsappaccount (id, created_at, updated_at, merchant_id, "
                "sender_type, provider, phone_number_id, status) VALUES (gen_random_uuid(), now(), "
                "now(), %s, 'OWN_NUMBER', 'meta_cloud', %s, 'ACTIVE')",
                [world["a"].merchant_id, "200000000000099"],
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            raw(
                "INSERT INTO whatsapp_whatsappaccount (id, created_at, updated_at, merchant_id, "
                "sender_type, provider, phone_number_id, status) VALUES (gen_random_uuid(), now(), "
                "now(), NULL, 'SHARED_POOL', 'meta_cloud', %s, 'ACTIVE')",
                ["100000000000098"],
            )

    with tenant_context(world["a"].merchant_id), tenant_atomic():
        row = WhatsAppAccount.objects.get(pk=world["own_a"].pk)
        assert row.status == "ACTIVE"


def test_the_manager_mirror_works_only_for_the_active_lookup_id(world):
    with pytest.raises(TenantContextError):
        WhatsAppAccount.objects.for_lookup_phone_number_id(PHONE_A)
    with whatsapp_number_lookup_atomic(PHONE_A):
        with pytest.raises(TenantContextError):
            WhatsAppAccount.objects.for_lookup_phone_number_id(PHONE_B)


def test_without_any_context_the_policy_matches_no_own_number(world):
    assert rows("SELECT count(*) FROM whatsapp_whatsappaccount WHERE sender_type = 'OWN_NUMBER'")[0][0] == 0


# --- the service helper ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["ACTIVE", "PENDING", "SUSPENDED"])
def test_own_number_merchant_id_has_no_status_filter(status):
    owner = make_owner(30)
    make_own(owner, "200000000000777", status=status)

    assert services.own_number_merchant_id("200000000000777") == owner.merchant_id


def test_own_number_merchant_id_is_none_for_unknown_and_non_digit_ids(world):
    assert services.own_number_merchant_id("999999999999999") is None
    assert services.own_number_merchant_id("OWN-PHONE-ID-1") is None
    assert services.own_number_merchant_id("") is None


def test_own_number_merchant_id_returns_the_owning_merchant_only(world):
    assert services.own_number_merchant_id(PHONE_A) == world["a"].merchant_id
    assert services.own_number_merchant_id(PHONE_B) == world["b"].merchant_id


# --- quality-fix pass: own_number_merchant_id no longer swallows unrelated errors ---------------------


def test_is_valid_phone_number_id_is_the_one_digits_only_check():
    from core.tenancy import is_valid_phone_number_id

    for good in ("1", "200000000000001", "9" * 64):
        assert is_valid_phone_number_id(good) is True
    for bad in ("", "abc", "12 3", "1" * 65, "123\n", "\u0661\u0662\u0663", None, 123, b"123"):
        assert is_valid_phone_number_id(bad) is False


@pytest.mark.parametrize("bad", ["", "abc", "12 3", "1" * 65, "123\n", None])
def test_an_id_that_cannot_be_an_own_number_returns_none_without_opening_the_lookup(world, monkeypatch, bad):
    def must_not_run(*args, **kwargs):
        raise AssertionError("the lookup must not be entered for an invalid id")

    monkeypatch.setattr(services, "whatsapp_number_lookup_atomic", must_not_run)

    assert services.own_number_merchant_id(bad) is None


def test_an_unexpected_value_error_inside_the_lookup_is_not_mistaken_for_an_unknown_number(world, monkeypatch):
    from whatsapp.models import WhatsAppAccountManager

    def boom(self, phone_number_id):
        raise ValueError("unrelated bug")

    monkeypatch.setattr(WhatsAppAccountManager, "for_lookup_phone_number_id", boom)

    with pytest.raises(ValueError, match="unrelated bug"):
        services.own_number_merchant_id(PHONE_A)
