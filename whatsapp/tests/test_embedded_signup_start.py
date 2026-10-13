"""POST /whatsapp/embedded-signup/start (spec 08-whatsapp-embedded-signup,
P6b `start` only).

Everything here is ReviewFlow's own behaviour: gating, state storage,
permissions and throttling. `start` makes no Meta call, so there is nothing
to mock; `meta_http` is used only to assert that no request was sent. These
tests are not evidence about Meta, and the `complete` endpoint, code exchange
and ownership check are deliberately absent (blocked on E-2b/E-10 and E-4)."""
import hashlib
import json
import logging
from datetime import timedelta

import pytest
from django.conf import settings as django_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from rest_framework.test import APIClient

from auditlog.models import AuditLog
from core.tenancy import tenant_atomic, tenant_context
from whatsapp.models import EmbeddedSignupState

pytestmark = pytest.mark.django_db

START = "/api/v1/whatsapp/embedded-signup/start"
APP_ID = "TEST-APP-ID"
CONFIG_ID = "TEST-CONFIG-ID"


@pytest.fixture
def signup_enabled(settings, fernet_key):
    settings.WHATSAPP_EMBEDDED_SIGNUP_ENABLED = True
    settings.META_APP_ID = APP_ID
    settings.META_APP_SECRET = "test-app-secret"
    settings.META_EMBEDDED_SIGNUP_CONFIG_ID = CONFIG_ID
    return settings


@pytest.fixture
def owner_client(make_merchant, session_client):
    owner = make_merchant("A")
    return owner, session_client(owner.user.email)


def start(client):
    return client.post(START, {}, format="json", HTTP_X_CSRFTOKEN=client.csrf)


def rows(merchant):
    with tenant_context(merchant.id), tenant_atomic():
        return list(EmbeddedSignupState.objects.all())


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


# --- configuration gating (ES-10) -------------------------------------------------------------


def test_the_flag_defaults_to_off():
    assert django_settings.WHATSAPP_EMBEDDED_SIGNUP_ENABLED is False


def test_flag_off_returns_503_and_stores_nothing(owner_client, signup_enabled, meta_http):
    owner, client = owner_client
    signup_enabled.WHATSAPP_EMBEDDED_SIGNUP_ENABLED = False

    resp = start(client)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "whatsapp_not_configured"
    assert rows(owner.merchant) == []
    assert meta_http.requests == []


@pytest.mark.parametrize(
    "setting", ["META_APP_ID", "META_APP_SECRET", "META_EMBEDDED_SIGNUP_CONFIG_ID", "FERNET_KEY"]
)
def test_any_unset_setting_returns_503_and_stores_nothing(owner_client, signup_enabled, setting):
    owner, client = owner_client
    setattr(signup_enabled, setting, "")

    resp = start(client)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "whatsapp_not_configured"
    assert rows(owner.merchant) == []


def test_the_default_test_settings_are_disabled(owner_client):
    owner, client = owner_client

    assert start(client).status_code == 503
    assert rows(owner.merchant) == []


# --- happy path and state handling (ES-18) -----------------------------------------------------


def test_start_returns_state_app_id_and_config_id_and_stores_one_bound_row(
    owner_client, signup_enabled, meta_http
):
    owner, client = owner_client

    resp = start(client)

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"state", "app_id", "config_id"}
    assert body["app_id"] == APP_ID
    assert body["config_id"] == CONFIG_ID
    (row,) = rows(owner.merchant)
    assert row.merchant_id == owner.merchant.id
    assert row.user_id == owner.user.id
    assert (row.expires_at - row.created_at).total_seconds() == pytest.approx(
        signup_enabled.WHATSAPP_SIGNUP_STATE_MAX_AGE_SECONDS, abs=5
    )
    assert meta_http.requests == []  # start makes no Meta call


