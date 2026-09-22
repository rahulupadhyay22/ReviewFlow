import pytest
from rest_framework.test import APIClient

from accounts.models import TeamMember
from auditlog.models import AuditLog
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

TEAM_MEMBERS = "/api/v1/team-members"


def _detail(pk):
    return f"{TEAM_MEMBERS}/{pk}"


def _post(client, data):
    return client.post(TEAM_MEMBERS, data, format="json", HTTP_X_CSRFTOKEN=client.csrf)


def _patch(client, pk, data):
    return client.patch(_detail(pk), data, format="json", HTTP_X_CSRFTOKEN=client.csrf)


def _delete(client, pk):
    return client.delete(_detail(pk), HTTP_X_CSRFTOKEN=client.csrf)


def _client_for_role(role, owner, add_member, session_client):
    if role == "OWNER":
        return session_client(owner.user.email), owner
    email = f"{role.lower()}@example.com"
    member = add_member(owner.merchant, role, email)
    return session_client(email), member


def _audit_rows(merchant_id, action=None):
    qs = AuditLog.objects.filter(merchant_id=merchant_id)
    if action:
        qs = qs.filter(action=action)
    return list(qs)


# --- Permissions -----------------------------------------------------------


@pytest.mark.parametrize("role", ["OWNER", "ADMIN"])
def test_get_team_members_succeeds_for_owner_and_admin(role, make_merchant, add_member, session_client):
    owner = make_merchant()
    client, _ = _client_for_role(role, owner, add_member, session_client)
    assert client.get(TEAM_MEMBERS).status_code == 200


@pytest.mark.parametrize("role", ["MANAGER", "VIEWER"])
def test_get_team_members_returns_403_for_manager_and_viewer(role, make_merchant, add_member, session_client):
    owner = make_merchant()
    client, _ = _client_for_role(role, owner, add_member, session_client)
    resp = client.get(TEAM_MEMBERS)
    assert resp.status_code == 403
    assert set(resp.json()) == {"error"}


def test_get_team_members_returns_403_when_unauthenticated():
    assert APIClient().get(TEAM_MEMBERS).status_code == 403


@pytest.mark.parametrize("role", ["OWNER", "ADMIN"])
def test_post_team_members_succeeds_for_owner_and_admin(role, make_merchant, add_member, session_client):
    owner = make_merchant()
    client, _ = _client_for_role(role, owner, add_member, session_client)
    resp = _post(client, {"email": f"invitee-{role}@x.com", "role": "VIEWER"})
    assert resp.status_code == 201


@pytest.mark.parametrize("role", ["MANAGER", "VIEWER"])
def test_post_team_members_returns_403_for_manager_and_viewer(role, make_merchant, add_member, session_client):
    owner = make_merchant()
    client, _ = _client_for_role(role, owner, add_member, session_client)
    resp = _post(client, {"email": "invitee@x.com", "role": "VIEWER"})
    assert resp.status_code == 403


def test_post_team_members_returns_403_when_unauthenticated():
    resp = APIClient().post(TEAM_MEMBERS, {"email": "x@x.com", "role": "VIEWER"}, format="json")
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["OWNER", "ADMIN"])
def test_patch_and_delete_team_member_succeed_for_owner_and_admin(role, make_merchant, add_member, session_client):
    owner = make_merchant()
    client, actor = _client_for_role(role, owner, add_member, session_client)
    target = add_member(owner.merchant, "VIEWER", f"target-{role}@x.com")

    resp = _patch(client, target.pk, {"role": "MANAGER"})
    assert resp.status_code == 200

    resp = _delete(client, target.pk)
    assert resp.status_code == 204


@pytest.mark.parametrize("role", ["MANAGER", "VIEWER"])
def test_patch_and_delete_team_member_return_403_for_manager_and_viewer(
    role, make_merchant, add_member, session_client
):
    owner = make_merchant()
    client, _ = _client_for_role(role, owner, add_member, session_client)
    target = add_member(owner.merchant, "VIEWER", f"target-{role}@x.com")

    assert _patch(client, target.pk, {"role": "MANAGER"}).status_code == 403
    assert _delete(client, target.pk).status_code == 403


