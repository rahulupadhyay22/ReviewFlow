"""Unit tests for the shared Fernet helper (core/crypto.py). No database."""
import pytest
from cryptography.fernet import Fernet, InvalidToken
from django.core.exceptions import ImproperlyConfigured

from core import crypto


@pytest.fixture(autouse=True)
def fernet_key(settings):
    settings.FERNET_KEY = Fernet.generate_key().decode()


def test_encrypt_decrypt_round_trip():
    token = crypto.encrypt("s3kr1t-base32-secret")
    assert token != "s3kr1t-base32-secret"
    assert crypto.decrypt(token) == "s3kr1t-base32-secret"


def test_encrypt_with_empty_fernet_key_raises_improperly_configured(settings):
    settings.FERNET_KEY = ""
    with pytest.raises(ImproperlyConfigured):
        crypto.encrypt("x")


def test_decrypt_with_invalid_fernet_key_raises_improperly_configured(settings):
    settings.FERNET_KEY = "not-a-valid-key"
    with pytest.raises(ImproperlyConfigured):
        crypto.decrypt("whatever")


def test_decrypt_tampered_token_raises_invalid_token():
    token = crypto.encrypt("s3kr1t")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(InvalidToken):
        crypto.decrypt(tampered)
