"""Django Admin staff login, through the REAL /admin/login/ flow (spec 16 D2;
plan step 4). No helper sets the session flag here: every success comes from
password + second factor. accounts/tests/conftest.py's autouse local_cache
keeps the throttle counters local."""
import re

import pyotp
import pytest
from cryptography.fernet import Fernet
from django.db import DatabaseError
from django.test import Client
from django.utils import timezone as dj_timezone

from accounts import services
from accounts.models import User
from config.admin import ADMIN_2FA_SESSION_KEY

pytestmark = pytest.mark.django_db

PASSWORD = "Tr1cky-Horse-Battery-Staple!"
LOGIN = "/admin/login/"
INDEX = "/admin/"
GENERIC_ERROR = "Please enter the correct email and password for a staff account."


@pytest.fixture(autouse=True)
def admin_env(settings):
    settings.FERNET_KEY = Fernet.generate_key().decode()
    settings.ADMIN_ALLOWED_IPS = ["127.0.0.1/32"]


def enrolled_staff(email="staff@example.com", **extra):
    user = User.objects.create_user(email, PASSWORD, is_staff=True, **extra)
    secret, _ = services.begin_staff_totp_setup(user=user)
    codes = services.confirm_staff_totp_setup(
        user=user, code=pyotp.TOTP(secret).at(dj_timezone.now(), counter_offset=-1)
    )
    return user, secret, codes


def post_login(client, email, password=PASSWORD, code=""):
    return client.post(LOGIN, {"username": email, "password": password, "otp_code": code, "next": INDEX})


def reaches_index(client):
    return client.get(INDEX).status_code == 200


def test_the_login_page_renders_the_second_factor_field():
    html = Client().get(LOGIN).content.decode()
    assert 'name="otp_code"' in html


def test_password_plus_a_valid_totp_code_reaches_the_admin_index():
    user, secret, _ = enrolled_staff()
    client = Client()
    resp = post_login(client, user.email, code=pyotp.TOTP(secret).now())
    assert resp.status_code == 302 and resp["Location"] == INDEX
    assert client.session[ADMIN_2FA_SESSION_KEY] == str(user.pk)
    assert reaches_index(client)


def _assert_rejected(resp, client):
    assert resp.status_code == 200
    assert GENERIC_ERROR in resp.content.decode()
    assert ADMIN_2FA_SESSION_KEY not in client.session
    assert not reaches_index(client)


def test_a_replayed_totp_code_is_rejected():
    user, secret, _ = enrolled_staff()
    code = pyotp.TOTP(secret).now()
    assert post_login(Client(), user.email, code=code).status_code == 302
    replay = Client()
    _assert_rejected(post_login(replay, user.email, code=code), replay)


@pytest.mark.parametrize("code", ["", "000000", "not-a-code"])
def test_a_missing_or_wrong_code_is_rejected_with_the_generic_error(code):
    user, _secret, _ = enrolled_staff()
    client = Client()
    _assert_rejected(post_login(client, user.email, code=code), client)


@pytest.mark.parametrize(
    "username, password",
    [("staff@example.com", ""), ("", PASSWORD), ("", "")],
    ids=["blank-password", "blank-username", "blank-both"],
)
def test_a_blank_credential_with_a_populated_code_is_the_generic_error_not_a_500(username, password):
    """Regression (code review 2026-10-13): the parent form never authenticates
    when a field is blank, so get_user() is None; that used to reach
    user.id and answer 500."""
    user, secret, _ = enrolled_staff()
    client = Client(raise_request_exception=False)
    resp = client.post(
        LOGIN,
        {"username": username, "password": password, "otp_code": pyotp.TOTP(secret).now(), "next": INDEX},
    )
    assert resp.status_code == 200
    assert ADMIN_2FA_SESSION_KEY not in client.session
    assert "This field is required" in resp.content.decode() or GENERIC_ERROR in resp.content.decode()
    # The valid code was not consumed by the rejected attempt.
    ok = Client()
    assert post_login(ok, user.email, code=pyotp.TOTP(secret).now()).status_code == 302


def test_a_wrong_password_with_a_valid_code_is_rejected_identically():
    user, secret, _ = enrolled_staff()
    client = Client()
    _assert_rejected(post_login(client, user.email, password="wrong", code=pyotp.TOTP(secret).now()), client)


def test_a_staff_user_without_2fa_cannot_log_in():
    User.objects.create_user("bare@example.com", PASSWORD, is_staff=True)
    client = Client()
    _assert_rejected(post_login(client, "bare@example.com", code="123456"), client)


def test_a_non_staff_user_with_2fa_cannot_log_in():
    user, secret, _ = enrolled_staff()
    User.objects.filter(pk=user.pk).update(is_staff=False)
    client = Client()
    _assert_rejected(post_login(client, user.email, code=pyotp.TOTP(secret).now()), client)


def test_a_recovery_code_logs_in_once():
    user, _secret, codes = enrolled_staff()
    first = Client()
    assert post_login(first, user.email, code=codes[0]).status_code == 302
    assert reaches_index(first)
    second = Client()
    _assert_rejected(post_login(second, user.email, code=codes[0]), second)


def test_a_failed_recovery_audit_gives_the_generic_error_and_keeps_the_code_valid(monkeypatch):
    user, _secret, codes = enrolled_staff()

    def boom(*args, **kwargs):
        raise DatabaseError("audit insert failed")

    with monkeypatch.context() as m:
        m.setattr(services, "record_platform", boom)
        client = Client()
        _assert_rejected(post_login(client, user.email, code=codes[0]), client)
    retry = Client()
    assert post_login(retry, user.email, code=codes[0]).status_code == 302


def test_force_login_without_the_flag_is_redirected_to_the_admin_login():
    user, _secret, _ = enrolled_staff()
    client = Client()
    client.force_login(user)
    resp = client.get(INDEX)
    assert resp.status_code == 302 and resp["Location"].startswith(LOGIN)


def test_a_dashboard_only_staff_session_is_redirected(make_merchant, add_member, session_client):
    owner = make_merchant()
    user, _secret, _ = enrolled_staff("dashstaff@example.com")
    # Disable 2FA for the dashboard login only to keep this test about the
    # admin gate; the user is still staff.
    User.objects.filter(pk=user.pk).update(totp_confirmed_at=None)
    add_member(owner.merchant, "ADMIN", None, user=user)
    api = session_client(user.email)
    resp = api.get(INDEX)
    assert resp.status_code == 302 and resp["Location"].startswith(LOGIN)


def test_a_correct_login_on_another_users_session_reaches_the_index():
    other, other_secret, _ = enrolled_staff("other@example.com")
    user, secret, _ = enrolled_staff("staff2@example.com")
    client = Client()
    assert post_login(client, other.email, code=pyotp.TOTP(other_secret).now()).status_code == 302
    resp = post_login(client, user.email, code=pyotp.TOTP(secret).now())
    assert resp.status_code == 302
    assert client.session[ADMIN_2FA_SESSION_KEY] == str(user.pk)
    assert reaches_index(client)


def test_the_sixth_login_post_from_one_ip_within_a_minute_is_429():
    client = Client()
    for _ in range(5):
        assert post_login(client, "nobody@example.com", code="000000").status_code == 200
    assert post_login(client, "nobody@example.com", code="000000").status_code == 429


def test_the_rendered_login_never_echoes_the_submitted_code():
    user, _secret, codes = enrolled_staff()
    html = post_login(Client(), user.email, password="wrong", code=codes[1]).content.decode()
    assert codes[1] not in html
    assert not re.search(r'name="otp_code"[^>]*value="', html)
