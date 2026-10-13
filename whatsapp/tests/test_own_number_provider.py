"""Provider contract tests for the Embedded Signup additions (spec
08-whatsapp-embedded-signup P3): per-account token selection, register_number
and subscribe_app. Only the HTTP call is mocked (`meta_http`).

exchange_code and verify_number_access are NOT here: they are blocked on
evidence (WhatsApp-Meta.md E-2b/E-10 and E-4) and are not implemented.
"""
import logging

import pytest

from core.crypto import encrypt
from whatsapp.exceptions import WhatsAppNotConfigured
from whatsapp.models import WhatsAppAccount
from whatsapp.providers.base import (
    ProviderCredential,
    ProviderPermanentError,
    ProviderTransientError,
)
from whatsapp.providers.meta_cloud import MetaCloudProvider, _token_for
from whatsapp.tests.conftest import http_error, load_fixture
from whatsapp.tests.helpers import ACCESS_TOKEN, SHARED_PHONE_NUMBER_ID, SHARED_WABA_ID

pytestmark = pytest.mark.django_db

provider = MetaCloudProvider()
OWN_TOKEN = "EAAOWN-merchant-business-token"
PIN = "246810"
PHONE_ID = "200000000000001"
WABA_ID = "300000000000001"


def own_account(*, token_enc=None):
    return WhatsAppAccount(
        sender_type="OWN_NUMBER",
        provider="meta_cloud",
        phone_number_id=PHONE_ID,
        business_account_id=WABA_ID,
        status="ACTIVE",
        access_token_enc=token_enc,
    )


def shared_account():
    return WhatsAppAccount(
        sender_type="SHARED_POOL",
        provider="meta_cloud",
        phone_number_id=SHARED_PHONE_NUMBER_ID,
        business_account_id=SHARED_WABA_ID,
        status="ACTIVE",
    )


def auth_header(request):
    return request.get_header("Authorization")


# --- credential selection -------------------------------------------------------------


def test_shared_account_uses_the_platform_env_token_unchanged(meta_settings):
    assert _token_for(shared_account()) == ACCESS_TOKEN


def test_own_account_uses_its_decrypted_token(fernet_key):
    assert _token_for(own_account(token_enc=encrypt(OWN_TOKEN))) == OWN_TOKEN


def test_own_account_without_a_token_fails_closed(fernet_key):
    with pytest.raises(WhatsAppNotConfigured):
        _token_for(own_account(token_enc=None))


def test_own_account_with_an_undecryptable_token_fails_closed_without_leaking(fernet_key):
    with pytest.raises(WhatsAppNotConfigured) as caught:
        _token_for(own_account(token_enc="not-a-fernet-token"))
    assert "not-a-fernet-token" not in str(caught.value)


def test_own_account_with_no_fernet_key_fails_closed_never_plaintext(settings):
    settings.FERNET_KEY = ""
    with pytest.raises(WhatsAppNotConfigured):
        _token_for(own_account(token_enc="anything"))


def test_unset_shared_token_still_fails_closed(settings):
    settings.META_SHARED_POOL_ACCESS_TOKEN = ""
    with pytest.raises(WhatsAppNotConfigured):
        _token_for(shared_account())


def test_existing_provider_methods_send_the_accounts_own_token(meta_http, fernet_key):
    meta_http.on("/phone_numbers", load_fixture("quality_rating.json"))
    account = own_account(token_enc=encrypt(OWN_TOKEN))
    account.phone_number_id = load_fixture("quality_rating.json")["data"][0]["id"]
    provider.fetch_quality_rating(account)
    assert auth_header(meta_http.requests[-1]) == f"Bearer {OWN_TOKEN}"


def test_existing_provider_methods_still_send_the_shared_token(meta_http):
    meta_http.on("/phone_numbers", load_fixture("quality_rating.json"))
    provider.fetch_quality_rating(shared_account())
    assert auth_header(meta_http.requests[-1]) == f"Bearer {ACCESS_TOKEN}"


# --- register_number ----------------------------------------------------------------------