def test_only_the_hash_is_stored_never_the_plaintext(owner_client, signup_enabled):
    owner, client = owner_client

    state = start(client).json()["state"]

    (row,) = rows(owner.merchant)
    assert row.state_hash == sha(state)
    stored = {str(getattr(row, f.attname)) for f in row._meta.concrete_fields}
    assert state not in stored


def test_each_start_issues_a_distinct_state(owner_client, signup_enabled):
    owner, client = owner_client

    first = start(client).json()["state"]
    second = start(client).json()["state"]

    assert first != second
    assert len(rows(owner.merchant)) == 2


def test_the_state_is_never_logged_or_audited(owner_client, signup_enabled, caplog):
    owner, client = owner_client
    caplog.set_level(logging.DEBUG)

    state = start(client).json()["state"]

    assert state not in caplog.text
    with tenant_context(owner.merchant.id), tenant_atomic():
        audit = json.dumps(list(AuditLog.objects.values()), default=str)
    assert state not in audit


def test_start_purges_only_the_callers_expired_rows(owner_client, make_merchant, signup_enabled):
    owner, client = owner_client
    other = make_merchant("B")
    for merchant, user in ((owner.merchant, owner.user), (other.merchant, other.user)):
        with tenant_context(merchant.id), tenant_atomic():
            EmbeddedSignupState.objects.create(
                merchant=merchant,
                user=user,
                state_hash=sha(f"stale-{merchant.id}"),
                expires_at=timezone.now() - timedelta(minutes=1),
            )

    assert start(client).status_code == 200

    assert sha(f"stale-{owner.merchant.id}") not in {r.state_hash for r in rows(owner.merchant)}
    assert [r.state_hash for r in rows(other.merchant)] == [sha(f"stale-{other.merchant.id}")]


def test_the_response_carries_no_tenant_ids(owner_client, signup_enabled):
    owner, client = owner_client

    text = start(client).content.decode()

    assert str(owner.merchant.id) not in text
    assert "merchant" not in text


# --- permissions, CSRF, throttle (ES-7) --------------------------------------------------------


def test_an_admin_may_start(owner_client, add_member, session_client, signup_enabled):
    owner, _ = owner_client
    add_member(owner.merchant, "ADMIN", "admin@example.com")

    assert start(session_client("admin@example.com")).status_code == 200


@pytest.mark.parametrize("role", ["MANAGER", "VIEWER"])
def test_manager_and_viewer_are_forbidden_and_store_nothing(
    owner_client, add_member, session_client, signup_enabled, role
):
    owner, _ = owner_client
    add_member(owner.merchant, role, f"{role.lower()}@example.com")

    resp = start(session_client(f"{role.lower()}@example.com"))

    assert resp.status_code == 403
    assert rows(owner.merchant) == []


def test_anonymous_is_refused_and_stores_nothing(owner_client, signup_enabled):
    owner, _ = owner_client

    resp = APIClient().post(START, {}, format="json")

    assert resp.status_code in (401, 403)
    assert rows(owner.merchant) == []


def test_a_missing_csrf_token_is_refused(owner_client, signup_enabled):
    owner, client = owner_client

    resp = client.post(START, {}, format="json")

    assert resp.status_code == 403
    assert rows(owner.merchant) == []


def test_start_is_throttled_per_user(owner_client, signup_enabled, throttle_rates):
    throttle_rates(whatsapp_signup="2/min")
    owner, client = owner_client

    assert start(client).status_code == 200
    assert start(client).status_code == 200
    assert start(client).status_code == 429
    assert len(rows(owner.merchant)) == 2


# --- regression: `complete` stays absent (P6b blocked on E-4, E-2b/E-10) ------------------------


def test_the_complete_route_does_not_exist(owner_client, signup_enabled):
    owner, client = owner_client

    resp = client.post(
        "/api/v1/whatsapp/embedded-signup/complete", {}, format="json", HTTP_X_CSRFTOKEN=client.csrf
    )

    assert resp.status_code == 404
    with pytest.raises(NoReverseMatch):
        reverse("whatsapp-embedded-signup-complete")
