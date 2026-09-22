"""Service-layer unit tests for TOTP 2FA (accounts/services.py). No
external providers are used. core/crypto.py's own tests live in
core/tests/test_crypto.py.

Every test that touches core.crypto needs a valid FERNET_KEY; the
`fernet_key` fixture below overrides settings for the whole module.
"""
import pyotp
import pytest
from cryptography.fernet import Fernet
from django.utils import timezone as dj_timezone

from accounts import services
from accounts.exceptions import (
    InvalidCredentials,
    InvalidTotpCode,
    ReauthenticationFailed,
    TotpAlreadyEnabled,
    TotpNotEnabled,
    TotpSetupRequired,
)
from accounts.models import User
from auditlog.models import AuditLog
from core import crypto
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def fernet_key(settings):
    settings.FERNET_KEY = Fernet.generate_key().decode()


_PASSWORD = "Tr1cky-Horse-Battery-Staple!"


def _enroll(user, merchant_id):
    """Runs begin_totp_setup + confirm_totp_setup like an authenticated
    request already inside the session merchant's tenant context. Returns
    (secret, recovery_codes).

    Confirms with the previous time step's code: confirm consumes the step
    it accepts (replay protection), so confirming with .now() would make a
    test's next .now() code a replay within the same 30 s window."""
    with tenant_context(merchant_id):
        secret, _uri = services.begin_totp_setup(user=user, password=_PASSWORD)
        confirm_code = pyotp.TOTP(secret).at(dj_timezone.now(), counter_offset=-1)
        codes = services.confirm_totp_setup(user=user, code=confirm_code)
    return secret, codes


# --- Model defaults (migration on a database with existing users) ---------


def test_new_user_has_totp_disabled_by_default_with_empty_recovery_codes():
    """Proxy for accounts.0003_user_totp applying cleanly to existing rows:
    a User created without touching any TOTP field lands in the same state
    a pre-migration user must have after the migration runs."""
    user = User.objects.create_user("plain@example.com", _PASSWORD)
    assert user.totp_secret_encrypted is None
    assert user.totp_confirmed_at is None
    assert user.totp_last_used_step is None
    assert user.totp_recovery_code_hashes == []
    assert user.is_totp_enabled is False


# --- begin_totp_setup -----------------------------------------------------


def test_begin_totp_setup_wrong_password_raises_reauthentication_failed(make_merchant):
    member = make_merchant()
    with tenant_context(member.merchant_id):
        with pytest.raises(ReauthenticationFailed):
            services.begin_totp_setup(user=member.user, password="wrong")


def test_begin_totp_setup_stores_only_the_encrypted_secret(make_merchant):
    member = make_merchant()
    with tenant_context(member.merchant_id):
        secret, uri = services.begin_totp_setup(user=member.user, password=_PASSWORD)
    stored = User.objects.get(pk=member.user_id).totp_secret_encrypted
    assert stored is not None
    assert stored != secret
    assert crypto.decrypt(stored) == secret
    assert secret in uri


def test_begin_totp_setup_already_enrolled_raises_totp_already_enabled(make_merchant):
    member = make_merchant()
    _enroll(member.user, member.merchant_id)
    with tenant_context(member.merchant_id):
        with pytest.raises(TotpAlreadyEnabled):
            services.begin_totp_setup(user=member.user, password=_PASSWORD)


def test_begin_totp_setup_second_call_replaces_pending_secret(make_merchant):
    member = make_merchant()
    with tenant_context(member.merchant_id):
        secret1, _uri1 = services.begin_totp_setup(user=member.user, password=_PASSWORD)
        secret2, _uri2 = services.begin_totp_setup(user=member.user, password=_PASSWORD)
        assert secret1 != secret2
        codes = services.confirm_totp_setup(user=member.user, code=pyotp.TOTP(secret2).now())
    assert len(codes) == 10


# --- confirm_totp_setup ----------------------------------------------------


def test_confirm_totp_setup_returns_10_unique_codes_and_sets_confirmed_at(make_merchant):
    member = make_merchant()
    secret, codes = _enroll(member.user, member.merchant_id)
    assert len(codes) == 10
    assert len(set(codes)) == 10
    user = User.objects.get(pk=member.user_id)
    assert user.is_totp_enabled
    assert len(user.totp_recovery_code_hashes) == 10


def test_confirm_totp_setup_writes_exactly_one_audit_row_with_no_secret_leakage(make_merchant):
    member = make_merchant()
    secret, codes = _enroll(member.user, member.merchant_id)
    with tenant_context(member.merchant_id), tenant_atomic():
        rows = list(AuditLog.objects.filter(action=services.AUDIT_TOTP_ENABLED))
    assert len(rows) == 1
    assert rows[0].actor_user_id == member.user_id
    assert rows[0].metadata_json is None
    assert secret not in str(rows[0].metadata_json)
    for code in codes:
        assert code not in str(rows[0].metadata_json)


def test_confirm_totp_setup_wrong_code_raises_invalid_totp_code(make_merchant):
    member = make_merchant()
    with tenant_context(member.merchant_id):
        services.begin_totp_setup(user=member.user, password=_PASSWORD)
        with pytest.raises(InvalidTotpCode):
            services.confirm_totp_setup(user=member.user, code="000000")


def test_confirm_totp_setup_without_pending_secret_raises_totp_setup_required(make_merchant):
    member = make_merchant()
    with tenant_context(member.merchant_id):
        with pytest.raises(TotpSetupRequired):
            services.confirm_totp_setup(user=member.user, code="123456")