def test_register_number_posts_the_pin_with_the_given_credential(meta_http):
    meta_http.on(f"/{PHONE_ID}/register", load_fixture("success_true.json"))
    provider.register_number(ProviderCredential(OWN_TOKEN), PHONE_ID, PIN)

    request = meta_http.requests[-1]
    assert request.get_method() == "POST"
    assert request.full_url.endswith(f"/{PHONE_ID}/register")
    assert auth_header(request) == f"Bearer {OWN_TOKEN}"
    assert meta_http.body() == {"messaging_product": "whatsapp", "pin": PIN}
    assert meta_http.timeouts[-1] == 10  # the strict per-call timeout (ES-4)


def test_register_number_makes_exactly_one_call(meta_http):
    meta_http.on("/register", load_fixture("success_true.json"))
    provider.register_number(ProviderCredential(OWN_TOKEN), PHONE_ID, PIN)
    assert len(meta_http.requests) == 1


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (http_error(400), ProviderPermanentError),  # wrong PIN etc.
        (http_error(403), ProviderPermanentError),
        (http_error(429), ProviderTransientError),
        (http_error(500), ProviderTransientError),
        (TimeoutError(), ProviderTransientError),
    ],
)
def test_register_number_failures_are_classified(meta_http, failure, expected):
    meta_http.on("/register", failure)
    with pytest.raises(expected):
        provider.register_number(ProviderCredential(OWN_TOKEN), PHONE_ID, PIN)


def test_register_number_unreadable_success_is_transient(meta_http):
    meta_http.on("/register", {"success": False})
    with pytest.raises(ProviderTransientError):
        provider.register_number(ProviderCredential(OWN_TOKEN), PHONE_ID, PIN)


def test_register_number_never_leaks_the_pin_or_token(meta_http, caplog):
    caplog.set_level(logging.DEBUG)
    meta_http.on("/register", http_error(400))
    with pytest.raises(ProviderPermanentError) as caught:
        provider.register_number(ProviderCredential(OWN_TOKEN), PHONE_ID, PIN)

    surfaced = str(caught.value) + caplog.text + repr(ProviderCredential(OWN_TOKEN))
    assert PIN not in surfaced
    assert OWN_TOKEN not in surfaced
    assert "SECRET-BODY-MARKER" not in surfaced


def test_register_number_without_a_credential_token_fails_closed(meta_http):
    with pytest.raises(WhatsAppNotConfigured):
        provider.register_number(ProviderCredential(""), PHONE_ID, PIN)
    assert meta_http.requests == []


# --- subscribe_app ---------------------------------------------------------------------------


def test_subscribe_app_posts_to_the_wabas_subscribed_apps_with_its_token(meta_http, fernet_key):
    meta_http.on(f"/{WABA_ID}/subscribed_apps", load_fixture("success_true.json"))
    provider.subscribe_app(own_account(token_enc=encrypt(OWN_TOKEN)))

    request = meta_http.requests[-1]
    assert request.get_method() == "POST"
    assert request.full_url.endswith(f"/{WABA_ID}/subscribed_apps")
    assert auth_header(request) == f"Bearer {OWN_TOKEN}"
    assert len(meta_http.requests) == 1


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (http_error(400), ProviderPermanentError),
        (http_error(429), ProviderTransientError),
        (http_error(503), ProviderTransientError),
        (TimeoutError(), ProviderTransientError),
    ],
)
def test_subscribe_app_failures_are_classified(meta_http, fernet_key, failure, expected):
    meta_http.on("/subscribed_apps", failure)
    with pytest.raises(expected):
        provider.subscribe_app(own_account(token_enc=encrypt(OWN_TOKEN)))


def test_subscribe_app_without_a_stored_token_makes_no_call(meta_http, fernet_key):
    with pytest.raises(WhatsAppNotConfigured):
        provider.subscribe_app(own_account(token_enc=None))
    assert meta_http.requests == []


def test_subscribe_app_never_leaks_the_token(meta_http, fernet_key, caplog):
    caplog.set_level(logging.DEBUG)
    meta_http.on("/subscribed_apps", http_error(400))
    with pytest.raises(ProviderPermanentError) as caught:
        provider.subscribe_app(own_account(token_enc=encrypt(OWN_TOKEN)))
    assert OWN_TOKEN not in str(caught.value) + caplog.text
