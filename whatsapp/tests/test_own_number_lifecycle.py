"""Own-number activation and disconnect (spec 08-whatsapp-embedded-signup P5;
Definition of done "Activation", "Mapping & migration (reuse)", "Disconnect").

Own-number rows are created through the model under the merchant's own
context (the tenant_insert policy), because the Embedded Signup completion
that creates them in production is P6b and is blocked on evidence. Only the
Meta HTTP boundary is mocked (`meta_http`)."""
import logging
from unittest.mock import Mock

import pytest

from auditlog.models import AuditLog
from core.crypto import decrypt
from core.tenancy import tenant_atomic, tenant_context
from whatsapp import services, tasks
from whatsapp.exceptions import SenderInUse, SenderNotFound
from whatsapp.models import WhatsAppAccount, WhatsAppLocationMapping
from whatsapp.providers.base import ProviderPermanentError
from whatsapp.tests.conftest import http_error, load_fixture

pytestmark = pytest.mark.django_db

OWN_TOKEN = "EAAOWN-merchant-business-token"
PHONE_ID = "200000000000001"


def reload(account, merchant):
    with tenant_context(merchant.id), tenant_atomic():
        return WhatsAppAccount.objects.get(pk=account.pk)


def audit(merchant, action):
    with tenant_context(merchant.id), tenant_atomic():
        return list(AuditLog.objects.filter(action=action).order_by("created_at"))


@pytest.fixture
def pending(make_merchant, make_own_account, fernet_key):
    owner = make_merchant("A")
    account = make_own_account(owner.merchant, status="PENDING", token=OWN_TOKEN, phone_number_id=PHONE_ID)
    return owner, account


# --- activation -----------------------------------------------------------------------------------


def test_activation_subscribes_with_the_merchants_own_token_and_activates(pending, meta_http):
    owner, account = pending
    meta_http.on(f"/{account.business_account_id}/subscribed_apps", load_fixture("success_true.json"))

    with tenant_context(owner.merchant_id):
        services.activate_own_number(account.id)

    row = reload(account, owner.merchant)
    assert row.status == "ACTIVE"
    assert row.connected_at is not None
    assert len(meta_http.requests) == 1
    assert meta_http.requests[0].get_header("Authorization") == f"Bearer {OWN_TOKEN}"
    assert meta_http.requests[0].get_method() == "POST"


def test_activation_keeps_the_original_connected_at(pending, meta_http):
    owner, account = pending
    meta_http.on("/subscribed_apps", load_fixture("success_true.json"))
    from django.utils import timezone

    earlier = timezone.now().replace(year=2026, month=1, day=1)
    with tenant_context(owner.merchant_id), tenant_atomic():
        WhatsAppAccount.objects.filter(pk=account.pk).update(connected_at=earlier)

    with tenant_context(owner.merchant_id):
        services.activate_own_number(account.id)

    assert reload(account, owner.merchant).connected_at == earlier


def test_activation_is_idempotent_and_a_repeat_makes_no_meta_call(pending, meta_http):
    owner, account = pending
    meta_http.on("/subscribed_apps", load_fixture("success_true.json"))

    with tenant_context(owner.merchant_id):
        services.activate_own_number(account.id)
        services.activate_own_number(account.id)

    assert len(meta_http.requests) == 1
    assert reload(account, owner.merchant).status == "ACTIVE"


def test_activation_never_touches_an_already_suspended_or_active_number(
    make_merchant, make_own_account, fernet_key, meta_http
):
    owner = make_merchant("A")
    suspended = make_own_account(owner.merchant, status="SUSPENDED", token=OWN_TOKEN)

    with tenant_context(owner.merchant_id):
        services.activate_own_number(suspended.id)

    assert meta_http.requests == []
    assert reload(suspended, owner.merchant).status == "SUSPENDED"


def test_activation_without_a_stored_token_is_a_no_op(make_merchant, make_own_account, meta_http):
    owner = make_merchant("A")
    account = make_own_account(owner.merchant, status="PENDING", token=None)

    with tenant_context(owner.merchant_id):
        services.activate_own_number(account.id)

    assert meta_http.requests == []
    assert reload(account, owner.merchant).status == "PENDING"


