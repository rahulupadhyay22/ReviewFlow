"""
Scaffold tests (Phase 00). No apps/models/business logic exist yet;
these tests only verify the project skeleton per
.claude/specs/00-project-scaffold.md.
"""
import io
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connection

from config import celery_app

BASE_DIR = Path(settings.BASE_DIR)

EXPECTED_REQUIREMENTS = {
    "Django==5.2.17",
    "celery[redis]==5.6.3",
    "psycopg[binary]==3.3.6",
    "django-environ==0.14.0",
    "pytest==9.1.1",
    "pytest-django==4.14.0",
}

DOC_FILES_REQUIRING_CONFIG_CELERY = [
    BASE_DIR / "CLAUDE.md",
    BASE_DIR / ".claude" / "commands" / "ship-feature.md",
    BASE_DIR / "docs" / "10-development" / "Development-Setup.md",
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_database_vendor_is_postgresql():
    assert connection.vendor == "postgresql"
    assert connection.settings_dict["ENGINE"] == "django.db.backends.postgresql"


# ---------------------------------------------------------------------------
# Celery configuration
# ---------------------------------------------------------------------------


def test_celery_queue_names_are_the_four_locked_queues():
    queue_names = {q.name for q in celery_app.conf.task_queues}
    assert queue_names == {"events", "whatsapp", "google_sync", "default"}


def test_celery_default_queue_is_default():
    assert celery_app.conf.task_default_queue == "default"


def test_celery_broker_and_result_backend_come_from_redis_url():
    assert celery_app.conf.broker_url == settings.REDIS_URL
    assert celery_app.conf.result_backend == settings.REDIS_URL


def test_celery_runs_tasks_inline_under_pytest():
    @celery_app.task
    def _double(x):
        return x * 2

    result = _double.delay(2)
    assert result.get(timeout=5) == 4


def test_celery_eager_mode_propagates_task_exceptions():
    @celery_app.task
    def _boom():
        raise ValueError("scaffold-test-boom")

    with pytest.raises(ValueError, match="scaffold-test-boom"):
        _boom.delay()


def test_beat_schedule_is_empty():
    assert settings.CELERY_BEAT_SCHEDULE == {}


def test_timezone_settings():
    assert settings.USE_TZ is True
    assert settings.TIME_ZONE == "UTC"
    assert settings.CELERY_TIMEZONE == "UTC"


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def test_requirements_pins_exactly_the_six_direct_dependencies():
    lines = (BASE_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
    parsed = {line.strip() for line in lines if line.strip() and not line.strip().startswith("#")}
    assert parsed == EXPECTED_REQUIREMENTS


# ---------------------------------------------------------------------------
# Secrets hygiene
# ---------------------------------------------------------------------------


def test_env_file_is_not_tracked_by_git():
    result = subprocess.run(
        ["git", "ls-files", ".env"],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""


def test_env_example_contains_no_real_secrets():
    text = (BASE_DIR / ".env.example").read_text(encoding="utf-8")
    env_values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env_values[key.strip()] = value.strip()

    assert env_values.get("DJANGO_SECRET_KEY") != settings.SECRET_KEY
    for placeholder_key in ("FERNET_KEY", "META_APP_ID", "META_APP_SECRET",
                             "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"):
        assert env_values.get(placeholder_key, "") == ""


# ---------------------------------------------------------------------------
# Docs / command consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc_path", DOC_FILES_REQUIRING_CONFIG_CELERY)
def test_docs_use_celery_dash_a_config_not_reviewflow(doc_path):
    text = doc_path.read_text(encoding="utf-8")
    assert "celery -A config" in text
    assert "celery -A reviewflow" not in text


# ---------------------------------------------------------------------------
# Django system checks / migrations
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_manage_py_check_reports_no_issues():
    # call_command("check") raises CommandError/SystemCheckError if any
    # issue is found; not raising is the pass condition here, since
    # Django's check framework has no "all clear" return value to assert
    # on for the empty-issues case.
    call_command("check")


@pytest.mark.django_db
def test_no_missing_migrations():
    out = io.StringIO()
    with redirect_stdout(out):
        call_command("makemigrations", "--check", "--dry-run", stdout=out)
    assert "No changes detected" in out.getvalue()


# ---------------------------------------------------------------------------
# Settings loading (subprocess: settings.py reads env at import time)
# ---------------------------------------------------------------------------


def _run_settings_subprocess(env_overrides, env_removals=()):
    """
    Import config.settings in a fresh subprocess with a controlled
    environment. Neutralises environ.Env.read_env so the developer's
    real .env (which would otherwise backfill missing/removed vars)
    never gets consulted.
    """
    child_env = os.environ.copy()
    child_env.update(env_overrides)
    for key in env_removals:
        child_env.pop(key, None)

    script = (
        "import environ; "
        "environ.Env.read_env = staticmethod(lambda *a, **k: None); "
        "import config.settings"
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=BASE_DIR,
        env=child_env,
        capture_output=True,
        text=True,
    )


def _base_env():
    return {
        "DJANGO_SECRET_KEY": "scaffold-test-secret-key",
        "DATABASE_URL": "postgres://reviewflow:local_dev_only@localhost:5432/reviewflow",
        "REDIS_URL": "redis://localhost:6379/0",
    }


def test_settings_fail_without_secret_key():
    result = _run_settings_subprocess(_base_env(), env_removals=["DJANGO_SECRET_KEY"])
    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY" in result.stderr


def test_debug_defaults_to_false_when_django_debug_unset():
    env = _base_env()
    script = (
        "import environ; "
        "environ.Env.read_env = staticmethod(lambda *a, **k: None); "
        "import config.settings as s; "
        "assert s.DEBUG is False, s.DEBUG"
    )
    child_env = os.environ.copy()
    child_env.update(env)
    child_env.pop("DJANGO_DEBUG", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BASE_DIR,
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_non_postgres_database_url_is_rejected():
    env = _base_env()
    env["DATABASE_URL"] = "sqlite:///db.sqlite3"
    result = _run_settings_subprocess(env)
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "PostgreSQL" in result.stderr


# ---------------------------------------------------------------------------
# Installed interpreter / admin URL (Definition of done, appended)
# ---------------------------------------------------------------------------


def test_installed_django_version_matches_the_pin():
    import django

    assert django.get_version() == "5.2.17"


@pytest.mark.django_db
def test_admin_url_serves_login_page_for_anonymous_user(client):
    response = client.get("/admin/", follow=True)
    assert response.status_code == 200
    assert response.redirect_chain[-1][0].startswith("/admin/login/")
    assert b'name="username"' in response.content
    assert b"csrfmiddlewaretoken" in response.content
