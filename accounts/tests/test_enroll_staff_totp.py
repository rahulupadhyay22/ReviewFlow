"""enroll_staff_totp command (spec 16 D2, LC-1; plan step 3c)."""
import logging
import re
from io import StringIO

import pyotp
import pytest
from cryptography.fernet import Fernet
from django.core.management import CommandError, call_command
from django.db import DatabaseError

from accounts import services
from accounts.models import User
from whatsapp.tests.helpers import clear_platform_write_setting, platform_audit_rows

pytestmark = pytest.mark.django_db

PASSWORD = "Tr1cky-Horse-Battery-Staple!"


@pytest.fixture(autouse=True)
def fernet_key(settings):
    settings.FERNET_KEY = Fernet.generate_key().decode()


def _run(email, monkeypatch, *, code_for=lambda secret: pyotp.TOTP(secret).now()):
    """Runs the command, answering the code prompt from the printed secret."""
    out = StringIO()

    def fake_input(prompt):
        secret = re.search(r"Secret: (\S+)", out.getvalue()).group(1)
        return code_for(secret)

    monkeypatch.setattr("builtins.input", fake_input)
    call_command("enroll_staff_totp", email, stdout=out)
    return out.getvalue()


def test_it_enrolls_a_staff_user_prints_ten_codes_and_writes_one_audit_row(monkeypatch, caplog):
    staff = User.objects.create_user("Staff@Example.com", PASSWORD, is_staff=True)
    with caplog.at_level(logging.DEBUG):
        output = _run(" STAFF@example.com ", monkeypatch)

    staff.refresh_from_db()
    assert staff.is_totp_enabled
    printed_codes = re.findall(r"^  ([0-9a-f]{5}(?:-[0-9a-f]{5}){3})$", output, flags=re.M)
    assert len(printed_codes) == 10
    secret = re.search(r"Secret: (\S+)", output).group(1)

    clear_platform_write_setting()
    rows = platform_audit_rows()
    assert [(r["action"], r["actor_user_id"], r["metadata"]) for r in rows] == [
        ("user.totp_enabled", staff.id, None)
    ]
    # No secret or code reaches the logs or the audit row.
    for value in (secret, *printed_codes):
        assert value not in caplog.text
        assert value not in str(rows)


@pytest.mark.parametrize(
    "extra, message",
    [({"is_staff": False}, "not an active staff user"), ({"is_staff": True, "is_active": False}, "not an active staff user")],
)
def test_it_refuses_non_staff_and_inactive_users(monkeypatch, extra, message):
    User.objects.create_user("user@example.com", PASSWORD, **extra)
    with pytest.raises(CommandError, match=message):
        _run("user@example.com", monkeypatch)


def test_it_refuses_an_already_enrolled_user(monkeypatch):
    User.objects.create_user("staff@example.com", PASSWORD, is_staff=True)
    _run("staff@example.com", monkeypatch, code_for=lambda s: pyotp.TOTP(s).now())
    with pytest.raises(CommandError, match="already enabled"):
        _run("staff@example.com", monkeypatch)


def test_an_unknown_email_and_a_wrong_code_fail_without_enabling(monkeypatch):
    with pytest.raises(CommandError, match="no user"):
        _run("nobody@example.com", monkeypatch)
    staff = User.objects.create_user("staff@example.com", PASSWORD, is_staff=True)
    with pytest.raises(CommandError, match="did not verify"):
        _run("staff@example.com", monkeypatch, code_for=lambda s: "000000")
    staff.refresh_from_db()
    assert not staff.is_totp_enabled


def test_a_failed_audit_insert_leaves_2fa_disabled(monkeypatch):
    staff = User.objects.create_user("staff@example.com", PASSWORD, is_staff=True)

    def boom(*args, **kwargs):
        raise DatabaseError("audit insert failed")

    monkeypatch.setattr(services, "record_platform", boom)
    with pytest.raises(DatabaseError):
        _run("staff@example.com", monkeypatch)
    staff.refresh_from_db()
    assert not staff.is_totp_enabled
