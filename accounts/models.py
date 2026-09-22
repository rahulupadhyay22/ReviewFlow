"""
Accounts (Data-Dictionary.md §Merchant, §User, §TeamMember).

User and Merchant are GLOBAL (Database-Design.md Legend): no merchant_id,
no RLS, plain managers. TeamMember is MERCHANT-scoped with RLS.
"""
import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models

from core.exceptions import TenantContextError
from core.managers import TenantScopedManager
from core.models import BaseModel
from core.tenancy import get_current_lookup_user_id


class UserManager(BaseUserManager):
    @classmethod
    def normalize_email(cls, email):
        return (email or "").strip().lower()

    def get_by_natural_key(self, email):
        return self.get(email=self.normalize_email(email))

    def create_user(self, email, password=None, **extra_fields):
        user = self.model(email=self.normalize_email(email), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra_fields)


class User(BaseModel, AbstractBaseUser, PermissionsMixin):
    """Login identity, independent of any one merchant. `password` is the
    dictionary's password_hash."""

    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)
    # Not in the dictionary: Django Admin staff-only access needs it.
    is_staff = models.BooleanField(default=False)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    def save(self, *args, **kwargs):
        self.email = UserManager.normalize_email(self.email)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email


class Merchant(BaseModel):
    """The tenant root. Soft-delete only (status = DELETED). `plan` arrives
    with Plan in Phase 07."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE"
        SUSPENDED = "SUSPENDED"
        DELETED = "DELETED"

    name = models.CharField(max_length=255)
    business_type = models.CharField(max_length=100, null=True, blank=True)
    timezone = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)

    def __str__(self):
        return self.name


class TeamMemberManager(TenantScopedManager):
    def for_lookup_user(self, user_id):
        """The only merchant-unscoped read of TeamMember: mirrors the
        self_membership RLS policy and only works inside
        core.tenancy.user_lookup_atomic() for this same user."""
        if not isinstance(user_id, uuid.UUID):
            user_id = uuid.UUID(str(user_id))
        if get_current_lookup_user_id() != user_id:
            raise TenantContextError("for_lookup_user() requires user_lookup_atomic() for this user.")
        return models.Manager.get_queryset(self).filter(user_id=user_id)


class TeamMember(BaseModel):
    """Grants a User a role on a Merchant. Only accepted rows grant access."""

    class Role(models.TextChoices):
        OWNER = "OWNER"
        ADMIN = "ADMIN"
        MANAGER = "MANAGER"
        VIEWER = "VIEWER"

    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="team_members")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="memberships")
    role = models.CharField(max_length=16, choices=Role.choices)
    invited_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    objects = TeamMemberManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["merchant", "user"], name="uniq_teammember_merchant_user"),
        ]
