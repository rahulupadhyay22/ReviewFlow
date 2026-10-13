"""Template validation by WABA scope and the own-number binding guard (spec
08-whatsapp-embedded-signup P4; Definition of done "Templates").

Own-number template submission and sync are NOT built: they are blocked on
WhatsApp-Meta.md E-7b. Until then, naming an own account is refused with 422,
so a request can never silently create a shared template instead."""
import pytest
from django.core.exceptions import ValidationError

from core.tenancy import tenant_atomic, tenant_context
from whatsapp import services
from whatsapp.models import MessageTemplate
from whatsapp.tests.conftest import load_fixture

pytestmark = pytest.mark.django_db

TEMPLATES = "/api/v1/whatsapp/templates"
WITH_NAME = "Hi {{customer_name}}, thanks for visiting {{business_name}}!"
WITHOUT_NAME = "Hi {{customer_name}}, thanks for your visit. Review: {{review_link}}"
GOOD = {"name": "Review ask", "language": "en", "body": WITH_NAME}


def field_errors(exc):
    return exc.value.message_dict


def template_count(merchant):
    with tenant_context(merchant.id), tenant_atomic():
        return MessageTemplate.objects.count()


# --- validate_template: {{business_name}} only for the shared pool --------------------------------


def test_a_shared_pool_template_without_business_name_is_still_rejected():
    with pytest.raises(ValidationError) as exc:
        services.validate_template(name="n", language="en", body=WITHOUT_NAME)
    assert "body" in field_errors(exc)

    with pytest.raises(ValidationError):
        services.validate_template(name="n", language="en", body=WITHOUT_NAME, whatsapp_account=None)


def test_an_own_number_template_is_not_required_to_include_business_name(make_merchant, make_own_account):
    merchant = make_merchant("A").merchant
    own = make_own_account(merchant)

    services.validate_template(name="n", language="en", body=WITHOUT_NAME, whatsapp_account=own)  # no raise


def test_an_own_number_template_may_still_include_business_name(make_merchant, make_own_account):
    merchant = make_merchant("A").merchant
    own = make_own_account(merchant)

    services.validate_template(name="n", language="en", body=WITH_NAME, whatsapp_account=own)  # no raise


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"body": "Hi {{customer_name}} {{coupon_code}}"}, "body"),  # unknown placeholder
        ({"body": "Hi {{customer_name"}, "body"),  # malformed
        ({"body": ""}, "body"),
        ({"body": "x" * 1025}, "body"),  # over length
        ({"language": "fr"}, "language"),
        ({"name": ""}, "name"),
    ],
)
def test_every_other_rule_applies_to_own_number_templates_too(
    make_merchant, make_own_account, kwargs, field
):
    merchant = make_merchant("A").merchant
    own = make_own_account(merchant)
    payload = {"name": "n", "language": "en", "body": WITHOUT_NAME, **kwargs}

    with pytest.raises(ValidationError) as exc:
        services.validate_template(whatsapp_account=own, **payload)
    assert field in field_errors(exc)


# --- POST /whatsapp/templates ----------------------------------------------------------------------


@pytest.fixture
def owner_client(make_merchant, session_client, shared_account):
    owner = make_merchant("A")
    return owner, session_client(owner.user.email)


def post(client, payload):
    return client.post(TEMPLATES, payload, format="json", HTTP_X_CSRFTOKEN=client.csrf)


def test_naming_an_own_account_is_refused_with_422_and_creates_nothing(
    owner_client, make_own_account, meta_http
):
    owner, client = owner_client
    own = make_own_account(owner.merchant)

    resp = post(client, {**GOOD, "whatsapp_account_id": str(own.id)})

    assert resp.status_code == 422
    assert resp.json()["error"]["field_errors"] == {"whatsapp_account_id": ["not yet available"]}
    assert template_count(owner.merchant) == 0  # no silent shared template
    assert meta_http.requests == []  # nothing submitted to Meta


def test_naming_any_account_id_at_all_is_refused_not_dropped(owner_client):
    owner, client = owner_client

    resp = post(client, {**GOOD, "whatsapp_account_id": "00000000-0000-0000-0000-000000000001"})

    assert resp.status_code == 422
    assert template_count(owner.merchant) == 0


def test_the_refusal_applies_even_when_the_shared_pool_is_not_configured(
    make_merchant, session_client, make_own_account, meta_settings
):
    # No shared account here, so a plain POST would be 503. Naming an own
    # account is refused first, with the documented 422.
    owner = make_merchant("A")
    own = make_own_account(owner.merchant)
    client = session_client(owner.user.email)

    assert post(client, GOOD).status_code == 503
    assert post(client, {**GOOD, "whatsapp_account_id": str(own.id)}).status_code == 422


def test_a_malformed_account_id_is_a_validation_error(owner_client):
    _, client = owner_client

    resp = post(client, {**GOOD, "whatsapp_account_id": "not-a-uuid"})

    assert resp.status_code == 422
    assert "whatsapp_account_id" in resp.json()["error"]["field_errors"]


@pytest.mark.parametrize("extra", [{}, {"whatsapp_account_id": None}])
def test_without_an_account_the_shared_pool_path_is_unchanged(owner_client, meta_http, extra):
    owner, client = owner_client
    meta_http.on("/message_templates", load_fixture("template_create_response.json"))

    resp = post(client, {**GOOD, **extra})

    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"id", "name", "language", "body", "status", "created_at", "updated_at"}
    with tenant_context(owner.merchant.id), tenant_atomic():
        (template,) = MessageTemplate.objects.all()
    assert template.whatsapp_account_id is None


def test_a_shared_template_without_business_name_still_gets_422(owner_client):
    owner, client = owner_client

    resp = post(client, {**GOOD, "body": WITHOUT_NAME})

    assert resp.status_code == 422
    assert "body" in resp.json()["error"]["field_errors"]
