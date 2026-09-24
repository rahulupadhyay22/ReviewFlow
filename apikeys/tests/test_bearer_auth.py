"""Public-API Bearer auth on GET /merchant, the Phase-05 compatibility
consumer (spec .claude/specs/05-public-api-keys.md §"API endpoints",
Definition of done §"Public-API auth"). Testing-Strategy.md's tenant
isolation priority scenario is covered here (Bearer key vs. session)."""
import pytest
from django.db import DatabaseError
from django.test import RequestFactory
from django.urls import get_resolver
from django.utils.module_loading import import_string
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.test import APIClient
from rest_framework.views import APIView

from accounts.models import Merchant, TeamMember
from apikeys.authentication import ApiKeyAuthentication
from apikeys.models import ApiKey
from apikeys.permissions import HasApiKeyScope
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

MERCHANT = "/api/v1/merchant"
LOCATIONS = "/api/v1/locations"
API_KEYS = "/api/v1/api-keys"
LOGIN = "/api/v1/auth/login"


def _owner(merchant, add_member, email):
    return add_member(merchant, TeamMember.Role.OWNER, email)


def _bearer(raw):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


# --- Basic auth success/shape -------------------------------------------


def test_valid_key_returns_200_with_that_keys_merchant_no_session_no_csrf(merchant_a, add_member, make_key):
    owner = _owner(merchant_a, add_member, "bearer-basic@example.com")
    key, raw = make_key(owner)
    client = APIClient()
    resp = client.get(MERCHANT, **_bearer(raw))
    assert resp.status_code == 200
    assert resp.json()["id"] == str(merchant_a.id)


@pytest.mark.parametrize(
    "bad_header",
    [
        "Bearer malformed-token",
        "Bearer ",  # empty Bearer value
        "Bearer",  # scheme with no value at all
        "Bearer rf_live_" + "z" * 32,  # unknown key, valid shape
    ],
)
def test_bad_keys_return_identical_401_body_and_www_authenticate(merchant_a, add_member, make_key, bad_header):
    owner = _owner(merchant_a, add_member, "bearer-bad@example.com")
    key, raw = make_key(owner)
    client = APIClient()
    resp = client.get(MERCHANT, HTTP_AUTHORIZATION=bad_header)
    assert resp.status_code == 401
    assert resp["WWW-Authenticate"] == "Bearer"
    body = resp.json()
    assert body["error"]["code"] == "invalid_api_key"


def test_revoked_key_and_unknown_key_return_the_same_body(merchant_a, add_member, make_key):
    from apikeys import services

    owner = _owner(merchant_a, add_member, "bearer-samebody@example.com")
    key, raw = make_key(owner)
    with tenant_context(merchant_a.id):
        services.revoke_api_key(actor=owner, api_key=key)
    client = APIClient()
    revoked_resp = client.get(MERCHANT, **_bearer(raw))
    unknown_resp = client.get(MERCHANT, **_bearer("rf_live_" + "y" * 32))
    assert revoked_resp.status_code == unknown_resp.status_code == 401
    assert revoked_resp.json() == unknown_resp.json()


def test_no_authorization_header_and_no_session_returns_403(merchant_a):
    client = APIClient()
    resp = client.get(MERCHANT)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"]
    # D-B only affects API-key failures; session failures carry no challenge.
    assert "WWW-Authenticate" not in resp


def test_revoked_key_returns_401_on_the_very_next_request(merchant_a, add_member, make_key, session_client):
    owner = _owner(merchant_a, add_member, "bearer-revoke-next@example.com")
    key, raw = make_key(owner)
    client = APIClient()
    assert client.get(MERCHANT, **_bearer(raw)).status_code == 200

    owner_client = session_client(owner.user.email)
    csrf = owner_client.csrf
    resp = owner_client.delete(f"{API_KEYS}/{key.id}", HTTP_X_CSRFTOKEN=csrf)
    assert resp.status_code == 204

    assert client.get(MERCHANT, **_bearer(raw)).status_code == 401


def test_key_of_suspended_merchant_returns_401(merchant_a, add_member, make_key):
    owner = _owner(merchant_a, add_member, "bearer-suspended@example.com")
    key, raw = make_key(owner)
    with tenant_context(merchant_a.id), tenant_atomic():
        Merchant.objects.filter(id=merchant_a.id).update(status=Merchant.Status.SUSPENDED)
    client = APIClient()
    assert client.get(MERCHANT, **_bearer(raw)).status_code == 401


# --- Cross-tenant isolation (Testing-Strategy priority scenario) --------


