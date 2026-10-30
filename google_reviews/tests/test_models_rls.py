"""Schema, constraints and RLS of the four google_reviews tables (spec 09
Definition of done "Schema & RLS"; Multi-Tenancy.md; Testing-Strategy.md Tenant
Isolation + RLS). Raw SQL is used on purpose: it bypasses TenantScopedManager, so
only RLS can stop it."""
from datetime import timedelta

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from core.crypto import encrypt
from core.tenancy import tenant_atomic, tenant_context
from google_reviews.models import GoogleConnection, GoogleLocation, GoogleOAuthState, GoogleReview
from google_reviews.tests.conftest import GOOGLE_A, GOOGLE_B
from google_reviews.tests.helpers import GOOGLE_TABLES, make_state

pytestmark = pytest.mark.django_db(transaction=True)

MODELS = {
    "google_reviews_googleconnection": GoogleConnection,
    "google_reviews_googleoauthstate": GoogleOAuthState,
    "google_reviews_googlelocation": GoogleLocation,
    "google_reviews_googlereview": GoogleReview,
}


def sql(query, params=None):
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


@pytest.fixture
def two_merchants(make_merchant, make_location, make_connection, make_mapping):
    worlds = {}
    for name in ("A", "B"):
        owner = make_merchant(name)
        location = make_location(owner.merchant)
        conn = make_connection(owner.merchant)
        worlds[name] = {
            "owner": owner,
            "location": location,
            "connection": conn,
            "mapping": make_mapping(owner.merchant, location, conn, GOOGLE_A, reviews=2),
            "state": make_state(owner),
        }
    return worlds


def test_rls_is_enabled_and_forced_on_all_four_tables():
    rows = sql("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(%s)", [GOOGLE_TABLES])
    assert sorted(rows) == sorted((t, True, True) for t in GOOGLE_TABLES)


def test_googlereview_has_no_reply_columns():
    columns = {
        row[0]
        for row in sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'google_reviews_googlereview'"
        )
    }
    assert "reply_text" not in columns and "reply_status" not in columns
    assert {"google_location_id", "google_review_id", "rating", "review_created_at", "fetched_at"} <= columns


@pytest.mark.parametrize("table", GOOGLE_TABLES)
def test_manager_and_raw_select_return_none_of_the_other_merchants_rows(two_merchants, table):
    a, b = two_merchants["A"], two_merchants["B"]
    model = MODELS[table]

    with tenant_context(b["owner"].merchant_id), tenant_atomic():
        b_ids = set(model.objects.values_list("id", flat=True))
    with tenant_context(a["owner"].merchant_id), tenant_atomic():
        manager_ids = set(model.objects.values_list("id", flat=True))
        raw_ids = {row[0] for row in sql(f"SELECT id FROM {table}")}

    assert b_ids and not (b_ids & manager_ids)
    assert raw_ids == manager_ids
    assert not (b_ids & raw_ids)


def test_raw_insert_of_a_review_under_another_merchants_google_location_fails_rls(two_merchants):
    a, b = two_merchants["A"], two_merchants["B"]
    with tenant_context(a["owner"].merchant_id), tenant_atomic():
        with pytest.raises(DatabaseError), transaction.atomic():
            sql(
                "INSERT INTO google_reviews_googlereview (id, created_at, updated_at, google_location_id, "
                "google_review_id, rating, review_created_at, fetched_at) "
                "VALUES (gen_random_uuid(), now(), now(), %s, 'forged', 5, now(), now())",
                [b["mapping"].pk],
            )
    with tenant_context(b["owner"].merchant_id), tenant_atomic():
        assert not GoogleReview.objects.filter(google_review_id="forged").exists()


def test_raw_insert_of_a_google_location_for_another_merchants_location_fails_rls(two_merchants):
    a, b = two_merchants["A"], two_merchants["B"]
    with tenant_context(a["owner"].merchant_id), tenant_atomic():
        with pytest.raises(DatabaseError), transaction.atomic():
            sql(
                "INSERT INTO google_reviews_googlelocation (id, created_at, updated_at, location_id, "
                "google_connection_id, google_location_id, review_link) "
                "VALUES (gen_random_uuid(), now(), now(), %s, %s, 'accounts/9/locations/9', 'https://g.page/r/x/review')",
                [b["location"].id, a["connection"].id],
            )


