import pytest

from config import celery_app


@pytest.fixture(autouse=True)
def fast_password_hasher(settings):
    """Test-only: Django's default PBKDF2 hasher (1,000,000 rounds) costs
    about 0.5 s per hash or check, and almost every test creates a user or logs
    one in, so hashing dominated the suite's run time. MD5 is used here only
    because it is fast; production settings are untouched. No test depends on
    the algorithm (they call check_password)."""
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def admin_session_client(settings):
    """Factory: a Django test client for an ALREADY-authenticated admin
    session (spec 16). It sets the same session flag a real admin login sets
    after the second factor verifies; ReviewFlowAdminSite.has_permission is
    not bypassed and still checks it, along with is_active/is_staff. The
    allowlist admits the test client's REMOTE_ADDR (127.0.0.1). The real
    login flow is tested in accounts/tests/test_staff_admin_login.py."""
    from django.test import Client

    from config.admin import ADMIN_2FA_SESSION_KEY

    settings.ADMIN_ALLOWED_IPS = ["127.0.0.1/32"]

    def _make(user):
        client = Client()
        client.force_login(user)
        session = client.session
        session[ADMIN_2FA_SESSION_KEY] = str(user.pk)
        session.save()
        return client

    return _make


@pytest.fixture(autouse=True, scope="session")
def celery_eager_mode():
    """Run Celery tasks inline under pytest (Testing-Strategy.md §Tooling)."""
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
