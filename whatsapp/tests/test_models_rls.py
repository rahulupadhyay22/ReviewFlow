"""Schema, constraints and RLS of the three whatsapp tables (spec 08 Definition
of done "Schema & RLS", Change 1; Multi-Tenancy.md; Testing-Strategy.md
Tenant Isolation + RLS). Raw SQL is used on purpose: it bypasses
TenantScopedManager, so only RLS can stop it."""
import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction

from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context
from whatsapp import services
from whatsapp.models import MessageTemplate, WhatsAppAccount, WhatsAppLocationMapping
from whatsapp.tests.helpers import SHARED_PHONE_NUMBER_ID, rls_off

pytestmark = pytest.mark.django_db

TABLES = [
    "whatsapp_whatsappaccount",
    "whatsapp_whatsapplocationmapping",
    "whatsapp_messagetemplate",
]


def _sql(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _rowcount(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount


def _clear_merchant_context():
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.current_merchant_id', '', true)")


def test_rls_is_enabled_and_forced_on_all_three_tables():
    rows = _sql(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(%s)",
        [TABLES],
    )
    assert sorted(rows) == sorted((t, True, True) for t in TABLES)


def test_whatsappaccount_has_exactly_the_six_change_1_policies():
    rows = _sql("SELECT policyname, cmd FROM pg_policies WHERE tablename = 'whatsapp_whatsappaccount'")
    assert sorted(rows) == sorted(
        [
            ("tenant_read", "SELECT"),
            ("tenant_insert", "INSERT"),
            ("tenant_update", "UPDATE"),
            ("tenant_delete", "DELETE"),
            ("platform_insert", "INSERT"),
            ("platform_update", "UPDATE"),
        ]
    )


def test_raw_query_on_accounts_returns_own_rows_plus_shared_but_not_other_merchants(
    make_merchant, make_own_account, shared_account
):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    own_a = make_own_account(owner_a.merchant)
    own_b = make_own_account(owner_b.merchant)

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        ids = {row[0] for row in _sql("SELECT id FROM whatsapp_whatsappaccount")}

    assert ids == {own_a.id, shared_account.id}
    assert own_b.id not in ids


def test_shared_row_is_readable_by_raw_query_without_any_merchant_context(shared_account):
    _clear_merchant_context()
    ids = {row[0] for row in _sql("SELECT id FROM whatsapp_whatsappaccount")}
    assert ids == {shared_account.id}


def test_raw_query_on_templates_returns_only_the_current_merchants_rows(make_merchant):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    for owner in (owner_a, owner_b):
        with tenant_context(owner.merchant_id), tenant_atomic():
            MessageTemplate.objects.create(
                merchant_id=owner.merchant_id, name="t", language="en", body="{{business_name}}"
            )

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        merchants = {row[0] for row in _sql("SELECT merchant_id FROM whatsapp_messagetemplate")}

    assert merchants == {owner_a.merchant_id}


def test_raw_query_on_mappings_returns_only_mappings_of_the_current_merchants_locations(
    make_merchant, make_location, shared_account
):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    loc_a, loc_b = make_location(owner_a.merchant), make_location(owner_b.merchant)
    for owner, loc in ((owner_a, loc_a), (owner_b, loc_b)):
        with tenant_context(owner.merchant_id):
            services.set_location_sender(location=loc, account=shared_account, actor=owner.user)

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        locations = {row[0] for row in _sql("SELECT location_id FROM whatsapp_whatsapplocationmapping")}

    assert locations == {loc_a.id}


def test_tenant_cannot_insert_a_shared_pool_row(make_merchant):
    owner = make_merchant("A")

    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(DatabaseError), transaction.atomic():
            WhatsAppAccount._base_manager.create(
                merchant=None,
                sender_type="SHARED_POOL",
                provider="meta_cloud",
                phone_number_id="FORGED-SHARED",
                status="ACTIVE",
            )

    assert not WhatsAppAccount.objects.shared_pool().exists()


def test_tenant_update_and_delete_of_the_shared_row_affect_zero_rows(make_merchant, shared_account):
    owner = make_merchant("A")

    with tenant_context(owner.merchant_id), tenant_atomic():
        updated = _rowcount("UPDATE whatsapp_whatsappaccount SET status = 'SUSPENDED' WHERE id = %s", [shared_account.id])
        deleted = _rowcount("DELETE FROM whatsapp_whatsappaccount WHERE id = %s", [shared_account.id])

    assert (updated, deleted) == (0, 0)
    assert WhatsAppAccount.objects.shared_pool().get().status == "ACTIVE"


def test_tenant_cannot_insert_an_own_number_row_for_another_merchant(make_merchant):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(DatabaseError), transaction.atomic():
            WhatsAppAccount._base_manager.create(
                merchant=owner_b.merchant,
                sender_type="OWN_NUMBER",
                provider="meta_cloud",
                phone_number_id="FORGED-OWN",
                status="ACTIVE",
            )


def test_tenant_cannot_convert_its_own_row_into_a_shared_one(make_merchant, make_own_account):
    owner = make_merchant("A")
    own = make_own_account(owner.merchant)

    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(DatabaseError), transaction.atomic():
            _rowcount(
                "UPDATE whatsapp_whatsappaccount SET merchant_id = NULL, sender_type = 'SHARED_POOL' WHERE id = %s",
                [own.id],
            )

    assert not WhatsAppAccount.objects.shared_pool().exists()


def test_a_tenant_can_still_update_and_delete_its_own_number_row(make_merchant, make_own_account):
    owner = make_merchant("A")
    own = make_own_account(owner.merchant)

    with tenant_context(owner.merchant_id), tenant_atomic():
        updated = _rowcount("UPDATE whatsapp_whatsappaccount SET status = 'SUSPENDED' WHERE id = %s", [own.id])
        deleted = _rowcount("DELETE FROM whatsapp_whatsappaccount WHERE id = %s", [own.id])

    assert (updated, deleted) == (1, 1)


def test_shared_pool_manager_works_without_context_but_the_default_query_raises(shared_account):
    # no tenant context active
    assert list(WhatsAppAccount.objects.shared_pool().values_list("id", flat=True)) == [shared_account.id]
    with pytest.raises(TenantContextError):
        list(WhatsAppAccount.objects.all())


def test_tenant_manager_sees_own_and_shared_rows_only(make_merchant, make_own_account, shared_account):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    own_a = make_own_account(owner_a.merchant)
    make_own_account(owner_b.merchant)

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        ids = set(WhatsAppAccount.objects.values_list("id", flat=True))

    assert ids == {own_a.id, shared_account.id}


def test_shared_pool_manager_never_returns_own_number_rows(make_merchant, make_own_account, shared_account):
    make_own_account(make_merchant("A").merchant)
    assert list(WhatsAppAccount.objects.shared_pool().values_list("id", flat=True)) == [shared_account.id]


def test_check_constraint_rejects_shared_pool_with_a_merchant(make_merchant):
    owner = make_merchant("A")
    with rls_off("whatsapp_whatsappaccount"):
        with pytest.raises(IntegrityError), transaction.atomic():
            WhatsAppAccount._base_manager.create(
                merchant=owner.merchant,
                sender_type="SHARED_POOL",
                provider="meta_cloud",
                phone_number_id="X1",
                status="ACTIVE",
            )


def test_check_constraint_rejects_own_number_without_a_merchant():
    with rls_off("whatsapp_whatsappaccount"):
        with pytest.raises(IntegrityError), transaction.atomic():
            WhatsAppAccount._base_manager.create(
                merchant=None,
                sender_type="OWN_NUMBER",
                provider="meta_cloud",
                phone_number_id="X2",
                status="ACTIVE",
            )


def test_a_second_shared_pool_row_is_rejected(shared_account):
    with rls_off("whatsapp_whatsappaccount"):
        with pytest.raises(IntegrityError), transaction.atomic():
            WhatsAppAccount._base_manager.create(
                merchant=None,
                sender_type="SHARED_POOL",
                provider="meta_cloud",
                phone_number_id="ANOTHER-SHARED",
                status="ACTIVE",
            )


def test_duplicate_provider_and_phone_number_id_is_rejected(make_merchant, make_own_account, shared_account):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    make_own_account(owner_a.merchant)

    with tenant_context(owner_b.merchant_id), tenant_atomic():
        with pytest.raises(IntegrityError), transaction.atomic():
            WhatsAppAccount.objects.create(
                merchant=owner_b.merchant,
                sender_type="OWN_NUMBER",
                provider="meta_cloud",
                phone_number_id="OWN-PHONE-ID-1",  # make_own_account's first number
                status="ACTIVE",
            )

    with rls_off("whatsapp_whatsappaccount"):
        with pytest.raises(IntegrityError), transaction.atomic():
            WhatsAppAccount._base_manager.create(
                merchant=owner_b.merchant,
                sender_type="OWN_NUMBER",
                provider="meta_cloud",
                phone_number_id=SHARED_PHONE_NUMBER_ID,
                status="ACTIVE",
            )


def test_one_mapping_per_location(make_merchant, make_location, shared_account, make_own_account):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    own = make_own_account(owner.merchant)
    with tenant_context(owner.merchant_id), tenant_atomic():
        WhatsAppLocationMapping.objects.create(whatsapp_account=shared_account, location=location)
        with pytest.raises(IntegrityError), transaction.atomic():
            WhatsAppLocationMapping.objects.create(whatsapp_account=own, location=location)


def test_template_name_and_language_are_unique_per_merchant_but_not_across_merchants(make_merchant):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    kwargs = dict(name="Review ask", language="en", body="{{business_name}}")
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        MessageTemplate.objects.create(merchant_id=owner_a.merchant_id, **kwargs)
        with pytest.raises(IntegrityError), transaction.atomic():
            MessageTemplate.objects.create(merchant_id=owner_a.merchant_id, **kwargs)
        MessageTemplate.objects.create(merchant_id=owner_a.merchant_id, **{**kwargs, "language": "hi"})
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        MessageTemplate.objects.create(merchant_id=owner_b.merchant_id, **kwargs)


def test_provider_template_id_is_unique_when_set_and_repeatable_when_null(make_merchant):
    owner = make_merchant("A")
    with tenant_context(owner.merchant_id), tenant_atomic():
        for n in range(2):
            MessageTemplate.objects.create(
                merchant_id=owner.merchant_id, name=f"n{n}", language="en", body="{{business_name}}"
            )
        MessageTemplate.objects.create(
            merchant_id=owner.merchant_id, name="s1", language="en", body="x", provider_template_id="P-1"
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            MessageTemplate.objects.create(
                merchant_id=owner.merchant_id, name="s2", language="en", body="x", provider_template_id="P-1"
            )


def test_tenant_manager_hides_other_merchants_templates_and_mappings(
    make_merchant, make_location, shared_account
):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    loc_b = make_location(owner_b.merchant)
    with tenant_context(owner_b.merchant_id):
        services.set_location_sender(location=loc_b, account=shared_account, actor=owner_b.user)
    with tenant_context(owner_b.merchant_id), tenant_atomic():
        MessageTemplate.objects.create(
            merchant_id=owner_b.merchant_id, name="t", language="en", body="{{business_name}}"
        )

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        assert not MessageTemplate.objects.exists()
        assert not WhatsAppLocationMapping.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_platform_write_setting_does_not_leak_to_the_next_transaction_on_the_same_connection(shared_account):
    # shared_account was written inside platform_write_atomic and committed.
    # A following transaction on the same pooled connection must not be able
    # to write the shared row (SET LOCAL did not leak).
    with transaction.atomic():
        assert _sql("SELECT current_setting('app.platform_write', true)")[0][0] in (None, "")
        updated = _rowcount(
            "UPDATE whatsapp_whatsappaccount SET status = 'SUSPENDED' WHERE sender_type = 'SHARED_POOL'"
        )
    assert updated == 0
    assert WhatsAppAccount.objects.shared_pool().get().status == "ACTIVE"