def test_confirm_totp_setup_already_enrolled_raises_totp_already_enabled(make_merchant):
    member = make_merchant()
    secret, _codes = _enroll(member.user, member.merchant_id)
    with tenant_context(member.merchant_id):
        with pytest.raises(TotpAlreadyEnabled):
            services.confirm_totp_setup(user=member.user, code=pyotp.TOTP(secret).now())


# --- disable_totp ------------------------------------------------------


def test_disable_totp_not_enrolled_raises_totp_not_enabled(make_merchant):
    member = make_merchant()
    with tenant_context(member.merchant_id):
        with pytest.raises(TotpNotEnabled):
            services.disable_totp(user=member.user, password=_PASSWORD, code="123456")


def test_disable_totp_wrong_password_raises_reauthentication_failed(make_merchant):
    member = make_merchant()
    secret, _codes = _enroll(member.user, member.merchant_id)
    with tenant_context(member.merchant_id):
        with pytest.raises(ReauthenticationFailed):
            services.disable_totp(user=member.user, password="wrong", code=pyotp.TOTP(secret).now())


def test_disable_totp_wrong_code_raises_reauthentication_failed(make_merchant):
    member = make_merchant()
    _enroll(member.user, member.merchant_id)
    with tenant_context(member.merchant_id):
        with pytest.raises(ReauthenticationFailed):
            services.disable_totp(user=member.user, password=_PASSWORD, code="000000")


def test_disable_totp_clears_all_four_fields_and_writes_one_audit_row(make_merchant):
    member = make_merchant()
    secret, _codes = _enroll(member.user, member.merchant_id)
    with tenant_context(member.merchant_id):
        services.disable_totp(user=member.user, password=_PASSWORD, code=pyotp.TOTP(secret).now())
    user = User.objects.get(pk=member.user_id)
    assert user.totp_secret_encrypted is None
    assert user.totp_confirmed_at is None
    assert user.totp_last_used_step is None
    assert user.totp_recovery_code_hashes == []
    with tenant_context(member.merchant_id), tenant_atomic():
        rows = list(AuditLog.objects.filter(action=services.AUDIT_TOTP_DISABLED))
    assert len(rows) == 1


# --- make_pending_totp / record_failed_totp_attempt ------------------------


def test_make_pending_totp_marker_has_no_auth_user_id_or_merchant_id(make_merchant):
    member = make_merchant()
    marker = services.make_pending_totp(member.user)
    assert marker == {"u": str(member.user.id), "iat": marker["iat"], "n": 0}
    assert "merchant_id" not in marker
    assert "_auth_user_id" not in marker


def test_record_failed_totp_attempt_reaches_cap_after_five_failures():
    pending = {"u": "x", "iat": 0, "n": 0}
    for expected_n in range(1, 5):
        pending = services.record_failed_totp_attempt(pending)
        assert pending is not None
        assert pending["n"] == expected_n
    assert services.record_failed_totp_attempt(pending) is None


# --- complete_totp_login: replay protection ---------------------------------


def test_complete_totp_login_replay_of_an_accepted_code_is_rejected(make_merchant):
    member = make_merchant()
    secret, _codes = _enroll(member.user, member.merchant_id)
    code = pyotp.TOTP(secret).now()

    services.complete_totp_login(pending=services.make_pending_totp(member.user), code=code)
    with pytest.raises(InvalidCredentials):
        services.complete_totp_login(pending=services.make_pending_totp(member.user), code=code)


def test_complete_totp_login_rejects_a_step_at_or_before_last_used_step(make_merchant):
    member = make_merchant()
    secret, _codes = _enroll(member.user, member.merchant_id)
    future_step = pyotp.TOTP(secret).timecode(dj_timezone.now()) + 100
    User.objects.filter(pk=member.user_id).update(totp_last_used_step=future_step)

    with pytest.raises(InvalidCredentials):
        services.complete_totp_login(
            pending=services.make_pending_totp(member.user), code=pyotp.TOTP(secret).now()
        )


# --- complete_totp_login: recovery codes ------------------------------------


def test_complete_totp_login_recovery_code_normalizes_case_and_hyphens(make_merchant):
    member = make_merchant()
    _secret, codes = _enroll(member.user, member.merchant_id)
    variant = codes[0].upper().replace("-", "")

    services.complete_totp_login(pending=services.make_pending_totp(member.user), code=variant)

    assert len(User.objects.get(pk=member.user_id).totp_recovery_code_hashes) == 9


def test_complete_totp_login_recovery_code_fails_on_reuse(make_merchant):
    member = make_merchant()
    _secret, codes = _enroll(member.user, member.merchant_id)

    services.complete_totp_login(pending=services.make_pending_totp(member.user), code=codes[0])
    with pytest.raises(InvalidCredentials):
        services.complete_totp_login(pending=services.make_pending_totp(member.user), code=codes[0])


def test_complete_totp_login_recovery_code_writes_audit_row_with_remaining_count(make_merchant):
    member = make_merchant()
    _secret, codes = _enroll(member.user, member.merchant_id)

    services.complete_totp_login(pending=services.make_pending_totp(member.user), code=codes[0])

    with tenant_context(member.merchant_id), tenant_atomic():
        rows = list(AuditLog.objects.filter(action=services.AUDIT_TOTP_RECOVERY_USED))
    assert len(rows) == 1
    assert rows[0].metadata_json == {"recovery_codes_remaining": 9}
