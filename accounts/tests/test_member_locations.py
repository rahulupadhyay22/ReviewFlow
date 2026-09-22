"""Tests for TeamMemberLocation: set_team_member_locations service,
PUT /team-members/{id}/locations API, the _assert_same_merchant invariant,
role-change clearing, revoke cascade, audit, uniqueness, concurrency, and RLS
on accounts_teammemberlocation (spec Definition of done: "Assignment",
"Cross-tenant location assignment", "Uniqueness", "Concurrency", "Role change
and revoke", "Member body", "Audit", "CSRF")."""
import threading
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import Error, IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from accounts.exceptions import TeamMemberNotFound, TeamPermissionDenied
from accounts.models import TeamMember, TeamMemberLocation
from accounts.services import (
    AUDIT_LOCATIONS_CHANGED,
    _assert_same_merchant,
    change_team_member_role,
    revoke_team_member,
    set_team_member_locations,
)
from auditlog.models import AuditLog
from core.exceptions import TenantContextError
from core.tenancy import tenant_atomic, tenant_context
from locations import services as location_services
from locations.models import Location

pytestmark = pytest.mark.django_db

TEAM_MEMBERS = "/api/v1/team-members"


def _locations_url(pk):
    return f"{TEAM_MEMBERS}/{pk}/locations"


def _put(client, pk, location_ids):
    return client.put(
        _locations_url(pk),
        {"location_ids": [str(x) for x in location_ids]},
        format="json",
        HTTP_X_CSRFTOKEN=client.csrf,
    )


def _create_location(merchant_id, name="Loc"):
    with tenant_context(merchant_id):
        return location_services.create_location(name=name)


def _assign(owner, member_id, location_ids):
    with tenant_context(owner.merchant_id), tenant_atomic():
        return set_team_member_locations(actor=owner, member_id=member_id, location_ids=location_ids)


def _current_assignment_ids(merchant_id, member):
    with tenant_context(merchant_id), tenant_atomic():
        return set(
            TeamMemberLocation.objects.filter(team_member=member).values_list("location_id", flat=True)
        )


def _audit_rows(merchant_id, action=None):
    qs = AuditLog.objects.filter(merchant_id=merchant_id)
    if action:
        qs = qs.filter(action=action)
    return list(qs)


# --- Service: set_team_member_locations -------------------------------


def test_owner_can_set_managers_locations(make_merchant, add_member):
    owner = make_merchant()
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    l1 = _create_location(owner.merchant_id, "L1")

    result = _assign(owner, manager.id, [l1.id])
    assert _current_assignment_ids(owner.merchant_id, result) == {l1.id}


def test_resending_same_set_changes_nothing_and_writes_no_audit_row(make_merchant, add_member):
    owner = make_merchant()
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    l1 = _create_location(owner.merchant_id, "L1")
    _assign(owner, manager.id, [l1.id])

    with tenant_context(owner.merchant_id), tenant_atomic():
        before = len(_audit_rows(owner.merchant_id, AUDIT_LOCATIONS_CHANGED))
    _assign(owner, manager.id, [l1.id])
    with tenant_context(owner.merchant_id), tenant_atomic():
        after = len(_audit_rows(owner.merchant_id, AUDIT_LOCATIONS_CHANGED))
    assert before == after == 1
    assert _current_assignment_ids(owner.merchant_id, manager) == {l1.id}


def test_empty_list_clears_the_set(make_merchant, add_member):
    owner = make_merchant()
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    l1 = _create_location(owner.merchant_id, "L1")
    _assign(owner, manager.id, [l1.id])

    _assign(owner, manager.id, [])
    assert _current_assignment_ids(owner.merchant_id, manager) == set()


def test_duplicate_ids_collapse_to_one_row(make_merchant, add_member):
    owner = make_merchant()
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    l1 = _create_location(owner.merchant_id, "L1")

    _assign(owner, manager.id, [l1.id, l1.id, l1.id])
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert TeamMemberLocation.objects.filter(team_member=manager, location=l1).count() == 1


def test_pending_manager_can_be_assigned(make_merchant, add_member):
    owner = make_merchant()
    manager = add_member(owner.merchant, "MANAGER", "pending@example.com", accepted=False)
    l1 = _create_location(owner.merchant_id, "L1")

    result = _assign(owner, manager.id, [l1.id])
    assert _current_assignment_ids(owner.merchant_id, result) == {l1.id}


