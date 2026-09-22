"""Role permission classes (Security-Architecture.md §Authorization).
MANAGER location scoping (which locations, not just the role gate) lives in
locations.services.accessible_locations, not here: a permission class
cannot see the target's assignment before the object is loaded (same
precedent as accounts.services._check_can_manage for team-member targets)."""
from rest_framework.permissions import BasePermission

from accounts import services
from accounts.models import TeamMember


class IsMerchantMember(BasePermission):
    """Authenticated, has a session merchant, and holds an accepted
    membership in it. The DRF default permission, so views fail closed."""

    def has_permission(self, request, view):
        if not request.user.is_authenticated or getattr(request, "merchant_id", None) is None:
            return False
        request.team_member = services.get_active_membership(request.user)
        return request.team_member is not None


class HasRole(IsMerchantMember):
    roles = frozenset()

    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.team_member.role in self.roles


class IsOwner(HasRole):
    roles = frozenset({TeamMember.Role.OWNER})


class IsOwnerOrAdmin(HasRole):
    roles = frozenset({TeamMember.Role.OWNER, TeamMember.Role.ADMIN})


class IsOwnerAdminOrManager(HasRole):
    roles = frozenset({TeamMember.Role.OWNER, TeamMember.Role.ADMIN, TeamMember.Role.MANAGER})