def test_merchant_as_key_returns_only_merchant_a(merchant_a, merchant_b, add_member, make_key):
    owner_a = _owner(merchant_a, add_member, "cross-a@example.com")
    key_a, raw_a = make_key(owner_a)
    client = APIClient()
    resp = client.get(MERCHANT, **_bearer(raw_a))
    assert resp.json()["id"] == str(merchant_a.id)


def test_keys_bearer_wins_over_a_logged_in_session_for_a_different_merchant(
    merchant_a, merchant_b, add_member, make_key, session_client
):
    owner_a = _owner(merchant_a, add_member, "cross-bearer-a@example.com")
    owner_b = _owner(merchant_b, add_member, "cross-session-b@example.com")
    key_a, raw_a = make_key(owner_a)

    client = session_client(owner_b.user.email)
    resp = client.get(MERCHANT, **_bearer(raw_a))
    assert resp.status_code == 200
    assert resp.json()["id"] == str(merchant_a.id)


def test_bad_bearer_key_with_valid_session_returns_401_no_fallback(merchant_a, add_member, session_client):
    owner = _owner(merchant_a, add_member, "cross-badkey-session@example.com")
    client = session_client(owner.user.email)
    resp = client.get(MERCHANT, **_bearer("not-a-real-key"))
    assert resp.status_code == 401


# --- Session behavior is unchanged --------------------------------------


def test_patch_merchant_with_valid_key_and_no_session_returns_403_and_changes_nothing(
    merchant_a, add_member, make_key
):
    owner = _owner(merchant_a, add_member, "patch-key-only@example.com")
    key, raw = make_key(owner)
    original_name = merchant_a.name
    client = APIClient()
    resp = client.patch(MERCHANT, {"name": "Hijacked"}, format="json", **_bearer(raw))
    assert resp.status_code == 403
    with tenant_context(merchant_a.id), tenant_atomic():
        assert Merchant.objects.get(id=merchant_a.id).name == original_name


def test_session_get_and_patch_merchant_are_unchanged(merchant_a, add_member, session_client):
    owner = _owner(merchant_a, add_member, "session-unchanged@example.com")
    client = session_client(owner.user.email)
    assert client.get(MERCHANT).status_code == 200
    resp = client.patch(MERCHANT, {"name": "Renamed"}, format="json", HTTP_X_CSRFTOKEN=client.csrf)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"


def test_bearer_header_on_dashboard_only_endpoint_returns_403_and_does_not_touch_key(
    merchant_a, add_member, make_key
):
    owner = _owner(merchant_a, add_member, "dashboard-only@example.com")
    key, raw = make_key(owner)
    client = APIClient()
    resp = client.get(LOCATIONS, **_bearer(raw))
    assert resp.status_code == 403
    with tenant_context(merchant_a.id), tenant_atomic():
        assert ApiKey.objects.get(id=key.id).last_used_at is None


def test_bearer_header_on_api_keys_endpoint_returns_403(merchant_a, add_member, make_key):
    owner = _owner(merchant_a, add_member, "apikeys-endpoint@example.com")
    key, raw = make_key(owner)
    client = APIClient()
    resp = client.get(API_KEYS, **_bearer(raw))
    assert resp.status_code == 403


def test_valid_bearer_plus_valid_session_on_session_only_endpoint_uses_session_merchant(
    merchant_a, merchant_b, add_member, make_key, session_client
):
    owner_a = _owner(merchant_a, add_member, "sess-only-a@example.com")
    owner_b = _owner(merchant_b, add_member, "sess-only-b@example.com")
    key_a, raw_a = make_key(owner_a)

    client = session_client(owner_b.user.email)
    resp = client.get(LOCATIONS, **_bearer(raw_a))
    assert resp.status_code == 200
    # The session's merchant (B) served the request, not the key's (A):
    # every location returned belongs to merchant B.
    with tenant_context(merchant_b.id), tenant_atomic():
        from locations.models import Location

        b_ids = {str(pk) for pk in Location.objects.values_list("id", flat=True)}
    for loc in resp.json()["results"]:
        assert loc["id"] in b_ids


def test_bearer_header_on_login_does_not_break_login(merchant_a):
    client = APIClient(enforce_csrf_checks=True)
    client.get(LOGIN)
    token = client.cookies["csrftoken"].value
    resp = client.post(
        LOGIN,
        {"email": "nobody@example.com", "password": "wrong"},
        format="json",
        HTTP_X_CSRFTOKEN=token,
        HTTP_AUTHORIZATION="Bearer rf_live_" + "q" * 32,
    )
    assert resp.status_code == 401
    assert resp.status_code != 500


