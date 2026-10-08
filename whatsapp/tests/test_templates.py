"""Templates (spec 08 Definition of done "Templates"): validate_template,
POST/GET /whatsapp/templates, submit_template, sync_template_statuses.
Only Meta's HTTP call is mocked (`meta_http`)."""
import pytest
from celery.exceptions import Retry
from django.core.exceptions import ValidationError
from django.db import connection

from core.tenancy import tenant_atomic, tenant_context
from whatsapp import services, tasks
from whatsapp.models import MessageTemplate
from whatsapp.providers.base import ProviderPermanentError, ProviderTransientError
from whatsapp.providers import meta_cloud
from whatsapp.providers.meta_cloud import MetaCloudProvider, provider_template_name
from whatsapp.tests.conftest import http_error, load_fixture

pytestmark = pytest.mark.django_db

TEMPLATES = "/api/v1/whatsapp/templates"
DOC_EXAMPLE = (
    "Hi {{customer_name}}, thanks for visiting {{business_name}}! "
    "We'd love to hear about your experience. [Review us on Google]"
)
GOOD = {"name": "Review ask", "language": "en", "body": DOC_EXAMPLE}


def post(client, payload=None):
    return client.post(TEMPLATES, payload or GOOD, format="json", HTTP_X_CSRFTOKEN=client.csrf)


def make_template(merchant, *, name="t", provider_template_id=None, status="PENDING", language="en"):
    with tenant_context(merchant.id), tenant_atomic():
        return MessageTemplate.objects.create(
            merchant_id=merchant.id,
            name=name,
            language=language,
            body=DOC_EXAMPLE,
            status=status,
            provider_template_id=provider_template_id,
        )


def reload(merchant, template):
    with tenant_context(merchant.id), tenant_atomic():
        return MessageTemplate.objects.get(pk=template.pk)


# --- validate_template -----------------------------------------------------------


def test_validate_template_accepts_the_architecture_doc_example():
    services.validate_template(name="Review ask", language="en", body=DOC_EXAMPLE)  # does not raise


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"body": "Hi {{customer_name}}, thanks for visiting {{business_name}} {{coupon_code}}"}, "body"),
        ({"body": "Hi {{customer_name}}, thanks for visiting us"}, "body"),
        ({"language": "fr"}, "language"),
        ({"body": "{{business_name}} " + "x" * 5000}, "body"),
    ],
    ids=["unknown-placeholder", "missing-business-name", "language-not-allowlisted", "over-length"],
)
def test_validate_template_rejects_invalid_input_with_a_field_error(kwargs, field):
    args = {"name": "Review ask", "language": "en", "body": DOC_EXAMPLE, **kwargs}

    with pytest.raises(ValidationError) as exc_info:
        services.validate_template(**args)

    assert field in exc_info.value.message_dict


@pytest.mark.parametrize("language", ["en", "en_US", "hi"])
def test_validate_template_accepts_each_allowlisted_language(language):
    services.validate_template(name="n", language=language, body=DOC_EXAMPLE)


# --- POST /whatsapp/templates ----------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_post_creates_a_pending_template_and_the_submit_task_stores_the_provider_id(
    make_merchant, session_client, shared_account, meta_http
):
    owner = make_merchant("A")
    meta_http.on("/message_templates", load_fixture("template_create_response.json"))

    resp = post(session_client(owner.user.email))

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "PENDING"
    assert {"id", "name", "language", "body", "created_at", "updated_at"} <= set(body)
    assert len(meta_http.requests) == 1
    assert shared_account.business_account_id in meta_http.urls()[0]
    assert meta_http.urls()[0].endswith("/message_templates")
    with tenant_context(owner.merchant_id), tenant_atomic():
        stored = MessageTemplate.objects.get(pk=body["id"])
    assert stored.provider_template_id == load_fixture("template_create_response.json")["id"]
    assert stored.status == "PENDING"  # approval is a separate Meta decision


def test_post_duplicate_name_and_language_answers_409(make_merchant, session_client, shared_account, meta_http):
    owner = make_merchant("A")
    meta_http.on("/message_templates", load_fixture("template_create_response.json"))
    client = session_client(owner.user.email)
    assert post(client).status_code == 201

    resp = post(client)

    assert resp.status_code == 409
    assert "error" in resp.json()


