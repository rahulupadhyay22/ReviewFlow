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


@pytest.fixture(autouse=True, scope="session")
def celery_eager_mode():
    """Run Celery tasks inline under pytest (Testing-Strategy.md §Tooling)."""
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