def test_raw_insert_of_a_connection_and_a_state_for_another_merchant_fails_rls(two_merchants):
    a, b = two_merchants["A"], two_merchants["B"]
    with tenant_context(a["owner"].merchant_id), tenant_atomic():
        with pytest.raises(DatabaseError), transaction.atomic():
            sql(
                "INSERT INTO google_reviews_googleconnection (id, created_at, updated_at, merchant_id, "
                "google_account_sub, google_account_email, access_token_enc, refresh_token_enc, expires_at, status) "
                "VALUES (gen_random_uuid(), now(), now(), %s, 's', 'e@example.com', 'a', 'r', now(), 'ACTIVE')",
                [b["owner"].merchant_id],
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            sql(
                "INSERT INTO google_reviews_googleoauthstate (id, created_at, updated_at, merchant_id, user_id, "
                "state_hash, nonce_hash, status, expires_at) "
                "VALUES (gen_random_uuid(), now(), now(), %s, %s, 'h', 'n', 'ISSUED', now())",
                [b["owner"].merchant_id, a["owner"].user.pk],
            )


def test_a_new_transaction_with_no_context_sees_no_google_rows(two_merchants):
    # Rows are committed. The tenant transactions above ended, so SET LOCAL
    # must not leak to the next transaction on this pooled connection.
    with transaction.atomic():
        sql("SELECT set_config('app.current_merchant_id', '', true)")
        counts = {table: sql(f"SELECT count(*) FROM {table}")[0][0] for table in GOOGLE_TABLES}
    assert counts == {table: 0 for table in GOOGLE_TABLES}


def test_the_same_review_id_twice_in_a_location_is_an_integrity_error(two_merchants):
    a = two_merchants["A"]
    with tenant_context(a["owner"].merchant_id), tenant_atomic():
        existing = GoogleReview.objects.filter(google_location=a["mapping"]).first()
        with pytest.raises(IntegrityError), transaction.atomic():
            GoogleReview.objects.create(
                google_location=a["mapping"],
                google_review_id=existing.google_review_id,
                rating=4,
                review_created_at=timezone.now(),
                fetched_at=timezone.now(),
            )


def test_google_location_uniques_hold(two_merchants, make_location):
    a = two_merchants["A"]
    other = make_location(a["owner"].merchant, "Second")
    with tenant_context(a["owner"].merchant_id), tenant_atomic():
        common = dict(google_connection=a["connection"], review_link="https://g.page/r/x/review")
        with pytest.raises(IntegrityError), transaction.atomic():  # UNIQUE(location)
            GoogleLocation.objects.create(location=a["location"], google_location_id=GOOGLE_B, **common)
        with pytest.raises(IntegrityError), transaction.atomic():  # UNIQUE(connection, google_location_id)
            GoogleLocation.objects.create(location=other, google_location_id=GOOGLE_A, **common)
        GoogleLocation.objects.create(location=other, google_location_id=GOOGLE_B, **common)


def test_connection_sub_is_unique_per_merchant_but_not_across_merchants(two_merchants, make_connection):
    a, b = two_merchants["A"], two_merchants["B"]
    sub = a["connection"].google_account_sub
    with tenant_context(a["owner"].merchant_id), tenant_atomic():
        with pytest.raises(IntegrityError), transaction.atomic():
            GoogleConnection.objects.create(
                merchant=a["owner"].merchant,
                google_account_sub=sub,
                google_account_email="dup@example.com",
                access_token_enc="a",
                refresh_token_enc="r",
                expires_at=timezone.now(),
            )
    # the same sub under B already exists (both worlds use make_connection's default)
    with tenant_context(b["owner"].merchant_id), tenant_atomic():
        assert GoogleConnection.objects.filter(google_account_sub=sub).count() == 1


CHECK_CASES = [
    ("code outside PENDING", dict(status="ISSUED", code_enc="x")),
    ("FAILED without failure_code", dict(status="FAILED", failure_code=None)),
    ("failure_code without FAILED", dict(status="PENDING", failure_code="EXCHANGE_FAILED")),
    ("COMPLETED without a connection", dict(status="COMPLETED")),
]


@pytest.mark.parametrize("label,fields", CHECK_CASES, ids=[c[0] for c in CHECK_CASES])
def test_oauth_state_check_constraints_reject_unsafe_shapes(two_merchants, label, fields):
    a = two_merchants["A"]
    values = dict(
        merchant_id=a["owner"].merchant_id,
        user=a["owner"].user,
        state_hash="hash-" + label,
        nonce_hash="n",
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    values.update(fields)
    with tenant_context(a["owner"].merchant_id), tenant_atomic():
        with pytest.raises(IntegrityError), transaction.atomic():
            GoogleOAuthState.objects.create(**values)


def test_oauth_state_accepts_the_safe_shapes(two_merchants):
    a = two_merchants["A"]
    base = dict(merchant_id=a["owner"].merchant_id, user=a["owner"].user, nonce_hash="n",
                expires_at=timezone.now() + timedelta(minutes=5))
    with tenant_context(a["owner"].merchant_id), tenant_atomic():
        GoogleOAuthState.objects.create(state_hash="s1", status="PENDING", code_enc=encrypt("c"), **base)
        GoogleOAuthState.objects.create(state_hash="s2", status="FAILED", failure_code="EXCHANGE_FAILED", **base)
        GoogleOAuthState.objects.create(state_hash="s3", status="COMPLETED", google_connection=a["connection"], **base)