def test_post_same_name_in_another_language_is_allowed(make_merchant, session_client, shared_account, meta_http):
    owner = make_merchant("A")
    client = session_client(owner.user.email)
    assert post(client).status_code == 201
    assert post(client, {**GOOD, "language": "hi"}).status_code == 201


@pytest.mark.parametrize("role", ["VIEWER", "MANAGER"])
def test_viewer_and_manager_cannot_create_templates(
    make_merchant, add_member, session_client, shared_account, meta_http, role
):
    owner = make_merchant("A")
    add_member(owner.merchant, role, "member@example.com")

    resp = post(session_client("member@example.com"))

    assert resp.status_code == 403
    assert meta_http.requests == []
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not MessageTemplate.objects.exists()


def test_admin_can_create_templates(make_merchant, add_member, session_client, shared_account, meta_http):
    owner = make_merchant("A")
    add_member(owner.merchant, "ADMIN", "admin@example.com")
    assert post(session_client("admin@example.com")).status_code == 201


def test_post_without_an_active_shared_account_answers_503_and_creates_nothing(
    make_merchant, session_client, meta_http
):
    owner = make_merchant("A")  # no shared account exists

    resp = post(session_client(owner.user.email))

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "whatsapp_not_configured"
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not MessageTemplate.objects.exists()


def test_post_with_a_suspended_shared_account_answers_503(
    make_merchant, session_client, make_shared_account, meta_http
):
    make_shared_account("SUSPENDED")
    owner = make_merchant("A")

    resp = post(session_client(owner.user.email))

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "whatsapp_not_configured"


def test_post_with_the_platform_access_token_unset_answers_503_and_creates_nothing(
    make_merchant, session_client, shared_account, meta_http, settings
):
    settings.META_SHARED_POOL_ACCESS_TOKEN = ""
    owner = make_merchant("A")

    resp = post(session_client(owner.user.email))

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "whatsapp_not_configured"
    assert meta_http.requests == []
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not MessageTemplate.objects.exists()


def test_post_invalid_body_answers_422_with_field_errors_and_creates_nothing(
    make_merchant, session_client, shared_account, meta_http
):
    owner = make_merchant("A")

    resp = post(session_client(owner.user.email), {**GOOD, "body": "Hello {{who}}"})

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert "body" in error["field_errors"]
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not MessageTemplate.objects.exists()


def test_post_requires_csrf_and_authentication(make_merchant, session_client, shared_account, meta_http):
    from rest_framework.test import APIClient

    owner = make_merchant("A")
    client = session_client(owner.user.email)

    assert client.post(TEMPLATES, GOOD, format="json").status_code == 403
    assert APIClient().post(TEMPLATES, GOOD, format="json").status_code == 403


# --- GET /whatsapp/templates (tenant isolation, application layer) ---------------


def test_get_lists_only_own_templates_and_filters_by_status(make_merchant, session_client):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    pending = make_template(owner_a.merchant, name="p")
    approved = make_template(owner_a.merchant, name="a", status="APPROVED")
    foreign = make_template(owner_b.merchant, name="b")
    client = session_client(owner_a.user.email)

    everything = client.get(TEMPLATES)
    only_approved = client.get(TEMPLATES, {"status": "APPROVED"})

    assert everything.status_code == 200
    text = everything.content.decode()
    assert str(pending.id) in text and str(approved.id) in text
    assert str(foreign.id) not in text
    assert str(approved.id) in only_approved.content.decode()
    assert str(pending.id) not in only_approved.content.decode()


def test_any_role_can_list_templates(make_merchant, add_member, session_client):
    owner = make_merchant("A")
    add_member(owner.merchant, "VIEWER", "viewer@example.com")
    assert session_client("viewer@example.com").get(TEMPLATES).status_code == 200


# --- submit_template_to_provider ---------------------------------------------------


def test_submitting_the_same_template_twice_calls_meta_once(make_merchant, shared_account, meta_http):
    owner = make_merchant("A")
    template = make_template(owner.merchant)
    meta_http.on("/message_templates", load_fixture("template_create_response.json"))

    with tenant_context(owner.merchant_id):
        services.submit_template_to_provider(template.id)
        services.submit_template_to_provider(template.id)

    assert len(meta_http.requests) == 1
    assert reload(owner.merchant, template).provider_template_id == "900000000000001"