def test_patch_and_delete_team_member_return_403_when_unauthenticated(make_merchant, add_member):
    owner = make_merchant()
    target = add_member(owner.merchant, "VIEWER", "target@x.com")
    client = APIClient()
    assert client.patch(_detail(target.pk), {"role": "MANAGER"}, format="json").status_code == 403
    assert client.delete(_detail(target.pk)).status_code == 403


# --- GET list ---------------------------------------------------------------


def test_get_team_members_returns_only_current_merchant_pending_and_accepted_no_locations(
    make_merchant, add_member, session_client
):
    a = make_merchant()
    accepted = add_member(a.merchant, "ADMIN", "accepted@x.com")
    with tenant_context(a.merchant_id), tenant_atomic():
        from accounts import services

        pending, _ = services.invite_team_member(actor=a, email="pending@x.com", role="VIEWER")
    b = make_merchant()

    client = session_client(a.user.email)
    resp = client.get(TEAM_MEMBERS)
    assert resp.status_code == 200
    body = resp.json()
    ids = {m["id"] for m in body["results"]}
    assert str(a.pk) in ids
    assert str(accepted.pk) in ids
    assert str(pending.pk) in ids
    assert str(b.pk) not in ids
    for m in body["results"]:
        assert "locations" not in m
        assert set(m) == {"id", "user", "role", "invited_at", "accepted_at"}


# --- POST invite -------------------------------------------------------------


