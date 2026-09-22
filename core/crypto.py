"""
Fernet encryption for secrets at rest (Security-Architecture.md
§Encryption & Secrets): the TOTP secret here, OAuth tokens in Phase 09.
The key is read from settings.FERNET_KEY (env only, never hardcoded).
"""
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _fernet() -> Fernet:
    key = settings.FERNET_KEY
    if not key:
        raise ImproperlyConfigured("FERNET_KEY is not set.")
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured("FERNET_KEY is not a valid Fernet key.") from exc


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Raises cryptography.fernet.InvalidToken for a tampered/wrong-key token."""
    return _fernet().decrypt(token.encode()).decode()
