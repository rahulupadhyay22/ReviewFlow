"""Staff (Django Admin) 2FA services (spec 16 D2, LC-1; plan step 3b).

begin/confirm_staff_totp_setup and verify_staff_admin_login. Platform audit
rows (merchant_id NULL) are invisible through RLS by design, so they are read
with the spec-08 test helper."""
import threading

import pyotp
import pytest
from cryptography.fernet import Fernet
from django.db import DatabaseError, connection
from django.utils import timezone as dj_timezone

from accounts import services
from accounts.exceptions import InvalidTotpCode, NotActiveStaff, TotpAlreadyEnabled, TotpSetupRequired
from accounts.models import User
from whatsapp.tests.helpers import clear_platform_write_setting, platform_audit_rows

PASSWORD = "Tr1cky-Horse-Battery-Staple!"


@pytest.fixture(autouse=True)
def fernet_key(settings):
    settings.FERNET_KEY = Fernet.generate_key().decode()


def make_staff(email="staff@example.com", **extra):
    return User.objects.create_user(email, PASSWORD, is_staff=True, **extra)


def enroll(user):
    """begin + confirm, confirming with the previous step's code so the
    current step stays usable by the test (replay protection)."""
    secret, _uri = services.begin_staff_totp_setup(user=user)
    codes = services.confirm_staff_totp_setup(
        user=user, code=pyotp.TOTP(secret).at(dj_timezone.now(), counter_offset=-1)
    )
    return secret, codes


def now_code(secret):
    return pyotp.TOTP(secret).now()


def _break_record_platform(monkeypatch):
    def boom(*args, **kwargs):
        raise DatabaseError("audit insert failed")

    monkeypatch.setattr(services, "record_platform", boom)


# --- enrollment ----------------------------------------------------------------


@pytest.mark.django_db
class TestEnrollment:
    def test_begin_refuses_a_non_staff_or_inactive_user(self):
        plain = User.objects.create_user("plain@example.com", PASSWORD)
        inactive = make_staff("off@example.com", is_active=False)
        for user in (plain, inactive):
            with pytest.raises(NotActiveStaff):
                services.begin_staff_totp_setup(user=user)
            user.refresh_from_db()
            assert user.totp_secret_encrypted is None

    def test_confirm_enables_2fa_returns_ten_codes_and_writes_one_platform_row(self):
        staff = make_staff()
        _secret, codes = enroll(staff)
        clear_platform_write_setting()
        staff.refresh_from_db()
        assert staff.is_totp_enabled
        assert len(codes) == 10 and len(staff.totp_recovery_code_hashes) == 10
        rows = platform_audit_rows()
        assert [(r["action"], r["actor_user_id"], r["target_id"], r["metadata"]) for r in rows] == [
            ("user.totp_enabled", staff.id, str(staff.id), None)
        ]

    def test_begin_and_confirm_refuse_an_already_enrolled_user(self):
        staff = make_staff()
        secret, _codes = enroll(staff)
        with pytest.raises(TotpAlreadyEnabled):
            services.begin_staff_totp_setup(user=staff)
        with pytest.raises(TotpAlreadyEnabled):
            services.confirm_staff_totp_setup(user=staff, code=now_code(secret))

    def test_confirm_without_begin_and_with_a_wrong_code(self):
        staff = make_staff()
        with pytest.raises(TotpSetupRequired):
            services.confirm_staff_totp_setup(user=staff, code="123456")
        services.begin_staff_totp_setup(user=staff)
        with pytest.raises(InvalidTotpCode):
            services.confirm_staff_totp_setup(user=staff, code="not-a-code")
        clear_platform_write_setting()
        staff.refresh_from_db()
        assert not staff.is_totp_enabled
        assert platform_audit_rows() == []

    def test_a_failed_audit_insert_leaves_2fa_disabled(self, monkeypatch):
        staff = make_staff()
        secret, _uri = services.begin_staff_totp_setup(user=staff)
        _break_record_platform(monkeypatch)
        with pytest.raises(DatabaseError):
            services.confirm_staff_totp_setup(
                user=staff, code=pyotp.TOTP(secret).at(dj_timezone.now(), counter_offset=-1)
            )
        clear_platform_write_setting()
        staff.refresh_from_db()
        assert not staff.is_totp_enabled
        assert staff.totp_recovery_code_hashes == []
        assert platform_audit_rows() == []