def test_a_permanent_provider_error_marks_the_template_rejected(make_merchant, shared_account, meta_http):
    owner = make_merchant("A")
    template = make_template(owner.merchant)
    # The submit is refused (400); the recovery lookup then completes and the
    # template is genuinely not on Meta: only that combination may REJECT.
    meta_http.queue.extend([http_error(400), {"data": []}])

    with tenant_context(owner.merchant_id):
        services.submit_template_to_provider(template.id)

    stored = reload(owner.merchant, template)
    assert stored.status == "REJECTED"
    assert stored.provider_template_id is None


def test_a_transient_provider_error_propagates_and_changes_nothing(make_merchant, shared_account, meta_http):
    owner = make_merchant("A")
    template = make_template(owner.merchant)
    meta_http.on("/message_templates", http_error(503))

    with tenant_context(owner.merchant_id):
        with pytest.raises(ProviderTransientError):
            services.submit_template_to_provider(template.id)

    stored = reload(owner.merchant, template)
    assert (stored.status, stored.provider_template_id) == ("PENDING", None)


def test_the_submit_task_retries_a_transient_error_and_leaves_the_template_pending(
    make_merchant, shared_account, meta_http
):
    owner = make_merchant("A")
    template = make_template(owner.merchant)
    meta_http.on("/message_templates", http_error(503))

    # In Celery eager mode a task that autoretries surfaces celery's Retry
    # (carrying the ProviderTransientError), not the bare provider error.
    with pytest.raises(Retry) as retry:
        tasks.submit_template.delay(owner.merchant_id, template.id)
    assert isinstance(retry.value.exc, ProviderTransientError)

    # Eager mode runs the first attempt and surfaces Retry; it does not re-run
    # the task. The retry semantics are the task's configuration, asserted here.
    assert len(meta_http.requests) == 1
    assert tasks.submit_template.max_retries == 5
    assert ProviderTransientError in tasks.submit_template.autoretry_for
    stored = reload(owner.merchant, template)
    assert (stored.status, stored.provider_template_id) == ("PENDING", None)


def test_submit_is_a_no_op_for_a_template_that_is_no_longer_pending(make_merchant, shared_account, meta_http):
    owner = make_merchant("A")
    template = make_template(owner.merchant, status="APPROVED")

    with tenant_context(owner.merchant_id):
        services.submit_template_to_provider(template.id)

    assert meta_http.requests == []


# --- sync_template_statuses ----------------------------------------------------------


def test_sync_moves_pending_to_approved_per_the_fixture_and_a_second_run_is_a_no_op(
    make_merchant, shared_account, meta_http
):
    owner = make_merchant("A")
    template = make_template(owner.merchant, provider_template_id="900000000000001")
    meta_http.on("900000000000001", load_fixture("template_status_approved.json"))

    with tenant_context(owner.merchant_id):
        first = services.sync_template_statuses()
        second = services.sync_template_statuses()

    assert (first, second) == (1, 0)
    assert reload(owner.merchant, template).status == "APPROVED"


def test_sync_moves_pending_to_rejected_per_the_fixture(make_merchant, shared_account, meta_http):
    owner = make_merchant("A")
    template = make_template(owner.merchant, provider_template_id="900000000000001")
    meta_http.on("900000000000001", load_fixture("template_status_rejected.json"))

    with tenant_context(owner.merchant_id):
        assert services.sync_template_statuses() == 1

    assert reload(owner.merchant, template).status == "REJECTED"


def test_sync_leaves_a_still_pending_template_pending(make_merchant, shared_account, meta_http):
    owner = make_merchant("A")
    template = make_template(owner.merchant, provider_template_id="900000000000001")
    meta_http.on("900000000000001", load_fixture("template_status_pending.json"))

    with tenant_context(owner.merchant_id):
        assert services.sync_template_statuses() == 0

    assert reload(owner.merchant, template).status == "PENDING"


@pytest.mark.parametrize("meta_fixture", ["template_status_rejected.json", "template_status_pending.json"])
def test_sync_never_moves_an_approved_template_back(make_merchant, shared_account, meta_http, meta_fixture):
    owner = make_merchant("A")
    template = make_template(owner.merchant, provider_template_id="900000000000001", status="APPROVED")
    meta_http.on("900000000000001", load_fixture(meta_fixture))

    with tenant_context(owner.merchant_id):
        assert services.sync_template_statuses() == 0

    assert reload(owner.merchant, template).status == "APPROVED"