def test_a_transient_error_propagates_for_the_task_to_retry_and_leaves_pending(pending, meta_http):
    owner, account = pending
    meta_http.on("/subscribed_apps", http_error(503))

    from whatsapp.providers.base import ProviderTransientError

    with tenant_context(owner.merchant_id):
        with pytest.raises(ProviderTransientError):
            services.activate_own_number(account.id)

    assert reload(account, owner.merchant).status == "PENDING"
    assert audit(owner.merchant, "whatsapp.own_number_activation_failed") == []


def test_a_permanent_error_suspends_audits_and_keeps_the_encrypted_token(pending, meta_http):
    owner, account = pending
    meta_http.on("/subscribed_apps", http_error(400))

    with tenant_context(owner.merchant_id):
        services.activate_own_number(account.id)  # swallowed: no retry loop

    row = reload(account, owner.merchant)
    assert row.status == "SUSPENDED"
    # Approved behaviour: the token is NOT cleared on a permanent failure.
    assert row.access_token_enc and decrypt(row.access_token_enc) == OWN_TOKEN
    (event,) = audit(owner.merchant, "whatsapp.own_number_activation_failed")
    assert event.actor_user_id is None  # a system event
    assert event.metadata_json is None  # no ids, tokens or PINs
    assert event.target_id == str(account.pk)


def test_a_permanent_failure_is_not_audited_twice(pending, meta_http):
    owner, account = pending
    meta_http.on("/subscribed_apps", http_error(400))

    with tenant_context(owner.merchant_id):
        services.activate_own_number(account.id)
        services.activate_own_number(account.id)

    assert len(audit(owner.merchant, "whatsapp.own_number_activation_failed")) == 1
    assert len(meta_http.requests) == 1


def test_activation_never_calls_meta_inside_a_transaction_of_its_own(pending, meta_http, monkeypatch):
    owner, account = pending
    from django.db import connection

    seen = []

    def fake_urlopen(req, timeout=None):
        # pytest-django wraps the test in one atomic block; our code must add
        # no further savepoint around the Meta call.
        seen.append(len(connection.savepoint_ids))
        return meta_http(req, timeout)

    meta_http.on("/subscribed_apps", load_fixture("success_true.json"))
    monkeypatch.setattr("whatsapp.providers.meta_cloud.urllib.request.urlopen", fake_urlopen)

    with tenant_context(owner.merchant_id):
        services.activate_own_number(account.id)

    assert seen == [0]


def test_activation_for_another_merchants_account_changes_nothing(
    make_merchant, make_own_account, fernet_key, meta_http
):
    a, b = make_merchant("A"), make_merchant("B")
    foreign = make_own_account(b.merchant, status="PENDING", token=OWN_TOKEN)

    with tenant_context(a.merchant_id):
        services.activate_own_number(foreign.id)

    assert meta_http.requests == []
    assert reload(foreign, b.merchant).status == "PENDING"


def test_the_celery_task_takes_ids_only_and_runs_in_the_merchants_context(pending, meta_http):
    owner, account = pending
    meta_http.on("/subscribed_apps", load_fixture("success_true.json"))

    tasks.activate_own_number(owner.merchant_id, account.id)

    assert reload(account, owner.merchant).status == "ACTIVE"
    assert tasks.activate_own_number.queue == "default"
    # ids only: no code, token or PIN can be a task argument.
    import inspect

    assert list(inspect.signature(tasks.activate_own_number.run).parameters) == ["merchant_id", "account_id"]


def test_the_task_for_the_wrong_merchant_context_cannot_see_the_account(
    make_merchant, make_own_account, fernet_key, meta_http
):
    a, b = make_merchant("A"), make_merchant("B")
    foreign = make_own_account(b.merchant, status="PENDING", token=OWN_TOKEN)

    tasks.activate_own_number(a.merchant_id, foreign.id)

    assert meta_http.requests == []
    assert reload(foreign, b.merchant).status == "PENDING"


