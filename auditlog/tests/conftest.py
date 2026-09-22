"""Feature-local fixtures for auditlog tests. No external providers are used."""
import pytest

from accounts import services

PASSWORD = "Tr1cky-Horse-Battery-Staple!"


@pytest.fixture
def make_merchant():
    counter = iter(range(1, 1000))

    def _make():
        n = next(counter)
        return services.create_merchant_with_owner(
            name=f"Audit Merchant {n}",
            timezone="Asia/Kolkata",
            owner_email=f"audit-owner{n}@example.com",
            owner_password=PASSWORD,
        )

    return _make


@pytest.fixture
def member_a(make_merchant):
    return make_merchant()


@pytest.fixture
def member_b(make_merchant):
    return make_merchant()