def test_sync_ignores_templates_that_were_never_submitted(make_merchant, shared_account, meta_http):
    owner = make_merchant("A")
    make_template(owner.merchant, provider_template_id=None)

    with tenant_context(owner.merchant_id):
        assert services.sync_template_statuses() == 0
        assert services.templates_pending_sync() is False

    assert meta_http.requests == []


def test_templates_pending_sync_is_true_only_for_a_submitted_pending_template(make_merchant):
    owner = make_merchant("A")
    make_template(owner.merchant, name="done", provider_template_id="P-9", status="APPROVED")
    with tenant_context(owner.merchant_id):
        assert services.templates_pending_sync() is False
    make_template(owner.merchant, name="waiting", provider_template_id="P-10")
    with tenant_context(owner.merchant_id):
        assert services.templates_pending_sync() is True


def test_sync_merchant_templates_task_never_touches_another_merchants_templates(
    make_merchant, shared_account, meta_http
):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    template_a = make_template(owner_a.merchant, provider_template_id="900000000000001")
    template_b = make_template(owner_b.merchant, provider_template_id="900000000000002")
    meta_http.on("900000000000001", load_fixture("template_status_approved.json"))
    meta_http.on("900000000000002", load_fixture("template_status_approved.json"))

    tasks.sync_merchant_templates(merchant_id=owner_a.merchant_id)

    assert reload(owner_a.merchant, template_a).status == "APPROVED"
    assert reload(owner_b.merchant, template_b).status == "PENDING"
    assert not any("900000000000002" in url for url in meta_http.urls())


def test_the_stored_template_row_is_invisible_to_another_merchants_raw_sql(make_merchant):
    owner_a, owner_b = make_merchant("A"), make_merchant("B")
    template = make_template(owner_b.merchant)

    with tenant_context(owner_a.merchant_id), tenant_atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT id FROM whatsapp_messagetemplate WHERE id = %s", [template.id])
        assert cursor.fetchall() == []


# --- recovery after a permanent refusal (spec 08 code review) ----------------------------------------
#
# A permanent refusal may only mean "Meta already has it" (an earlier attempt was
# accepted but never recorded). REJECTED is terminal, so only a COMPLETED lookup
# that finds nothing may conclude it; every inconclusive or failed lookup leaves
# the template PENDING.


def listing(template, *, language="en", template_id="TEST-TPL-RECOVERED"):
    return {"data": [{"id": template_id, "name": provider_template_name(template), "language": language}]}


def submit(owner, template):
    with tenant_context(owner.merchant_id):
        services.submit_template_to_provider(template.id)


def test_a_refused_submit_whose_template_is_already_on_meta_records_its_id_and_stays_pending(
    make_merchant, shared_account, meta_http
):
    owner = make_merchant("A")
    template = make_template(owner.merchant)
    meta_http.queue.extend([http_error(400), listing(template)])

    submit(owner, template)

    stored = reload(owner.merchant, template)
    assert (stored.status, stored.provider_template_id) == ("PENDING", "TEST-TPL-RECOVERED")
    assert len(meta_http.requests) == 2  # the refused POST, then the lookup


def test_a_refused_submit_found_only_in_another_language_is_genuinely_not_found(
    make_merchant, shared_account, meta_http
):
    owner = make_merchant("A")
    template = make_template(owner.merchant, language="en")
    meta_http.queue.extend([http_error(400), listing(template, language="hi")])

    submit(owner, template)

    stored = reload(owner.merchant, template)
    assert (stored.status, stored.provider_template_id) == ("REJECTED", None)


def test_a_transient_lookup_error_propagates_and_leaves_the_template_pending(
    make_merchant, shared_account, meta_http
):
    owner = make_merchant("A")
    template = make_template(owner.merchant)
    meta_http.queue.extend([http_error(400), http_error(503)])

    with pytest.raises(ProviderTransientError):
        submit(owner, template)

    stored = reload(owner.merchant, template)
    assert (stored.status, stored.provider_template_id) == ("PENDING", None)


def test_a_permanent_lookup_error_propagates_and_leaves_the_template_pending(
    make_merchant, shared_account, meta_http
):
    owner = make_merchant("A")
    template = make_template(owner.merchant)
    meta_http.queue.extend([http_error(400), http_error(400)])

    with pytest.raises(ProviderPermanentError):
        submit(owner, template)

    stored = reload(owner.merchant, template)
    assert (stored.status, stored.provider_template_id) == ("PENDING", None)