def test_activation_logs_contain_no_token(pending, meta_http, caplog):
    owner, account = pending
    caplog.set_level(logging.DEBUG)
    meta_http.on("/subscribed_apps", http_error(400))

    with tenant_context(owner.merchant_id):
        services.activate_own_number(account.id)

    assert OWN_TOKEN not in caplog.text


# --- reuse of the existing mapping / migration path -------------------------------------------------


def test_an_active_own_number_can_be_mapped_and_the_migration_is_audited(
    make_merchant, make_own_account, make_location, shared_account, fernet_key
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    own = make_own_account(owner.merchant, status="ACTIVE", token=OWN_TOKEN)
    with tenant_context(owner.merchant_id):
        services.set_location_sender(location=location, account=shared_account, actor=owner.user)
        services.set_location_sender(location=location, account=own, actor=owner.user)
        assert services.get_location_sender(location).id == own.id

    changes = audit(owner.merchant, "whatsapp.sender_changed")
    assert changes[-1].metadata_json == {"from_sender_type": "SHARED_POOL", "to_sender_type": "OWN_NUMBER"}


@pytest.mark.parametrize("status", ["PENDING", "SUSPENDED"])
def test_a_non_active_own_number_cannot_be_mapped(make_merchant, make_own_account, make_location, status):
    from django.core.exceptions import ValidationError

    owner = make_merchant("A")
    location = make_location(owner.merchant)
    own = make_own_account(owner.merchant, status=status)

    with tenant_context(owner.merchant_id):
        with pytest.raises(ValidationError):
            services.set_location_sender(location=location, account=own, actor=owner.user)


def test_list_senders_returns_own_numbers_in_every_status_but_the_shared_row_only_when_active(
    make_merchant, make_own_account, make_shared_account
):
    owner = make_merchant("A")
    for status in ("ACTIVE", "PENDING", "SUSPENDED"):
        make_own_account(owner.merchant, status=status)
    make_shared_account("SUSPENDED")

    with tenant_context(owner.merchant_id), tenant_atomic():
        listed = list(services.list_senders())

    assert sorted(a.status for a in listed if a.sender_type == "OWN_NUMBER") == [
        "ACTIVE",
        "PENDING",
        "SUSPENDED",
    ]
    assert not [a for a in listed if a.sender_type == "SHARED_POOL"]


# --- disconnect (service) -------------------------------------------------------------------------


def test_disconnect_suspends_clears_the_token_audits_and_makes_no_meta_call(
    make_merchant, make_own_account, fernet_key, meta_http
):
    owner = make_merchant("A")
    account = make_own_account(owner.merchant, token=OWN_TOKEN)

    with tenant_context(owner.merchant_id):
        services.disconnect_own_number(account=account, actor=owner.user)

    row = reload(account, owner.merchant)
    assert row.status == "SUSPENDED"
    assert row.access_token_enc is None
    assert meta_http.requests == []  # no deregister, no unsubscribe, nothing
    (event,) = audit(owner.merchant, "whatsapp.own_number_disconnected")
    assert event.actor_user_id == owner.user_id
    assert event.metadata_json is None


def test_disconnect_is_idempotent_with_no_second_audit_row(make_merchant, make_own_account, fernet_key):
    owner = make_merchant("A")
    account = make_own_account(owner.merchant, token=OWN_TOKEN)

    with tenant_context(owner.merchant_id):
        services.disconnect_own_number(account=account, actor=owner.user)
        services.disconnect_own_number(account=account, actor=owner.user)

    assert len(audit(owner.merchant, "whatsapp.own_number_disconnected")) == 1


def test_disconnect_is_refused_while_a_location_is_mapped_and_changes_nothing(
    make_merchant, make_own_account, make_location, fernet_key
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    account = make_own_account(owner.merchant, token=OWN_TOKEN)
    with tenant_context(owner.merchant_id):
        services.set_location_sender(location=location, account=account, actor=owner.user)
        with pytest.raises(SenderInUse):
            services.disconnect_own_number(account=account, actor=owner.user)

    row = reload(account, owner.merchant)
    assert row.status == "ACTIVE" and row.access_token_enc
    assert audit(owner.merchant, "whatsapp.own_number_disconnected") == []
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert WhatsAppLocationMapping.objects.count() == 1  # never unmapped implicitly


def test_after_unmapping_the_disconnect_succeeds_and_get_location_sender_ignores_it(
    make_merchant, make_own_account, make_location, fernet_key
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    account = make_own_account(owner.merchant, token=OWN_TOKEN)
    with tenant_context(owner.merchant_id):
        services.set_location_sender(location=location, account=account, actor=owner.user)
        services.clear_location_sender(location=location, actor=owner.user)
        services.disconnect_own_number(account=account, actor=owner.user)
        assert services.get_location_sender(location) is None


def test_disconnect_of_the_shared_account_or_another_merchants_is_not_found(
    make_merchant, make_own_account, shared_account, fernet_key
):
    a, b = make_merchant("A"), make_merchant("B")
    foreign = make_own_account(b.merchant, token=OWN_TOKEN)

    with tenant_context(a.merchant_id):
        with pytest.raises(SenderNotFound):
            services.disconnect_own_number(account=shared_account, actor=a.user)
        with pytest.raises(SenderNotFound):
            services.disconnect_own_number(account=foreign, actor=a.user)

    assert reload(foreign, b.merchant).access_token_enc  # untouched
    with tenant_context(a.merchant_id), tenant_atomic():
        assert WhatsAppAccount.objects.get(pk=shared_account.pk).status == "ACTIVE"


# --- DELETE /whatsapp/senders/{id} -----------------------------------------------------------------


def sender_url(account):
    return f"/api/v1/whatsapp/senders/{account.id}"


def delete(client, account):
    return client.delete(sender_url(account), HTTP_X_CSRFTOKEN=client.csrf)


@pytest.fixture
def api(make_merchant, make_own_account, session_client, fernet_key):
    owner = make_merchant("A")
    account = make_own_account(owner.merchant, token=OWN_TOKEN)
    return owner, account, session_client(owner.user.email)


def test_delete_disconnects_with_204_and_never_returns_ids_or_tokens(api, meta_http):
    owner, account, client = api

    resp = delete(client, account)

    assert resp.status_code == 204
    assert resp.content == b""
    assert reload(account, owner.merchant).status == "SUSPENDED"
    assert meta_http.requests == []
    # a repeat is idempotent
    assert delete(client, account).status_code == 204
    assert len(audit(owner.merchant, "whatsapp.own_number_disconnected")) == 1


def test_delete_while_mapped_answers_409_sender_in_use(api, make_location):
    owner, account, client = api
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id):
        services.set_location_sender(location=location, account=account, actor=owner.user)

    resp = delete(client, account)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "sender_in_use"
    assert reload(account, owner.merchant).status == "ACTIVE"


def test_delete_of_the_shared_account_unknown_or_foreign_id_is_404(
    api, shared_account, make_merchant, make_own_account
):
    owner, account, client = api
    other = make_merchant("B")
    foreign = make_own_account(other.merchant, token=OWN_TOKEN)

    assert delete(client, shared_account).status_code == 404
    assert delete(client, foreign).status_code == 404
    import uuid

    assert client.delete(f"/api/v1/whatsapp/senders/{uuid.uuid4()}", HTTP_X_CSRFTOKEN=client.csrf).status_code == 404
    assert reload(foreign, other.merchant).access_token_enc  # untouched


@pytest.mark.parametrize("role", ["VIEWER", "MANAGER"])
def test_delete_is_forbidden_to_viewer_and_manager(api, add_member, session_client, role):
    owner, account, _ = api
    add_member(owner.merchant, role, f"{role.lower()}@example.com")
    client = session_client(f"{role.lower()}@example.com")

    assert delete(client, account).status_code == 403
    assert reload(account, owner.merchant).status == "ACTIVE"


def test_an_admin_may_disconnect(api, add_member, session_client):
    owner, account, _ = api
    add_member(owner.merchant, "ADMIN", "admin@example.com")

    assert delete(session_client("admin@example.com"), account).status_code == 204


def test_delete_requires_authentication_and_csrf(api):
    owner, account, client = api
    from rest_framework.test import APIClient

    assert APIClient().delete(sender_url(account)).status_code == 403
    assert client.delete(sender_url(account)).status_code == 403  # no CSRF header
    assert reload(account, owner.merchant).status == "ACTIVE"


def test_the_senders_list_exposes_status_but_never_provider_ids_or_the_token(api):
    owner, account, client = api

    resp = client.get("/api/v1/whatsapp/senders")

    assert resp.status_code == 200
    body = resp.content.decode()
    assert OWN_TOKEN not in body
    assert account.phone_number_id not in body
    assert account.business_account_id not in body
    assert "access_token" not in body
    item = resp.json()["results"][0]
    assert set(item) == {"id", "sender_type", "status"}


# --- WhatsAppNotConfigured during activation (characterization) -----------------------------------
#
# Spec-silent: the spec retries transient provider errors and suspends on
# permanent ones, and says nothing about a missing or undecryptable stored
# token. These tests pin the CURRENT behaviour so a change is deliberate:
# the error propagates, nothing is recorded, and the row stays PENDING.


def test_an_undecryptable_stored_token_propagates_and_leaves_pending_with_no_meta_call(pending, meta_http):
    from whatsapp.exceptions import WhatsAppNotConfigured

    owner, account = pending
    with tenant_context(owner.merchant_id), tenant_atomic():
        WhatsAppAccount.objects.filter(pk=account.pk).update(access_token_enc="not-a-fernet-token")

    with tenant_context(owner.merchant_id):
        with pytest.raises(WhatsAppNotConfigured):
            services.activate_own_number(account.id)

    assert meta_http.requests == []  # failed closed before any HTTP call
    assert reload(account, owner.merchant).status == "PENDING"
    assert audit(owner.merchant, "whatsapp.own_number_activation_failed") == []  # nothing recorded


def test_an_unset_fernet_key_propagates_and_leaves_pending(pending, meta_http, settings):
    from whatsapp.exceptions import WhatsAppNotConfigured

    owner, account = pending
    settings.FERNET_KEY = ""

    with tenant_context(owner.merchant_id):
        with pytest.raises(WhatsAppNotConfigured):
            services.activate_own_number(account.id)

    assert meta_http.requests == []
    assert reload(account, owner.merchant).status == "PENDING"
    assert audit(owner.merchant, "whatsapp.own_number_activation_failed") == []


def test_the_task_does_not_auto_retry_whatsapp_not_configured(pending, meta_http, settings):
    """Only ProviderTransientError is auto-retried, so a configuration failure
    fails the task once and nothing re-enqueues it."""
    from whatsapp.exceptions import WhatsAppNotConfigured
    from whatsapp.providers.base import ProviderTransientError

    owner, account = pending
    settings.FERNET_KEY = ""

    assert tuple(tasks.activate_own_number.autoretry_for) == (ProviderTransientError,)
    with pytest.raises(WhatsAppNotConfigured):
        tasks.activate_own_number(owner.merchant_id, account.id)  # eager: raised, not retried
    assert reload(account, owner.merchant).status == "PENDING"


def test_once_the_configuration_is_fixed_a_rerun_activates(pending, meta_http, settings):
    """The row is not poisoned: the same task, rerun after the cause is fixed,
    activates it. Nothing reruns it automatically."""
    from whatsapp.exceptions import WhatsAppNotConfigured

    owner, account = pending
    good_key = settings.FERNET_KEY
    settings.FERNET_KEY = ""
    with tenant_context(owner.merchant_id):
        with pytest.raises(WhatsAppNotConfigured):
            services.activate_own_number(account.id)

    settings.FERNET_KEY = good_key
    meta_http.on("/subscribed_apps", load_fixture("success_true.json"))
    with tenant_context(owner.merchant_id):
        services.activate_own_number(account.id)

    assert reload(account, owner.merchant).status == "ACTIVE"


# --- quality-fix pass: the set_location_sender / disconnect race ----------------------------------
#
# A disconnect can commit between set_location_sender's first status check and
# its mapping insert. The account is now re-read under a row lock inside the
# transaction, so a mapping to a suspended number can no longer be created.


def test_a_stale_active_account_object_cannot_be_mapped_after_it_was_disconnected(
    make_merchant, make_own_account, make_location, fernet_key
):
    from django.core.exceptions import ValidationError

    owner = make_merchant("A")
    location = make_location(owner.merchant)
    stale = make_own_account(owner.merchant, token=OWN_TOKEN)  # in-memory: ACTIVE
    with tenant_context(owner.merchant_id):
        services.disconnect_own_number(account=stale, actor=owner.user)
    assert stale.status == "ACTIVE"  # the caller's object is stale

    with tenant_context(owner.merchant_id):
        with pytest.raises(ValidationError) as exc:
            services.set_location_sender(location=location, account=stale, actor=owner.user)

    assert exc.value.message_dict == {"whatsapp_account_id": ["This sender is not active."]}
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert WhatsAppLocationMapping.objects.count() == 0
    assert audit(owner.merchant, "whatsapp.sender_changed") == []


def test_a_stale_active_shared_account_is_rechecked_without_a_lock(
    make_merchant, make_location, make_shared_account
):
    from django.core.exceptions import ValidationError

    from whatsapp import services as svc

    owner = make_merchant("A")
    location = make_location(owner.merchant)
    shared = make_shared_account("ACTIVE")  # in-memory: ACTIVE
    make_shared_account("SUSPENDED")  # the platform path suspends it meanwhile

    with tenant_context(owner.merchant_id):
        with pytest.raises(ValidationError):
            svc.set_location_sender(location=location, account=shared, actor=owner.user)


def test_mapping_an_active_own_number_still_works_and_audits_the_fresh_sender_type(
    make_merchant, make_own_account, make_location, fernet_key
):
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    own = make_own_account(owner.merchant, token=OWN_TOKEN)

    with tenant_context(owner.merchant_id):
        mapping = services.set_location_sender(location=location, account=own, actor=owner.user)

    assert mapping.whatsapp_account_id == own.id
    (event,) = audit(owner.merchant, "whatsapp.sender_changed")
    assert event.metadata_json == {"from_sender_type": None, "to_sender_type": "OWN_NUMBER"}


def test_a_repeat_disconnect_of_an_already_suspended_number_is_idempotent_even_if_it_is_mapped(
    make_merchant, make_own_account, make_location, fernet_key
):
    """The state a past race could leave: a mapping to a SUSPENDED, token-less
    number. A repeat disconnect answers the same way every time (no 409, no
    second audit row) instead of getting stuck behind the mapping check."""
    owner = make_merchant("A")
    location = make_location(owner.merchant)
    account = make_own_account(owner.merchant, token=OWN_TOKEN)
    with tenant_context(owner.merchant_id):
        services.set_location_sender(location=location, account=account, actor=owner.user)
    with tenant_context(owner.merchant_id), tenant_atomic():
        WhatsAppAccount.objects.filter(pk=account.pk).update(status="SUSPENDED", access_token_enc=None)

    with tenant_context(owner.merchant_id):
        services.disconnect_own_number(account=account, actor=owner.user)  # no SenderInUse

    assert audit(owner.merchant, "whatsapp.own_number_disconnected") == []
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert WhatsAppLocationMapping.objects.count() == 1  # still never unmapped implicitly


def test_the_api_repeat_disconnect_of_a_mapped_suspended_number_is_204(api, make_location):
    owner, account, client = api
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id):
        services.set_location_sender(location=location, account=account, actor=owner.user)
    with tenant_context(owner.merchant_id), tenant_atomic():
        WhatsAppAccount.objects.filter(pk=account.pk).update(status="SUSPENDED", access_token_enc=None)

    assert delete(client, account).status_code == 204


def test_a_mapped_active_number_is_still_refused_with_409(api, make_location):
    owner, account, client = api
    location = make_location(owner.merchant)
    with tenant_context(owner.merchant_id):
        services.set_location_sender(location=location, account=account, actor=owner.user)

    assert delete(client, account).status_code == 409


@pytest.mark.django_db(transaction=True)
def test_a_concurrent_map_and_disconnect_never_leave_a_mapping_to_a_suspended_number(
    make_merchant, make_own_account, make_location, fernet_key
):
    """Exactly one of the two wins every round, and the end state is always
    consistent: mapped and still ACTIVE with its token, or unmapped and
    SUSPENDED. Never mapped to a suspended number."""
    import threading

    from django.core.exceptions import ValidationError
    from django.db import connection

    owner = make_merchant("A")
    merchant_id, user = owner.merchant_id, owner.user

    outcomes = []
    for _ in range(12):
        location = make_location(owner.merchant, name="Race location")
        account = make_own_account(owner.merchant, token=OWN_TOKEN)
        result = {}
        barrier = threading.Barrier(2)

        def do_map():
            try:
                barrier.wait(timeout=10)
                with tenant_context(merchant_id):
                    services.set_location_sender(location=location, account=account, actor=user)
                result["map"] = "ok"
            except ValidationError:
                result["map"] = "refused"
            except BaseException as exc:  # surfaced below
                result["map"] = repr(exc)
            finally:
                connection.close()

        def do_disconnect():
            try:
                barrier.wait(timeout=10)
                with tenant_context(merchant_id):
                    services.disconnect_own_number(account=account, actor=user)
                result["disconnect"] = "ok"
            except SenderInUse:
                result["disconnect"] = "in_use"
            except BaseException as exc:
                result["disconnect"] = repr(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=do_map), threading.Thread(target=do_disconnect)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert result["map"] in ("ok", "refused"), result
        assert result["disconnect"] in ("ok", "in_use"), result
        with tenant_context(merchant_id), tenant_atomic():
            mapped = WhatsAppLocationMapping.objects.filter(whatsapp_account_id=account.pk).exists()
            row = WhatsAppAccount.objects.get(pk=account.pk)
        if mapped:
            assert (result["map"], result["disconnect"]) == ("ok", "in_use"), result
            assert row.status == "ACTIVE" and row.access_token_enc
        else:
            assert (result["map"], result["disconnect"]) == ("refused", "ok"), result
            assert row.status == "SUSPENDED" and row.access_token_enc is None
        outcomes.append((result["map"], result["disconnect"]))

    assert len(outcomes) == 12


# --- quality-fix pass: the disconnect throttle ------------------------------------------------------


def test_the_disconnect_endpoint_is_throttled_per_user(api, throttle_rates):
    throttle_rates(whatsapp_sender_write="2/min")
    owner, account, client = api

    first = delete(client, account)
    second = delete(client, account)
    third = delete(client, account)

    assert (first.status_code, second.status_code) == (204, 204)
    assert third.status_code == 429


def test_the_disconnect_throttle_does_not_limit_reading_senders(api, throttle_rates):
    throttle_rates(whatsapp_sender_write="1/min")
    owner, account, client = api

    assert delete(client, account).status_code == 204
    for _ in range(3):
        assert client.get("/api/v1/whatsapp/senders").status_code == 200


def test_the_disconnect_throttle_is_per_user_not_shared(api, add_member, session_client, throttle_rates):
    throttle_rates(whatsapp_sender_write="1/min")
    owner, account, client = api
    add_member(owner.merchant, "ADMIN", "second-admin@example.com")
    other = session_client("second-admin@example.com")

    assert delete(client, account).status_code == 204
    assert delete(client, account).status_code == 429  # the first user is limited
    assert delete(other, account).status_code == 204  # another user is not
