"""Role permission classes (Security-Architecture.md §Authorization).
MANAGER location scoping arrives with TeamMemberLocation in Phase 03."""
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