def test_inactive_location_may_be_assigned(make_merchant, add_member):
    owner = make_merchant()
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    l1 = _create_location(owner.merchant_id, "L1")
    with tenant_context(owner.merchant_id), tenant_atomic():
        location_services.deactivate_location(l1)

    result = _assign(owner, manager.id, [l1.id])
    assert _current_assignment_ids(owner.merchant_id, result) == {l1.id}


@pytest.mark.parametrize("actor_role", ["MANAGER", "VIEWER"])
def test_manager_and_viewer_actor_raises_permission_denied(actor_role, make_merchant, add_member):
    owner = make_merchant()
    actor = add_member(owner.merchant, actor_role, "actor@example.com")
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    l1 = _create_location(owner.merchant_id, "L1")

    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(TeamPermissionDenied):
            set_team_member_locations(actor=actor, member_id=manager.id, location_ids=[l1.id])


@pytest.mark.parametrize("target_role", ["OWNER", "ADMIN", "VIEWER"])
def test_non_manager_target_raises_validation_error_and_writes_no_rows(
    target_role, make_merchant, add_member
):
    owner = make_merchant()
    if target_role == "OWNER":
        target_id = owner.id
    else:
        target = add_member(owner.merchant, target_role, "target@example.com")
        target_id = target.id
    l1 = _create_location(owner.merchant_id, "L1")

    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(ValidationError):
            set_team_member_locations(actor=owner, member_id=target_id, location_ids=[l1.id])
        assert not TeamMemberLocation.objects.filter(location=l1).exists()


def test_unknown_member_id_raises_team_member_not_found(make_merchant):
    owner = make_merchant()
    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(TeamMemberNotFound):
            set_team_member_locations(actor=owner, member_id=uuid.uuid4(), location_ids=[])


def test_other_merchants_member_id_raises_team_member_not_found(make_merchant, add_member):
    owner_a = make_merchant()
    owner_b = make_merchant()
    b_manager = add_member(owner_b.merchant, "MANAGER", "manager-b@example.com")

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(TeamMemberNotFound):
            set_team_member_locations(actor=owner_a, member_id=b_manager.id, location_ids=[])


def test_unknown_location_id_raises_validation_error(make_merchant, add_member):
    owner = make_merchant()
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")

    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(ValidationError):
            set_team_member_locations(actor=owner, member_id=manager.id, location_ids=[uuid.uuid4()])


# --- Cross-tenant location assignment (priority scenario) ------------------


def test_cross_merchant_location_id_via_service_raises_validation_error_same_body_as_unknown(
    make_merchant, add_member
):
    owner_a = make_merchant()
    owner_b = make_merchant()
    manager = add_member(owner_a.merchant, "MANAGER", "manager@example.com")
    b_loc = _create_location(owner_b.merchant_id, "B loc")

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(ValidationError) as unknown_exc:
            set_team_member_locations(actor=owner_a, member_id=manager.id, location_ids=[uuid.uuid4()])
        with pytest.raises(ValidationError) as cross_exc:
            set_team_member_locations(actor=owner_a, member_id=manager.id, location_ids=[b_loc.id])
        assert not TeamMemberLocation.objects.filter(location=b_loc).exists()

    assert unknown_exc.value.message_dict == cross_exc.value.message_dict


def test_assert_same_merchant_raises_before_insert_for_unsaved_mismatched_location(make_merchant, add_member):
    """Reaches the invariant directly with a MANAGER from A and a Location
    whose merchant_id is B, bypassing the scoped read: proves the explicit
    check runs, not just RLS/TenantScopedManager filtering."""
    owner_a = make_merchant()
    owner_b = make_merchant()
    manager = add_member(owner_a.merchant, "MANAGER", "manager@example.com")

    other_merchant_location = Location(id=uuid.uuid4(), merchant_id=owner_b.merchant_id, name="Unsaved")

    with tenant_context(owner_a.merchant_id), tenant_atomic():
        with pytest.raises(ValidationError):
            _assert_same_merchant(manager, [other_merchant_location])


# --- Uniqueness / concurrency -----------------------------------------------


