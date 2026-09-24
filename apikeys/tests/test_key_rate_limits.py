"""Per-key and per-IP throttles (spec §"Auth / throttle plumbing", Decision 8,
Definition of done §"Rate limiting"). Uses the feature-local throttle_rates
fixture (apikeys/tests/conftest.py) to override rates deterministically, and
the autouse local_cache fixture so counters never leak through Redis. Each
low-rate test first asserts a fresh throttle instance resolves the override
(num_requests), so it fails loudly if the override mechanism stops applying."""
import pytest
from rest_framework.settings import api_settings as drf_api_settings
from rest_framework.test import APIClient

from accounts.models import TeamMember
from apikeys import services
from apikeys.throttling import ApiKeyIpRateThrottle, ApiKeyRateThrottle

pytestmark = pytest.mark.django_db

MERCHANT = "/api/v1/merchant"


def _bearer(raw):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _owner(merchant, add_member, email):
    return add_member(merchant, TeamMember.Role.OWNER, email)


def test_default_rates_are_600_and_1200_per_minute():
    assert drf_api_settings.DEFAULT_THROTTLE_RATES["api_key"] == "600/min"
    assert drf_api_settings.DEFAULT_THROTTLE_RATES["api_key_ip"] == "1200/min"
    assert ApiKeyRateThrottle().num_requests == 600
    assert ApiKeyIpRateThrottle().num_requests == 1200


def test_per_key_throttle_returns_429_after_the_rate_and_has_retry_after(
    merchant_a, add_member, make_key, throttle_rates
):
    throttle_rates(api_key="3/min")
    assert ApiKeyRateThrottle().num_requests == 3
    owner = _owner(merchant_a, add_member, "throttle-perkey@example.com")
    key, raw = make_key(owner)
    client = APIClient()
    statuses = [client.get(MERCHANT, **_bearer(raw)).status_code for _ in range(4)]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
    resp = client.get(MERCHANT, **_bearer(raw))
    assert resp.status_code == 429
    assert "Retry-After" in resp
    assert resp.json()["error"]["code"] == "throttled"


def test_per_key_throttle_counters_are_independent_per_key(merchant_a, add_member, make_key, throttle_rates):
    throttle_rates(api_key="3/min")
    assert ApiKeyRateThrottle().num_requests == 3
    owner = _owner(merchant_a, add_member, "throttle-independent@example.com")
    key1, raw1 = make_key(owner)
    key2, raw2 = make_key(owner)
    client = APIClient()
    for _ in range(3):
        assert client.get(MERCHANT, **_bearer(raw1)).status_code == 200
    assert client.get(MERCHANT, **_bearer(raw1)).status_code == 429
    # A second key from the same merchant has its own counter.
    assert client.get(MERCHANT, **_bearer(raw2)).status_code == 200


def test_per_ip_throttle_counts_invalid_key_attempts_before_the_lookup(throttle_rates, monkeypatch):
    throttle_rates(api_key_ip="3/min")
    assert ApiKeyIpRateThrottle().num_requests == 3

    lookups = []
    original = services.authenticate_api_key

    def _spy(raw):
        lookups.append(raw)
        return original(raw)

    monkeypatch.setattr(services, "authenticate_api_key", _spy)

    client = APIClient()
    statuses = [
        client.get(MERCHANT, **_bearer("rf_live_" + "n" * 32)).status_code for _ in range(4)
    ]
    assert statuses[:3] == [401, 401, 401]
    assert statuses[3] == 429
    # The throttled 4th request never reached the key lookup.
    assert len(lookups) == 3

    resp = client.get(MERCHANT, **_bearer("rf_live_" + "n" * 32))
    assert resp.status_code == 429
    assert "Retry-After" in resp
    assert resp.json()["error"]["code"] == "throttled"


def test_rotating_x_forwarded_for_does_not_create_fresh_per_ip_buckets(throttle_rates):
    """Security-Controls.md §Rate Limiting: with the configured NUM_PROXIES
    (0 by default, matching config/settings.py), DRF's get_ident() ignores a
    client-supplied X-Forwarded-For and keys on REMOTE_ADDR."""
    throttle_rates(api_key_ip="3/min")
    assert ApiKeyIpRateThrottle().num_requests == 3
    client = APIClient()
    statuses = [
        client.get(
            MERCHANT,
            HTTP_X_FORWARDED_FOR=f"10.0.0.{i}",
            **_bearer("rf_live_" + "m" * 32),
        ).status_code
        for i in range(4)
    ]
    assert statuses[:3] == [401, 401, 401]
    assert statuses[3] == 429


def test_session_requests_are_not_api_key_throttled(merchant_a, add_member, session_client, throttle_rates):
    throttle_rates(api_key="1/min", api_key_ip="1/min")
    assert ApiKeyRateThrottle().num_requests == 1
    assert ApiKeyIpRateThrottle().num_requests == 1
    owner = _owner(merchant_a, add_member, "throttle-session@example.com")
    client = session_client(owner.user.email)
    assert [client.get(MERCHANT).status_code for _ in range(3)] == [200, 200, 200]
