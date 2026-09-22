"""Tests for `python manage.py seed_dev` (Development-Setup.md §"Seed Data";
spec Definition of done: "Seed command")."""
import io

import pytest
from django.core.cache import cache
from django.core.management import CommandError, call_command
from rest_framework.test import APIClient

from accounts.models import Merchant, TeamMember, TeamMemberLocation, User
from core.management.commands.seed_dev import SEED_MANAGER_EMAIL, SEED_OWNER_EMAIL
from core.tenancy import tenant_atomic, tenant_context
from locations.models import Location

pytestmark = pytest.mark.django_db

PASSWORD = "Tr1cky-Horse-Battery-Staple!"


@pytest.fixture(autouse=True)
def local_cache(settings):
    """Throttle counters must not leak through Redis between tests."""
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    yield
    cache.clear()


def _run(password=None):
    out = io.StringIO()
    kwargs = {"stdout": out}
    if password is not None:
        call_command("seed_dev", "--password", password, **kwargs)
    else:
        call_command("seed_dev", **kwargs)
    return out.getvalue()


def _login(email, password):
    client = APIClient(enforce_csrf_checks=True)
    client.get("/api/v1/auth/login")
    token = client.cookies["csrftoken"].value
    resp = client.post(
        "/api/v1/auth/login",
        {"email": email, "password": password},
        format="json",
        HTTP_X_CSRFTOKEN=token,
    )
    client.csrf = token
    return client, resp


def test_creates_one_merchant_two_locations_and_manager_assigned_to_first(settings):
    settings.DEBUG = True
    _run(password=PASSWORD)

    owner = User.objects.get(email=SEED_OWNER_EMAIL)
    manager_user = User.objects.get(email=SEED_MANAGER_EMAIL)
    assert Merchant.objects.count() == 1
    merchant = Merchant.objects.get()
    assert merchant.status == Merchant.Status.ACTIVE

    with tenant_context(merchant.id), tenant_atomic():
        locations = list(Location.objects.filter(is_active=True).order_by("created_at"))
        assert len(locations) == 2

        owner_membership = TeamMember.objects.get(user=owner)
        assert owner_membership.role == TeamMember.Role.OWNER
        assert owner_membership.accepted_at is not None

        manager_membership = TeamMember.objects.get(user=manager_user)
        assert manager_membership.role == TeamMember.Role.MANAGER
        assert manager_membership.accepted_at is not None

        assigned = set(
            TeamMemberLocation.objects.filter(team_member=manager_membership).values_list(
                "location_id", flat=True
            )
        )
        assert assigned == {locations[0].id}


def test_both_users_can_log_in_and_manager_sees_one_location(settings):
    settings.DEBUG = True
    _run(password=PASSWORD)

    owner_client, owner_resp = _login(SEED_OWNER_EMAIL, PASSWORD)
    assert owner_resp.status_code == 200

    manager_client, manager_resp = _login(SEED_MANAGER_EMAIL, PASSWORD)
    assert manager_resp.status_code == 200

    resp = manager_client.get("/api/v1/locations")
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 1


def test_second_run_exits_cleanly_and_creates_nothing_new(settings):
    settings.DEBUG = True
    _run(password=PASSWORD)
    assert User.objects.count() == 2
    assert Merchant.objects.count() == 1

    output = _run(password=PASSWORD)

    assert "already seeded" in output
    assert User.objects.count() == 2
    assert Merchant.objects.count() == 1


def test_with_debug_false_raises_command_error_and_creates_nothing(settings):
    settings.DEBUG = False
    with pytest.raises(CommandError):
        _run(password=PASSWORD)
    assert not User.objects.filter(email=SEED_OWNER_EMAIL).exists()
    assert Merchant.objects.count() == 0


def test_without_password_prints_a_generated_password(settings):
    settings.DEBUG = True
    output = _run()
    assert "Password:" in output
    printed_password = output.splitlines()[-1].split("Password:")[-1].strip()
    assert len(printed_password) > 0

    client, resp = _login(SEED_OWNER_EMAIL, printed_password)
    assert resp.status_code == 200