def test_bearer_header_on_successful_login_still_logs_in(merchant_a, add_member, make_key):
    """The full pre-tenant login path (user_lookup_atomic) runs untouched."""
    owner = _owner(merchant_a, add_member, "login-with-bearer@example.com")
    _, raw = make_key(owner)
    client = APIClient(enforce_csrf_checks=True)
    client.get(LOGIN)
    token = client.cookies["csrftoken"].value
    resp = client.post(
        LOGIN,
        {"email": owner.user.email, "password": "Tr1cky-Horse-Battery-Staple!"},
        format="json",
        HTTP_X_CSRFTOKEN=token,
        **_bearer(raw),
    )
    assert resp.status_code == 200


def test_head_merchant_with_bearer_key_is_not_api_key_authenticated(merchant_a, add_member, make_key):
    owner = _owner(merchant_a, add_member, "head-method@example.com")
    key, raw = make_key(owner)
    client = APIClient()
    resp = client.head(MERCHANT, **_bearer(raw))
    # HEAD is not in api_key_methods, so the request is judged by session
    # alone; with no session it gets the existing no-credential response.
    assert resp.status_code == 403
    with tenant_context(merchant_a.id), tenant_atomic():
        assert ApiKey.objects.get(id=key.id).last_used_at is None


# --- Opt-in registry ------------------------------------------------------


def _iter_view_method_pairs(patterns, prefix=""):
    for p in patterns:
        if hasattr(p, "url_patterns"):
            yield from _iter_view_method_pairs(p.url_patterns, prefix)
            continue
        cls = getattr(p.callback, "cls", None)
        if cls is None:
            continue
        methods = getattr(cls, "api_key_methods", frozenset())
        for m in methods:
            yield (cls, m)


def test_opt_in_registry_is_exactly_merchantview_get():
    from accounts.views import MerchantView

    pairs = set(_iter_view_method_pairs(get_resolver().url_patterns))
    assert pairs == {(MerchantView, "GET")}


def test_api_key_authentication_is_not_a_default_authentication_class():
    from django.conf import settings

    classes = settings.REST_FRAMEWORK.get("DEFAULT_AUTHENTICATION_CLASSES", [])
    for path in classes:
        cls = import_string(path) if isinstance(path, str) else path
        assert cls.__name__ != "ApiKeyAuthentication"


def test_api_key_authentication_without_the_mixin_is_inert(monkeypatch):
    """Listing ApiKeyAuthentication without ApiKeyOptInMixin has no effect:
    it only reads middleware markers, which are set only for views that
    declare api_key_methods. No lookup ever runs."""
    from apikeys import services

    monkeypatch.setattr(
        services, "authenticate_api_key", lambda raw: pytest.fail("lookup must not run")
    )

    class _BareView(APIView):
        authentication_classes = [ApiKeyAuthentication]
        permission_classes = [AllowAny]

        def get(self, request):
            return Response({"auth": str(request.auth)})

    request = RequestFactory().get("/", HTTP_AUTHORIZATION="Bearer rf_live_" + "a" * 32)
    response = _BareView.as_view()(request)
    assert response.status_code == 200
    assert response.data == {"auth": "None"}


def test_unresolvable_path_with_bearer_passes_through_to_404():
    resp = APIClient().get("/api/v1/does-not-exist", **_bearer("rf_live_" + "a" * 32))
    assert resp.status_code == 404


# --- last_used_at ----------------------------------------------------


def test_last_used_at_is_set_after_a_successful_key_request(merchant_a, add_member, make_key):
    owner = _owner(merchant_a, add_member, "touch-success@example.com")
    key, raw = make_key(owner)
    client = APIClient()
    assert client.get(MERCHANT, **_bearer(raw)).status_code == 200
    with tenant_context(merchant_a.id), tenant_atomic():
        assert ApiKey.objects.get(id=key.id).last_used_at is not None


def test_best_effort_touch_failure_does_not_fail_a_valid_key_request(
    merchant_a, add_member, make_key, monkeypatch
):
    owner = _owner(merchant_a, add_member, "touch-fail@example.com")
    key, raw = make_key(owner)

    from django.db.models.query import QuerySet

    original_update = QuerySet.update

    def failing_update(self, **kwargs):
        if self.model is ApiKey and "last_used_at" in kwargs:
            raise DatabaseError("simulated touch failure")
        return original_update(self, **kwargs)

    monkeypatch.setattr(QuerySet, "update", failing_update)

    client = APIClient()
    resp = client.get(MERCHANT, **_bearer(raw))
    assert resp.status_code == 200
    assert resp.json()["id"] == str(merchant_a.id)


