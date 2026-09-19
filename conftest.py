import pytest

from config import celery_app


@pytest.fixture(autouse=True, scope="session")
def celery_eager_mode():
    """Run Celery tasks inline under pytest (Testing-Strategy.md §Tooling)."""
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