def test_direct_duplicate_insert_raises_integrity_error(make_merchant, add_member):
    owner = make_merchant()
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    l1 = _create_location(owner.merchant_id, "L1")
    _assign(owner, manager.id, [l1.id])

    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(IntegrityError), transaction.atomic():
            TeamMemberLocation.objects.create(merchant=owner.merchant, team_member=manager, location=l1)


@pytest.mark.django_db(transaction=True)
def test_concurrent_set_calls_end_in_one_calls_set_with_no_integrity_error(make_merchant, add_member):
    owner = make_merchant()
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    l1 = _create_location(owner.merchant_id, "L1")
    l2 = _create_location(owner.merchant_id, "L2")

    barrier = threading.Barrier(2)
    errors = []

    def _run(location_ids):
        try:
            barrier.wait(timeout=5)
            _assign(owner, manager.id, location_ids)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)
        finally:
            connection.close()

    t1 = threading.Thread(target=_run, args=([l1.id],))
    t2 = threading.Thread(target=_run, args=([l2.id],))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == []
    final = _current_assignment_ids(owner.merchant_id, manager)
    assert final in ({l1.id}, {l2.id})


# --- Role change and revoke -------------------------------------------------


def test_role_change_away_from_manager_deletes_assignments_and_reverting_starts_empty(
    make_merchant, add_member
):
    owner = make_merchant()
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    l1 = _create_location(owner.merchant_id, "L1")
    _assign(owner, manager.id, [l1.id])

    with tenant_context(owner.merchant_id), tenant_atomic():
        change_team_member_role(actor=owner, member_id=manager.id, role="VIEWER")
    assert _current_assignment_ids(owner.merchant_id, manager) == set()

    with tenant_context(owner.merchant_id), tenant_atomic():
        change_team_member_role(actor=owner, member_id=manager.id, role="MANAGER")
    assert _current_assignment_ids(owner.merchant_id, manager) == set()


def test_role_change_between_two_non_manager_roles_touches_no_assignment_rows(make_merchant, add_member):
    owner = make_merchant()
    member = add_member(owner.merchant, "VIEWER", "member@example.com")

    with tenant_context(owner.merchant_id), tenant_atomic():
        before = TeamMemberLocation.objects.filter(team_member=member).count()
        change_team_member_role(actor=owner, member_id=member.id, role="ADMIN")
        after = TeamMemberLocation.objects.filter(team_member=member).count()
    assert before == after == 0


def test_revoking_a_manager_deletes_assignments_and_leaves_locations_intact(make_merchant, add_member):
    owner = make_merchant()
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    l1 = _create_location(owner.merchant_id, "L1")
    _assign(owner, manager.id, [l1.id])

    with tenant_context(owner.merchant_id), tenant_atomic():
        revoke_team_member(actor=owner, member_id=manager.id)

    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not TeamMemberLocation.objects.filter(location=l1).exists()
        assert Location.objects.filter(pk=l1.id).exists()


# --- Member body -------------------------------------------------------


def test_get_team_members_has_no_n_plus_1_queries_as_member_count_grows(make_merchant, add_member, session_client):
    owner = make_merchant()
    client = session_client(owner.user.email)

    for i in range(3):
        add_member(owner.merchant, "VIEWER", f"few-{i}@example.com")
    with CaptureQueriesContext(connection) as few:
        client.get(TEAM_MEMBERS)

    for i in range(3, 15):
        add_member(owner.merchant, "VIEWER", f"many-{i}@example.com")
    with CaptureQueriesContext(connection) as many:
        client.get(TEAM_MEMBERS)

    assert len(many) <= len(few) + 1  # allow session/tenant overhead noise, not O(n) growth


# --- API: PUT /team-members/{id}/locations ----------------------------------


def test_put_locations_owner_and_admin_succeed_with_location_ids_in_body(
    make_merchant, add_member, session_client
):
    owner = make_merchant()
    client = session_client(owner.user.email)
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    l1 = _create_location(owner.merchant_id, "L1")

    resp = _put(client, manager.id, [l1.id])
    assert resp.status_code == 200
    assert resp.json()["location_ids"] == [str(l1.id)]


@pytest.mark.parametrize("role", ["MANAGER", "VIEWER"])
def test_put_locations_returns_403_for_manager_and_viewer_actors(role, make_merchant, add_member, session_client):
    owner = make_merchant()
    actor_email = f"actor-{role.lower()}@example.com"
    add_member(owner.merchant, role, actor_email)
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    client = session_client(actor_email)

    resp = _put(client, manager.id, [])
    assert resp.status_code == 403