def test_post_invite_new_email_returns_201_with_invite_token(make_merchant, session_client):
    owner = make_merchant()
    client = session_client(owner.user.email)
    resp = _post(client, {"email": "brandnew@x.com", "role": "ADMIN"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["invite_token"]
    assert body["role"] == "ADMIN"
    assert body["accepted_at"] is None


def test_post_invite_accepted_member_returns_409_already_member(make_merchant, add_member, session_client):
    owner = make_merchant()
    add_member(owner.merchant, "VIEWER", "already@x.com")
    client = session_client(owner.user.email)
    resp = _post(client, {"email": "already@x.com", "role": "ADMIN"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "already_member"


def test_post_invite_admin_inviting_owner_returns_403(make_merchant, add_member, session_client):
    owner = make_merchant()
    add_member(owner.merchant, "ADMIN", "admin@x.com")
    client = session_client("admin@x.com")
    resp = _post(client, {"email": "wannabe@x.com", "role": "OWNER"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "team_permission_denied"


def test_post_invite_invalid_role_or_email_returns_422(make_merchant, session_client):
    owner = make_merchant()
    client = session_client(owner.user.email)
    resp = _post(client, {"email": "not-an-email", "role": "ADMIN"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert resp.json()["error"]["field_errors"]["email"]

    resp = _post(client, {"email": "ok@x.com", "role": "SUPERUSER"})
    assert resp.status_code == 422
    assert resp.json()["error"]["field_errors"]["role"]


def test_post_invite_without_csrf_returns_403(make_merchant, session_client):
    owner = make_merchant()
    client = session_client(owner.user.email)
    resp = client.post(TEAM_MEMBERS, {"email": "x@x.com", "role": "VIEWER"}, format="json")
    assert resp.status_code == 403


# --- PATCH role change --------------------------------------------------


def test_patch_unknown_id_in_other_merchant_returns_404_not_403(make_merchant, add_member, session_client):
    a, b = make_merchant(), make_merchant()
    b_member = add_member(b.merchant, "VIEWER", "b-member@x.com")
    client = session_client(a.user.email)

    resp = _patch(client, b_member.pk, {"role": "ADMIN"})
    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "not_found", "message": "Team member not found."}}

    with tenant_context(b.merchant_id), tenant_atomic():
        b_member.refresh_from_db()
        assert b_member.role == "VIEWER"


def test_patch_self_returns_403(make_merchant, session_client):
    owner = make_merchant()
    client = session_client(owner.user.email)
    resp = _patch(client, owner.pk, {"role": "ADMIN"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "team_permission_denied"


def test_patch_invalid_role_returns_422(make_merchant, add_member, session_client):
    owner = make_merchant()
    target = add_member(owner.merchant, "VIEWER", "target@x.com")
    client = session_client(owner.user.email)
    resp = _patch(client, target.pk, {"role": "SUPERUSER"})
    assert resp.status_code == 422


def test_patch_without_csrf_returns_403(make_merchant, add_member, session_client):
    owner = make_merchant()
    target = add_member(owner.merchant, "VIEWER", "target@x.com")
    client = session_client(owner.user.email)
    resp = client.patch(_detail(target.pk), {"role": "ADMIN"}, format="json")
    assert resp.status_code == 403


# --- DELETE revoke -----------------------------------------------------


def test_delete_returns_204_and_deletes_row(make_merchant, add_member, session_client):
    owner = make_merchant()
    target = add_member(owner.merchant, "VIEWER", "target@x.com")
    client = session_client(owner.user.email)
    resp = _delete(client, target.pk)
    assert resp.status_code == 204
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not TeamMember.objects.filter(pk=target.pk).exists()


def test_revoked_members_next_request_with_existing_session_returns_403(
    make_merchant, add_member, session_client
):
    owner = make_merchant()
    target = add_member(owner.merchant, "VIEWER", "target@x.com")
    owner_client = session_client(owner.user.email)
    target_client = session_client("target@x.com")

    assert target_client.get("/api/v1/merchant").status_code == 200
    assert _delete(owner_client, target.pk).status_code == 204
    assert target_client.get("/api/v1/merchant").status_code == 403


def test_delete_other_merchant_id_returns_404(make_merchant, add_member, session_client):
    a, b = make_merchant(), make_merchant()
    b_member = add_member(b.merchant, "VIEWER", "b-member@x.com")
    client = session_client(a.user.email)

    resp = _delete(client, b_member.pk)
    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "not_found", "message": "Team member not found."}}
    with tenant_context(b.merchant_id), tenant_atomic():
        assert TeamMember.objects.filter(pk=b_member.pk).exists()


def test_delete_without_csrf_returns_403(make_merchant, add_member, session_client):
    owner = make_merchant()
    target = add_member(owner.merchant, "VIEWER", "target@x.com")
    client = session_client(owner.user.email)
    resp = client.delete(_detail(target.pk))
    assert resp.status_code == 403
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert TeamMember.objects.filter(pk=target.pk).exists()


# --- Tenant isolation, principal-derived merchant --------------------------


def test_post_team_members_ignores_merchant_id_in_body_creates_in_actors_merchant_only(
    make_merchant, session_client
):
    a, b = make_merchant(), make_merchant()
    client = session_client(a.user.email)

    resp = _post(client, {"email": "cross@x.com", "role": "VIEWER", "merchant_id": str(b.merchant_id)})
    assert resp.status_code == 201

    from accounts.models import User

    user = User.objects.get(email="cross@x.com")
    with tenant_context(a.merchant_id), tenant_atomic():
        assert TeamMember.objects.filter(merchant=a.merchant, user=user).exists()
    with tenant_context(b.merchant_id), tenant_atomic():
        assert not TeamMember.objects.filter(merchant=b.merchant, user=user).exists()


# --- Audit -------------------------------------------------------------


def test_successful_invite_writes_exactly_one_audit_row_with_correct_fields(make_merchant, session_client):
    owner = make_merchant()
    client = session_client(owner.user.email)
    resp = _post(client, {"email": "audited@x.com", "role": "ADMIN"})
    member_id = resp.json()["id"]

    with tenant_context(owner.merchant_id), tenant_atomic():
        rows = _audit_rows(owner.merchant_id, "team_member.invited")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_user_id == owner.user_id
        assert row.target_type == "accounts.teammember"
        assert row.target_id == member_id
        assert row.metadata_json == {"role": "ADMIN"}
        assert "invite_token" not in (row.metadata_json or {})
        assert "audited@x.com" not in str(row.metadata_json)


def test_failed_action_writes_no_audit_row(make_merchant, add_member, session_client):
    owner = make_merchant()
    add_member(owner.merchant, "VIEWER", "already@x.com")
    client = session_client(owner.user.email)
    with tenant_context(owner.merchant_id), tenant_atomic():
        before = len(_audit_rows(owner.merchant_id))
    _post(client, {"email": "already@x.com", "role": "ADMIN"})
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert len(_audit_rows(owner.merchant_id)) == before
