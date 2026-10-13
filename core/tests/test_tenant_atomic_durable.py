"""tenant_atomic(durable=True) (spec 16; plan step 3a). transaction=True: a
durable block can only be observed outside a test-case transaction."""
import uuid

import pytest
from django.db import connection, transaction

from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db(transaction=True)


def _setting():
    with connection.cursor() as cur:
        cur.execute("SELECT current_setting('app.current_merchant_id', true)")
        return cur.fetchone()[0]


def test_durable_cannot_nest_inside_another_transaction():
    with tenant_context(uuid.uuid4()):
        with transaction.atomic():
            with pytest.raises(RuntimeError):
                with tenant_atomic(durable=True):
                    pass


def test_durable_sets_and_then_clears_the_setting():
    merchant_id = uuid.uuid4()
    with tenant_context(merchant_id):
        with tenant_atomic(durable=True):
            assert _setting() == str(merchant_id)
    assert _setting() in ("", None)


def test_the_default_still_nests_as_a_savepoint():
    with tenant_context(uuid.uuid4()):
        with transaction.atomic():
            with tenant_atomic():
                assert connection.in_atomic_block
