"""EmbeddedSignupState (spec 08-whatsapp-embedded-signup ES-18): hash-only,
merchant- and user-bound, expiring, atomically single-use. Covers the storage
primitives issue_signup_state / consume_signup_state, the table's RLS, and the
race. The `start` endpoint that issues a state is tested in
test_embedded_signup_start.py. The `complete` endpoint that consumes one is
P6b (blocked on evidence), does not exist yet and is not tested here.

Two modules of tests: the default (test-wrapped) ones, and the
transaction=True ones, which need real commits and a second connection."""
import hashlib
import logging
import threading
from datetime import timedelta

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context
from whatsapp import services
from whatsapp.models import EmbeddedSignupState

TABLE = "whatsapp_embeddedsignupstate"
SHA256_HEX_LEN = 64


def sha(state):
    return hashlib.sha256(state.encode()).hexdigest()


def table_rows(merchant_id):
    with tenant_context(merchant_id), tenant_atomic():
        return list(EmbeddedSignupState.objects.all())


def raw(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount


def raw_rows(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def issue(owner, user=None):
    with tenant_context(owner.merchant_id):
        return services.issue_signup_state(actor=user or owner.user)


def consume(owner, state, user=None):
    with tenant_context(owner.merchant_id):
        return services.consume_signup_state(actor=user or owner.user, state=state)


# =============================================================================================
# Default tests: behaviour of the primitives and the table
# =============================================================================================


@pytest.mark.django_db
class TestIssue:
    def test_only_the_sha256_hash_is_stored_never_the_plaintext(self, make_merchant):
        owner = make_merchant("A")

        state = issue(owner)

        (row,) = table_rows(owner.merchant_id)
        assert row.state_hash == sha(state)
        assert len(row.state_hash) == SHA256_HEX_LEN
        assert state not in row.state_hash
        # No column of the row holds the plaintext (scan the raw row).
        with tenant_context(owner.merchant_id), tenant_atomic():
            (raw_row,) = raw_rows(f"SELECT t::text FROM {TABLE} t")
        assert state not in raw_row[0]

    def test_the_state_is_unguessable_and_distinct_per_issue(self, make_merchant):
        owner = make_merchant("A")

        first, second = issue(owner), issue(owner)

        assert first != second
        assert len(first) >= 43  # token_urlsafe(32)

    def test_the_row_is_bound_to_the_merchant_and_user_with_the_configured_expiry(
        self, make_merchant, settings
    ):
        settings.WHATSAPP_SIGNUP_STATE_MAX_AGE_SECONDS = 600
        owner = make_merchant("A")
        before = timezone.now()

        issue(owner)

        (row,) = table_rows(owner.merchant_id)
        assert row.merchant_id == owner.merchant_id
        assert row.user_id == owner.user_id
        assert before + timedelta(seconds=599) <= row.expires_at <= timezone.now() + timedelta(seconds=601)

    def test_it_requires_a_tenant_context(self, make_merchant):
        owner = make_merchant("A")
        with pytest.raises(TenantContextError):
            services.issue_signup_state(actor=owner.user)

    def test_issuing_purges_only_the_same_merchants_expired_rows(self, make_merchant):
        a, b = make_merchant("A"), make_merchant("B")
        issue(a)
        issue(b)
        past = timezone.now() - timedelta(seconds=1)
        for owner in (a, b):
            with tenant_context(owner.merchant_id), tenant_atomic():
                EmbeddedSignupState.objects.update(expires_at=past)

        issue(a)  # purges A's expired row, adds a fresh one

        assert len(table_rows(a.merchant_id)) == 1
        assert table_rows(a.merchant_id)[0].expires_at > timezone.now()
        assert len(table_rows(b.merchant_id)) == 1  # B's expired row untouched

    def test_nothing_is_logged_containing_the_state(self, make_merchant, caplog):
        caplog.set_level(logging.DEBUG)
        owner = make_merchant("A")

        state = issue(owner)
        consume(owner, state)

        assert state not in caplog.text


@pytest.mark.django_db
class TestConsume:
    def test_a_valid_state_is_consumed_exactly_once(self, make_merchant):
        owner = make_merchant("A")
        state = issue(owner)

        assert consume(owner, state) is True
        assert table_rows(owner.merchant_id) == []

        assert consume(owner, state) is False  # replay

    def test_consumption_is_one_conditional_delete(self, make_merchant):
        owner = make_merchant("A")
        state = issue(owner)

        with tenant_context(owner.merchant_id):
            with CaptureQueriesContext(connection) as queries:
                assert services.consume_signup_state(actor=owner.user, state=state) is True

        statements = [q["sql"].lstrip().split()[0].upper() for q in queries.captured_queries]
        # SET LOCAL (tenant_atomic) and the single DELETE, with no SELECT first.
        assert statements.count("DELETE") == 1
        assert "SELECT" not in statements

    def test_another_user_of_the_same_merchant_cannot_consume_it_and_does_not_burn_it(
        self, make_merchant, add_member
    ):
        owner = make_merchant("A")
        other = add_member(owner.merchant, "ADMIN", "other-admin@example.com")
        state = issue(owner)

        assert consume(owner, state, user=other.user) is False

        assert len(table_rows(owner.merchant_id)) == 1  # still there for the real user
        assert consume(owner, state) is True

    def test_another_merchant_cannot_consume_it(self, make_merchant):
        a, b = make_merchant("A"), make_merchant("B")
        state = issue(a)

        assert consume(b, state) is False
        assert consume(b, state, user=a.user) is False  # even naming A's user

        assert len(table_rows(a.merchant_id)) == 1
        assert consume(a, state) is True

    def test_an_expired_state_is_refused(self, make_merchant):
        owner = make_merchant("A")
        state = issue(owner)
        with tenant_context(owner.merchant_id), tenant_atomic():
            EmbeddedSignupState.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        assert consume(owner, state) is False

    def test_a_state_expiring_in_the_future_is_accepted(self, make_merchant):
        owner = make_merchant("A")
        state = issue(owner)
        with tenant_context(owner.merchant_id), tenant_atomic():
            EmbeddedSignupState.objects.update(expires_at=timezone.now() + timedelta(seconds=30))

        assert consume(owner, state) is True

    @pytest.mark.parametrize("garbage", ["", "x", "not-a-real-state", "A" * 500])
    def test_an_unknown_state_is_refused(self, make_merchant, garbage):
        owner = make_merchant("A")
        issue(owner)

        assert consume(owner, garbage) is False
        assert len(table_rows(owner.merchant_id)) == 1

    def test_it_requires_a_tenant_context(self, make_merchant):
        owner = make_merchant("A")
        with pytest.raises(TenantContextError):
            services.consume_signup_state(actor=owner.user, state="x")


# --- the table: schema and RLS ----------------------------------------------------------------


@pytest.mark.django_db
class TestTable:
    def test_rls_is_enabled_forced_with_exactly_tenant_isolation(self):
        (flags,) = raw_rows(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s", [TABLE]
        )
        assert flags == (True, True)
        policies = raw_rows("SELECT policyname, cmd FROM pg_policies WHERE tablename = %s", [TABLE])
        assert policies == [("tenant_isolation", "ALL")]

    def test_merchant_b_cannot_see_update_or_delete_merchant_as_state(self, make_merchant):
        a, b = make_merchant("A"), make_merchant("B")
        issue(a)

        with tenant_context(b.merchant_id), tenant_atomic():
            assert raw_rows(f"SELECT count(*) FROM {TABLE}")[0][0] == 0
            assert raw(f"UPDATE {TABLE} SET expires_at = now()") == 0
            assert raw(f"DELETE FROM {TABLE}") == 0

        assert len(table_rows(a.merchant_id)) == 1

    def test_merchant_b_cannot_insert_a_row_for_merchant_a(self, make_merchant):
        a, b = make_merchant("A"), make_merchant("B")
        with tenant_context(b.merchant_id), tenant_atomic():
            with pytest.raises(DatabaseError), transaction.atomic():
                EmbeddedSignupState.objects.create(
                    merchant_id=a.merchant_id,
                    user=a.user,
                    state_hash=sha("forged"),
                    expires_at=timezone.now() + timedelta(seconds=60),
                )

    def test_state_hash_is_unique(self, make_merchant):
        owner = make_merchant("A")
        with tenant_context(owner.merchant_id), tenant_atomic():
            EmbeddedSignupState.objects.create(
                merchant_id=owner.merchant_id,
                user=owner.user,
                state_hash=sha("dup"),
                expires_at=timezone.now() + timedelta(seconds=60),
            )
            with pytest.raises(IntegrityError), transaction.atomic():
                EmbeddedSignupState.objects.create(
                    merchant_id=owner.merchant_id,
                    user=owner.user,
                    state_hash=sha("dup"),
                    expires_at=timezone.now() + timedelta(seconds=60),
                )

    def test_the_manager_needs_a_tenant_context(self):
        with pytest.raises(TenantContextError):
            EmbeddedSignupState.objects.count()


# =============================================================================================
# transaction=True: real commits, a second connection, and the race
# =============================================================================================


def _run_threads(count, target):
    errors = []
    results = [None] * count
    barrier = threading.Barrier(count)

    def wrapper(index):
        try:
            barrier.wait(timeout=10)
            results[index] = target(index)
        except BaseException as exc:  # surfaced after join
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=wrapper, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    return results


@pytest.mark.django_db(transaction=True)
class TestConcurrencyAndCommit:
    def test_concurrent_consumption_of_one_state_succeeds_exactly_once(self, make_merchant):
        owner = make_merchant("A")
        merchant_id, user = owner.merchant_id, owner.user

        for _ in range(8):  # repeat: the race is timing-dependent
            state = issue(owner)

            def attempt(_index, state=state):
                with tenant_context(merchant_id):
                    return services.consume_signup_state(actor=user, state=state)

            results = _run_threads(6, attempt)

            assert results.count(True) == 1, results
            assert results.count(False) == 5
            assert table_rows(merchant_id) == []

    def test_concurrent_states_do_not_interfere(self, make_merchant):
        owner = make_merchant("A")
        merchant_id, user = owner.merchant_id, owner.user
        states = [issue(owner) for _ in range(5)]

        def attempt(index):
            with tenant_context(merchant_id):
                return services.consume_signup_state(actor=user, state=states[index])

        assert _run_threads(5, attempt) == [True] * 5

    def test_consumption_commits_immediately_and_leaves_no_open_transaction(self, make_merchant):
        owner = make_merchant("A")
        state = issue(owner)
        assert connection.in_atomic_block is False

        assert consume(owner, state) is True

        # No transaction is left open, so a Meta call made next runs outside one,
        assert connection.in_atomic_block is False
        # and the deletion is already committed: a second connection sees it.
        seen = _run_threads(
            1, lambda _i: _count_rows_from_another_connection(owner.merchant_id)
        )
        assert seen == [0]

    def test_an_issued_state_is_visible_to_another_connection_once_issue_returns(self, make_merchant):
        owner = make_merchant("A")
        issue(owner)

        seen = _run_threads(
            1, lambda _i: _count_rows_from_another_connection(owner.merchant_id)
        )
        assert seen == [1]


def _count_rows_from_another_connection(merchant_id):
    with tenant_context(merchant_id), tenant_atomic():
        return EmbeddedSignupState.objects.count()