def test_best_effort_touch_failure_does_not_make_an_invalid_key_succeed(
    merchant_a, add_member, make_key, monkeypatch
):
    owner = _owner(merchant_a, add_member, "touch-fail-invalid@example.com")
    key, raw = make_key(owner)
    from apikeys import services

    with tenant_context(merchant_a.id):
        services.revoke_api_key(actor=owner, api_key=key)

    from django.db.models.query import QuerySet

    original_update = QuerySet.update

    def failing_update(self, **kwargs):
        if self.model is ApiKey and "last_used_at" in kwargs:
            raise DatabaseError("simulated touch failure")
        return original_update(self, **kwargs)

    monkeypatch.setattr(QuerySet, "update", failing_update)

    client = APIClient()
    resp = client.get(MERCHANT, **_bearer(raw))
    assert resp.status_code == 401


# --- Point-in-time checks -------------------------------------------------


def test_in_flight_request_is_not_cancelled_by_mid_request_suspension(merchant_a, add_member, make_key, monkeypatch):
    owner = _owner(merchant_a, add_member, "pit-suspend@example.com")
    key, raw = make_key(owner)

    from accounts.views import MerchantView

    original_get = MerchantView.get

    def suspend_then_get(self, request):
        with tenant_context(merchant_a.id), tenant_atomic():
            Merchant.objects.filter(id=merchant_a.id).update(status=Merchant.Status.SUSPENDED)
        return original_get(self, request)

    monkeypatch.setattr(MerchantView, "get", suspend_then_get)

    client = APIClient()
    first = client.get(MERCHANT, **_bearer(raw))
    assert first.status_code == 200

    monkeypatch.setattr(MerchantView, "get", original_get)
    second = client.get(MERCHANT, **_bearer(raw))
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "invalid_api_key"


def test_in_flight_request_is_not_cancelled_by_mid_request_revocation(merchant_a, add_member, make_key, monkeypatch):
    owner = _owner(merchant_a, add_member, "pit-revoke@example.com")
    key, raw = make_key(owner)

    from accounts.views import MerchantView
    from apikeys import services

    original_get = MerchantView.get

    def revoke_then_get(self, request):
        with tenant_context(merchant_a.id):
            services.revoke_api_key(actor=owner, api_key=key)
        return original_get(self, request)

    monkeypatch.setattr(MerchantView, "get", revoke_then_get)

    client = APIClient()
    first = client.get(MERCHANT, **_bearer(raw))
    assert first.status_code == 200

    monkeypatch.setattr(MerchantView, "get", original_get)
    second = client.get(MERCHANT, **_bearer(raw))
    assert second.status_code == 401


# --- HasApiKeyScope ---------------------------------------------------


def test_has_api_key_scope_passes_for_a_key_with_that_scope_denies_without(merchant_a, add_member, make_key):
    owner = _owner(merchant_a, add_member, "scope-check@example.com")
    key_with, _ = make_key(owner, scopes=["sales:write"])
    key_without, _ = make_key(owner, scopes=["reviews:read"])

    permission_cls = HasApiKeyScope("sales:write")

    class _Req:
        pass

    req_with = _Req()
    req_with.auth = key_with
    req_without = _Req()
    req_without.auth = key_without

    assert permission_cls().has_permission(req_with, None) is True
    assert permission_cls().has_permission(req_without, None) is False


def test_has_api_key_scope_denies_a_session_principal(merchant_a, add_member):
    owner = _owner(merchant_a, add_member, "scope-session@example.com")
    permission_cls = HasApiKeyScope("sales:write")

    class _Req:
        pass

    req = _Req()
    req.auth = None
    assert permission_cls().has_permission(req, None) is False


def test_has_api_key_scope_denies_a_revoked_key(merchant_a, add_member, make_key):
    from apikeys import services

    owner = _owner(merchant_a, add_member, "scope-revoked@example.com")
    key, _ = make_key(owner, scopes=["sales:write"])
    with tenant_context(merchant_a.id):
        services.revoke_api_key(actor=owner, api_key=key)
        with tenant_atomic():
            key = ApiKey.objects.get(id=key.id)

    class _Req:
        pass

    req = _Req()
    req.auth = key
    assert HasApiKeyScope("sales:write")().has_permission(req, None) is False
