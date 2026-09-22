import pytest
from django.core.exceptions import ValidationError

from accounts import services
from accounts.exceptions import InvalidCredentials
from accounts.models import Merchant, TeamMember, User
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db


def test_create_merchant_with_owner_creates_user_merchant_and_accepted_owner(password):
    member = services.create_merchant_with_owner(
        name="Shop", timezone="Asia/Kolkata", owner_email="Owner@Shop.com", owner_password=password
    )
    assert member.role == TeamMember.Role.OWNER
    assert member.accepted_at is not None
    assert member.merchant.name == "Shop"
    assert member.user.email == "owner@shop.com"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timezone": "Mars/Olympus"},
        {"owner_password": "123"},
        {"owner_email": "dup@x.com"},
    ],
)
def test_create_merchant_with_owner_invalid_input_raises_and_leaves_no_partial_rows(
    kwargs, password
):
    User.objects.create_user("dup@x.com", password)
    base = {
        "name": "Shop",
        "timezone": "Asia/Kolkata",
        "owner_email": "new@x.com",
        "owner_password": password,
    }
    base.update(kwargs)
    users_before = User.objects.count()
    merchants_before = Merchant.objects.count()

    with pytest.raises(ValidationError):
        services.create_merchant_with_owner(**base)

    assert User.objects.count() == users_before
    assert Merchant.objects.count() == merchants_before


def test_authenticate_login_returns_membership(make_merchant, password):
    member = make_merchant()
    result = services.authenticate_login(
        request=None, email=member.user.email, password=password
    )
    assert result.id == member.id


def test_authenticate_login_wrong_password_raises(make_merchant):
    member = make_merchant()
    with pytest.raises(InvalidCredentials):
        services.authenticate_login(request=None, email=member.user.email, password="wrong")


def test_authenticate_login_unaccepted_membership_raises(add_member, password):
    from accounts.models import Merchant as M

    owner = User.objects.create_user("x@x.com", password)
    m = services.create_merchant_with_owner(
        name="S", timezone="UTC", owner_email="own@x.com", owner_password=password
    ).merchant
    add_member(m, TeamMember.Role.VIEWER, None, accepted=False, user=owner)
    with pytest.raises(InvalidCredentials):
        services.authenticate_login(request=None, email="x@x.com", password=password)
    assert M.objects.filter(id=m.id).exists()


@pytest.mark.parametrize("status", [Merchant.Status.SUSPENDED, Merchant.Status.DELETED])
def test_authenticate_login_non_active_merchant_raises(make_merchant, status):
    member = make_merchant()
    Merchant.objects.filter(id=member.merchant_id).update(status=status)
    with pytest.raises(InvalidCredentials):
        services.authenticate_login(
            request=None, email=member.user.email, password="Tr1cky-Horse-Battery-Staple!"
        )


def test_resolve_login_membership_none_for_user_without_membership(password):
    user = User.objects.create_user("lonely@x.com", password)
    assert services.resolve_login_membership(user) is None


def test_resolve_login_membership_picks_oldest_membership(make_merchant, add_member):
    first = make_merchant()
    second = make_merchant()
    add_member(second.merchant, TeamMember.Role.ADMIN, None, user=first.user)
    assert services.resolve_login_membership(first.user).id == first.id


def test_get_active_membership_returns_membership_in_current_merchant(make_merchant):
    a, b = make_merchant(), make_merchant()
    with tenant_context(a.merchant_id), tenant_atomic():
        assert services.get_active_membership(a.user).id == a.id
        assert services.get_active_membership(b.user) is None


def test_get_active_membership_none_when_merchant_suspended(make_merchant):
    a = make_merchant()
    Merchant.objects.filter(id=a.merchant_id).update(status=Merchant.Status.SUSPENDED)
    with tenant_context(a.merchant_id), tenant_atomic():
        assert services.get_active_membership(a.user) is None


def test_update_merchant_saves_only_given_fields(merchant_a):
    original_tz = merchant_a.timezone
    services.update_merchant(merchant_a, name="Renamed")
    merchant_a.refresh_from_db()
    assert merchant_a.name == "Renamed"
    assert merchant_a.timezone == original_tz


def test_update_merchant_invalid_timezone_raises_and_does_not_save(merchant_a):
    original_tz = merchant_a.timezone
    with pytest.raises(ValidationError):
        services.update_merchant(merchant_a, timezone="Mars/Olympus")
    merchant_a.refresh_from_db()
    assert merchant_a.timezone == original_tz