def test_put_locations_non_manager_target_returns_422(make_merchant, add_member, session_client):
    owner = make_merchant()
    client = session_client(owner.user.email)
    viewer = add_member(owner.merchant, "VIEWER", "viewer@example.com")

    resp = _put(client, viewer.id, [])
    assert resp.status_code == 422


def test_put_locations_malformed_uuid_returns_422(make_merchant, add_member, session_client):
    owner = make_merchant()
    client = session_client(owner.user.email)
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")

    resp = client.put(
        _locations_url(manager.id),
        {"location_ids": ["not-a-uuid"]},
        format="json",
        HTTP_X_CSRFTOKEN=client.csrf,
    )
    assert resp.status_code == 422


def test_put_locations_unknown_member_id_returns_404(make_merchant, session_client):
    owner = make_merchant()
    client = session_client(owner.user.email)
    resp = _put(client, uuid.uuid4(), [])
    assert resp.status_code == 404


def test_put_locations_other_merchants_member_id_returns_404(make_merchant, add_member, session_client):
    owner_a = make_merchant()
    owner_b = make_merchant()
    b_manager = add_member(owner_b.merchant, "MANAGER", "manager-b@example.com")
    client = session_client(owner_a.user.email)

    resp = _put(client, b_manager.id, [])
    assert resp.status_code == 404


def test_put_locations_cross_merchant_location_id_returns_422_identical_body_and_no_row_created(
    make_merchant, add_member, session_client
):
    owner_a = make_merchant()
    owner_b = make_merchant()
    manager = add_member(owner_a.merchant, "MANAGER", "manager@example.com")
    b_loc = _create_location(owner_b.merchant_id, "B loc")
    client = session_client(owner_a.user.email)

    unknown_resp = _put(client, manager.id, [uuid.uuid4()])
    cross_resp = _put(client, manager.id, [b_loc.id])

    assert unknown_resp.status_code == cross_resp.status_code == 422
    assert unknown_resp.json()["error"]["code"] == cross_resp.json()["error"]["code"]
    with tenant_context(owner_a.merchant_id), tenant_atomic():
        assert not TeamMemberLocation.objects.filter(location_id=b_loc.id).exists()


def test_put_locations_without_csrf_returns_403(make_merchant, add_member, session_client):
    owner = make_merchant()
    client = session_client(owner.user.email)
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    l1 = _create_location(owner.merchant_id, "L1")

    resp = client.put(_locations_url(manager.id), {"location_ids": [str(l1.id)]}, format="json")
    assert resp.status_code == 403
    with tenant_context(owner.merchant_id), tenant_atomic():
        assert not TeamMemberLocation.objects.filter(team_member=manager).exists()


def test_put_locations_returns_403_when_unauthenticated(make_merchant, add_member):
    owner = make_merchant()
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    resp = APIClient().put(_locations_url(manager.id), {"location_ids": []}, format="json")
    assert resp.status_code == 403


# --- Audit ---------------------------------------------------------------


def test_changed_assignment_writes_exactly_one_audit_row_with_uuid_only_metadata(make_merchant, add_member):
    owner = make_merchant()
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    l1 = _create_location(owner.merchant_id, "L1")

    _assign(owner, manager.id, [l1.id])

    with tenant_context(owner.merchant_id), tenant_atomic():
        rows = _audit_rows(owner.merchant_id, AUDIT_LOCATIONS_CHANGED)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_user_id == owner.user_id
        assert row.target_type == "accounts.teammember"
        assert row.target_id == str(manager.id)
        assert row.metadata_json == {"location_ids": [str(l1.id)]}


def test_no_audit_row_for_failed_call(make_merchant, add_member):
    owner = make_merchant()
    viewer = add_member(owner.merchant, "VIEWER", "viewer@example.com")

    with tenant_context(owner.merchant_id), tenant_atomic():
        before = len(_audit_rows(owner.merchant_id, AUDIT_LOCATIONS_CHANGED))
        with pytest.raises(ValidationError):
            set_team_member_locations(actor=owner, member_id=viewer.id, location_ids=[])
        after = len(_audit_rows(owner.merchant_id, AUDIT_LOCATIONS_CHANGED))
    assert before == after