def test_an_inconclusive_lookup_at_the_page_cap_leaves_the_template_pending(
    make_merchant, shared_account, meta_http, monkeypatch
):
    monkeypatch.setattr(meta_cloud, "_FIND_MAX_PAGES", 1)
    owner = make_merchant("A")
    template = make_template(owner.merchant)
    meta_http.queue.extend([http_error(400), load_fixture("template_list_page1.json")])  # has a next page

    with pytest.raises(ProviderTransientError):
        submit(owner, template)

    stored = reload(owner.merchant, template)
    assert (stored.status, stored.provider_template_id) == ("PENDING", None)


@pytest.mark.django_db(transaction=True)
def test_the_submit_task_retries_an_inconclusive_lookup_and_the_template_stays_pending(
    make_merchant, shared_account, meta_http, monkeypatch
):
    monkeypatch.setattr(meta_cloud, "_FIND_MAX_PAGES", 1)
    owner = make_merchant("A")
    template = make_template(owner.merchant)
    meta_http.queue.extend([http_error(400), load_fixture("template_list_page1.json")])

    with pytest.raises(Retry):  # eager mode surfaces the autoretry as Retry
        tasks.submit_template.delay(owner.merchant_id, template.id)

    stored = reload(owner.merchant, template)
    assert (stored.status, stored.provider_template_id) == ("PENDING", None)


def test_when_another_attempt_recorded_the_template_meanwhile_the_submit_is_a_no_op(
    make_merchant, shared_account, meta_http, monkeypatch
):
    owner = make_merchant("A")
    template = make_template(owner.merchant)

    def racing_submit(self, account, tpl):
        # While this attempt is at Meta, a concurrent attempt records its own id.
        with tenant_context(owner.merchant_id), tenant_atomic():
            MessageTemplate.objects.filter(pk=tpl.pk).update(provider_template_id="TEST-TPL-OTHER-ATTEMPT")
        return "TEST-TPL-MINE"

    monkeypatch.setattr(MetaCloudProvider, "submit_template", racing_submit)

    submit(owner, template)

    stored = reload(owner.merchant, template)
    assert (stored.status, stored.provider_template_id) == ("PENDING", "TEST-TPL-OTHER-ATTEMPT")


# --- list_templates ----------------------------------------------------------------------------------


def test_list_templates_ignores_an_unknown_status_and_returns_the_unfiltered_list(make_merchant, session_client):
    owner = make_merchant("A")
    pending = make_template(owner.merchant, name="p")
    approved = make_template(owner.merchant, name="a", status="APPROVED")
    foreign = make_template(make_merchant("B").merchant, name="b")

    with tenant_context(owner.merchant_id), tenant_atomic():
        service_ids = {t.id for t in services.list_templates("BOGUS")}
        none_ids = {t.id for t in services.list_templates(None)}
        approved_ids = {t.id for t in services.list_templates("APPROVED")}
    assert service_ids == none_ids == {pending.id, approved.id}
    assert approved_ids == {approved.id}

    resp = session_client(owner.user.email).get(TEMPLATES, {"status": "BOGUS"})
    assert resp.status_code == 200
    body = resp.content.decode()
    assert str(pending.id) in body and str(approved.id) in body
    assert str(foreign.id) not in body


# --- POST /whatsapp/templates throttle -----------------------------------------------------------------


def test_template_creation_is_throttled_per_user_on_post_only(
    make_merchant, session_client, shared_account, meta_http, throttle_rates
):
    throttle_rates(whatsapp_template_write="1/min")
    owner, other = make_merchant("A"), make_merchant("B")
    client = session_client(owner.user.email)

    first = post(client, {**GOOD, "name": "First"})
    second = post(client, {**GOOD, "name": "Second"})
    third = post(client, {**GOOD, "name": "Third"})

    assert first.status_code == 201
    assert second.status_code == 429
    assert third.status_code == 429
    assert client.get(TEMPLATES).status_code == 200  # reads are not throttled
    # Per user: another merchant's owner is not affected by this one's quota.
    assert post(session_client(other.user.email), {**GOOD, "name": "Other"}).status_code == 201
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert sorted(MessageTemplate.objects.values_list("name", flat=True)) == ["First"]
