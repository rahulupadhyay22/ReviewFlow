from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm

from accounts.models import Merchant, User


class UserCreationForm(AdminUserCreationForm):
    class Meta:
        model = User
        fields = ("email",)


class UserEditForm(UserChangeForm):
    class Meta:
        model = User
        # Never render or accept the TOTP secret, replay counter or recovery
        # hashes through admin (fields="__all__" would; exclude instead).
        # totp_confirmed_at is shown read-only below.
        exclude = ["totp_secret_encrypted", "totp_last_used_step", "totp_recovery_code_hashes"]

# TeamMember is not registered: TenantScopedManager needs a tenant context,
# so cross-tenant admin waits for the Phase 16 audited privileged path.


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    add_form = UserCreationForm
    form = UserEditForm
    ordering = ("email",)
    list_display = ("email", "is_staff", "is_active")
    list_filter = ("is_staff", "is_superuser", "is_active")
    search_fields = ("email",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login",)}),
        # Read-only: no staff reset path exists yet (Phase 16).
        ("Two-factor", {"fields": ("totp_confirmed_at",)}),
    )
    readonly_fields = ("totp_confirmed_at",)
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),
    )


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    """Read-only: suspension/reactivation is an audited Phase 16 action."""

    list_display = ("name", "status", "timezone", "created_at")
    search_fields = ("name",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