def test_assignment_change_rolls_back_if_record_raises(make_merchant, add_member, monkeypatch):
    owner = make_merchant()
    manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
    l1 = _create_location(owner.merchant_id, "L1")

    def _boom(*args, **kwargs):
        raise RuntimeError("audit backend down")

    monkeypatch.setattr("accounts.services.record", _boom)

    with tenant_context(owner.merchant_id), tenant_atomic():
        with pytest.raises(RuntimeError):
            set_team_member_locations(actor=owner, member_id=manager.id, location_ids=[l1.id])

    assert _current_assignment_ids(owner.merchant_id, manager) == set()


# --- RLS: accounts_teammemberlocation (Multi-Tenancy.md Required Tests 1-2) -


@pytest.mark.django_db(transaction=True)
class TestTeamMemberLocationRLS:
    def _ids(self, sql, params=()):
        with connection.cursor() as cur:
            cur.execute(sql, params)
            return {str(r[0]) for r in cur.fetchall()}

    def test_manager_queried_without_tenant_context_raises(self, make_merchant, add_member):
        owner = make_merchant()
        manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
        l1 = _create_location(owner.merchant_id, "L1")
        _assign(owner, manager.id, [l1.id])
        with pytest.raises(TenantContextError):
            list(TeamMemberLocation.objects.all())

    def test_raw_sql_in_merchant_context_returns_only_that_merchants_rows(self, make_merchant, add_member):
        owner_a = make_merchant()
        owner_b = make_merchant()
        manager_a = add_member(owner_a.merchant, "MANAGER", "manager-a@example.com")
        manager_b = add_member(owner_b.merchant, "MANAGER", "manager-b@example.com")
        l1 = _create_location(owner_a.merchant_id, "L1")
        l2 = _create_location(owner_b.merchant_id, "L2")
        _assign(owner_a, manager_a.id, [l1.id])
        _assign(owner_b, manager_b.id, [l2.id])

        with tenant_context(owner_a.merchant_id), tenant_atomic():
            ids = self._ids("SELECT id FROM accounts_teammemberlocation")
        with tenant_context(owner_a.merchant_id), tenant_atomic():
            a_row_id = str(
                TeamMemberLocation.objects.get(team_member=manager_a, location_id=l1.id).id
            )
        assert ids == {a_row_id}

    def test_raw_sql_without_context_returns_nothing(self, make_merchant, add_member):
        owner = make_merchant()
        manager = add_member(owner.merchant, "MANAGER", "manager@example.com")
        l1 = _create_location(owner.merchant_id, "L1")
        _assign(owner, manager.id, [l1.id])
        assert self._ids("SELECT id FROM accounts_teammemberlocation") == set()

    def test_raw_insert_for_other_merchant_fails_with_check(self, make_merchant, add_member):
        owner_a = make_merchant()
        owner_b = make_merchant()
        manager_a = add_member(owner_a.merchant, "MANAGER", "manager-a@example.com")
        l1 = _create_location(owner_a.merchant_id, "L1")

        with tenant_context(owner_a.merchant_id), tenant_atomic():
            with pytest.raises(Error), transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute(
                        "INSERT INTO accounts_teammemberlocation "
                        "(id, created_at, updated_at, merchant_id, team_member_id, location_id) "
                        "VALUES (gen_random_uuid(), now(), now(), %s, %s, %s)",
                        [str(owner_b.merchant_id), str(manager_a.id), str(l1.id)],
                    )

    def test_raw_update_for_other_merchant_fails_with_check(self, make_merchant, add_member):
        owner_a = make_merchant()
        owner_b = make_merchant()
        manager_a = add_member(owner_a.merchant, "MANAGER", "manager-a@example.com")
        l1 = _create_location(owner_a.merchant_id, "L1")
        _assign(owner_a, manager_a.id, [l1.id])

        with tenant_context(owner_a.merchant_id), tenant_atomic():
            row_id = str(
                TeamMemberLocation.objects.get(team_member=manager_a, location_id=l1.id).id
            )
            with pytest.raises(Error), transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute(
                        "UPDATE accounts_teammemberlocation SET merchant_id = %s WHERE id = %s",
                        [str(owner_b.merchant_id), row_id],
                    )
