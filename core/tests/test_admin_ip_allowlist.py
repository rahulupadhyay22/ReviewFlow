"""Admin IP allowlist (spec 16 D5; plan step 1): fail closed, client IP
under the DJANGO_NUM_PROXIES rule, never the raw forwarding header."""
import pytest
from rest_framework.settings import api_settings

pytestmark = pytest.mark.django_db

LOGIN = "/admin/login/"


@pytest.fixture
def num_proxies(settings):
    def _set(n):
        settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": n}
        assert api_settings.NUM_PROXIES == n

    return _set


def test_an_allowlisted_ip_reaches_the_admin_login(client, settings):
    settings.ADMIN_ALLOWED_IPS = ["127.0.0.1/32"]
    assert client.get(LOGIN).status_code == 200


@pytest.mark.parametrize("path", ["/admin/", LOGIN, "/admin/accounts/merchant/"])
def test_a_non_allowlisted_ip_gets_404_on_every_admin_path(client, settings, path):
    settings.ADMIN_ALLOWED_IPS = ["10.0.0.0/8"]
    assert client.get(path).status_code == 404
    assert client.post(path, {}).status_code == 404


@pytest.mark.parametrize("allowed", [[], ["10.0.0.0/8"]], ids=["empty", "ip-not-listed"])
def test_admin_without_the_trailing_slash_is_404_not_a_redirect(client, settings, allowed):
    """Regression (code review 2026-10-13): /admin used to fall through to
    CommonMiddleware's 301 to /admin/, revealing that the admin exists."""
    settings.ADMIN_ALLOWED_IPS = allowed
    resp = client.get("/admin")
    assert resp.status_code == 404
    assert resp.get("Location") is None
    assert client.get("/admin/").status_code == 404


def test_admin_without_the_trailing_slash_still_redirects_for_an_allowlisted_ip(client, settings):
    settings.ADMIN_ALLOWED_IPS = ["127.0.0.1/32"]
    resp = client.get("/admin")
    assert resp.status_code == 301 and resp["Location"] == "/admin/"


@pytest.mark.parametrize("allowed", [[], ["127.0.0.1/32", "nonsense"], ["  "]])
def test_an_empty_or_invalid_allowlist_denies_everyone(client, settings, allowed):
    settings.ADMIN_ALLOWED_IPS = allowed
    assert client.get(LOGIN).status_code == 404


def test_with_zero_proxies_a_spoofed_forwarded_for_is_ignored(client, settings, num_proxies):
    num_proxies(0)
    settings.ADMIN_ALLOWED_IPS = ["203.0.113.7/32"]
    # REMOTE_ADDR is 127.0.0.1; the header names an allowlisted address.
    assert client.get(LOGIN, HTTP_X_FORWARDED_FOR="203.0.113.7").status_code == 404


def test_with_one_proxy_the_right_most_forwarded_for_entry_is_checked(client, settings, num_proxies):
    num_proxies(1)
    settings.ADMIN_ALLOWED_IPS = ["203.0.113.7/32"]
    assert client.get(LOGIN, HTTP_X_FORWARDED_FOR="198.51.100.1, 203.0.113.7").status_code == 200
    # A client-chosen left entry cannot win.
    assert client.get(LOGIN, HTTP_X_FORWARDED_FOR="203.0.113.7, 198.51.100.1").status_code == 404


def test_an_unparsable_client_ip_is_denied(client, settings, num_proxies):
    num_proxies(1)
    settings.ADMIN_ALLOWED_IPS = ["0.0.0.0/0"]
    assert client.get(LOGIN, HTTP_X_FORWARDED_FOR="not-an-ip").status_code == 404


def test_non_admin_paths_are_unaffected(client, settings):
    settings.ADMIN_ALLOWED_IPS = []
    assert client.get("/api/v1/merchant").status_code != 404