# --- admin login second factor -------------------------------------------------


@pytest.mark.django_db
class TestVerifyStaffAdminLogin:
    def test_a_valid_totp_code_passes_and_its_replay_fails(self):
        staff = make_staff()
        secret, _ = enroll(staff)
        code = now_code(secret)
        assert services.verify_staff_admin_login(user=staff, code=code) is True
        assert services.verify_staff_admin_login(user=staff, code=code) is False

    def test_wrong_empty_and_none_codes_fail(self):
        staff = make_staff()
        enroll(staff)
        for code in ("000000", "", "   ", None):
            assert services.verify_staff_admin_login(user=staff, code=code) is False

    def test_non_staff_inactive_or_unenrolled_users_fail(self):
        staff = make_staff()
        secret, _ = enroll(staff)
        User.objects.filter(pk=staff.pk).update(is_staff=False)
        assert services.verify_staff_admin_login(user=staff, code=now_code(secret)) is False
        User.objects.filter(pk=staff.pk).update(is_staff=True, is_active=False)
        assert services.verify_staff_admin_login(user=staff, code=now_code(secret)) is False
        unenrolled = make_staff("new@example.com")
        assert services.verify_staff_admin_login(user=unenrolled, code="123456") is False

    def test_a_six_digit_code_never_opens_the_staff_auth_scope(self, monkeypatch):
        staff = make_staff()
        secret, _ = enroll(staff)

        def forbidden(scope):
            raise AssertionError(f"platform_write_atomic({scope!r}) opened for a TOTP code")

        monkeypatch.setattr(services, "platform_write_atomic", forbidden)
        assert services.verify_staff_admin_login(user=staff, code=now_code(secret)) is True
        assert services.verify_staff_admin_login(user=staff, code="000000") is False

    def test_a_recovery_code_works_once_and_is_audited_with_the_count_only(self):
        staff = make_staff()
        _secret, codes = enroll(staff)
        assert services.verify_staff_admin_login(user=staff, code=codes[0]) is True
        assert services.verify_staff_admin_login(user=staff, code=codes[0]) is False
        clear_platform_write_setting()
        used = [r for r in platform_audit_rows() if r["action"] == "user.totp_recovery_code_used"]
        assert [(r["actor_user_id"], r["metadata"]) for r in used] == [
            (staff.id, {"recovery_codes_remaining": 9})
        ]
        assert codes[0] not in str(used)

    def test_a_failed_recovery_audit_leaves_the_code_valid(self, monkeypatch):
        staff = make_staff()
        _secret, codes = enroll(staff)
        with monkeypatch.context() as m:
            _break_record_platform(m)
            with pytest.raises(DatabaseError):
                services.verify_staff_admin_login(user=staff, code=codes[0])
        staff.refresh_from_db()
        assert len(staff.totp_recovery_code_hashes) == 10
        assert services.verify_staff_admin_login(user=staff, code=codes[0]) is True

    def test_a_wrong_recovery_code_changes_nothing_and_writes_nothing(self):
        staff = make_staff()
        enroll(staff)
        assert services.verify_staff_admin_login(user=staff, code="aaaaa-bbbbb-ccccc-ddddd") is False
        clear_platform_write_setting()
        staff.refresh_from_db()
        assert len(staff.totp_recovery_code_hashes) == 10
        assert [r["action"] for r in platform_audit_rows()] == ["user.totp_enabled"]


# --- concurrency: one success per code ------------------------------------------


def _race(staff, code):
    barrier = threading.Barrier(2)
    results, errors = [], []

    def _run():
        try:
            barrier.wait(timeout=5)
            results.append(services.verify_staff_admin_login(user=staff, code=code))
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    return sorted(results)


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_uses_of_one_totp_code_give_exactly_one_success():
    staff = make_staff()
    secret, _ = enroll(staff)
    assert _race(staff, now_code(secret)) == [False, True]


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_uses_of_one_recovery_code_give_exactly_one_success():
    staff = make_staff()
    _secret, codes = enroll(staff)
    assert _race(staff, codes[0]) == [False, True]
    staff.refresh_from_db()
    assert len(staff.totp_recovery_code_hashes) == 9
