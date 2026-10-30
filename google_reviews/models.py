"""google_reviews models (spec 09-google-business-profile; Data-Dictionary.md
§GoogleConnection, §GoogleLocation, §GoogleReview).

GoogleConnection and GoogleOAuthState are MERCHANT tables (rls_direct).
GoogleLocation reaches the merchant through its Location, and GoogleReview
through its GoogleLocation (rls_via_parent, which chains), so neither carries a
merchant_id column. GoogleReview has no reply_text / reply_status: those are
Status: V2 - Planned. V1 Implementation: NO.
"""
from django.db import models
from django.db.models import Q

from core.managers import TenantScopedManager
from core.models import BaseModel


class GoogleConnection(BaseModel):
    """A merchant-level Google OAuth authorization. Tokens are Fernet
    ciphertext. google_account_sub (the ID token's `sub`) is the reconnect key
    (spec OD-4); the email is display-only and never an identifier."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        NEEDS_REAUTH = "NEEDS_REAUTH", "Needs reauth"

    merchant = models.ForeignKey(
        "accounts.Merchant", on_delete=models.PROTECT, related_name="google_connections"
    )
    google_account_sub = models.CharField(max_length=255)
    google_account_email = models.CharField(max_length=254)
    access_token_enc = models.TextField()
    refresh_token_enc = models.TextField()
    expires_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    connected_at = models.DateTimeField(null=True, blank=True)

    objects = TenantScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["merchant", "google_account_sub"], name="google_connection_merchant_sub_uniq"
            )
        ]
        indexes = [models.Index(fields=["merchant", "status"], name="google_conn_merch_status_idx")]


class GoogleOAuthState(BaseModel):
    """One connect attempt (spec OD-2, AM-1, AM-2). Only the sha256 of the
    state and of the OpenID Connect nonce are stored. Every status transition
    is a single conditional UPDATE (services.py); the CHECK constraints make
    the unsafe shapes unrepresentable."""

    class Status(models.TextChoices):
        ISSUED = "ISSUED", "Issued"
        PENDING = "PENDING", "Pending"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    class FailureCode(models.TextChoices):
        # Fixed list: no provider text is ever stored.
        OAUTH_EXPIRED = "OAUTH_EXPIRED"
        EXCHANGE_FAILED = "EXCHANGE_FAILED"
        EXCHANGE_INTERRUPTED = "EXCHANGE_INTERRUPTED"
        SCOPE_MISSING = "SCOPE_MISSING"
        IDENTITY_MISSING = "IDENTITY_MISSING"
        REFRESH_TOKEN_MISSING = "REFRESH_TOKEN_MISSING"
        ID_TOKEN_INVALID = "ID_TOKEN_INVALID"
        ID_TOKEN_UNVERIFIABLE = "ID_TOKEN_UNVERIFIABLE"
        ACTOR_NOT_MEMBER = "ACTOR_NOT_MEMBER"
        GOOGLE_ACCOUNT_ALREADY_CONNECTED = "GOOGLE_ACCOUNT_ALREADY_CONNECTED"

    merchant = models.ForeignKey(
        "accounts.Merchant", on_delete=models.PROTECT, related_name="google_oauth_states"
    )
    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="google_oauth_states")
    state_hash = models.CharField(max_length=64, unique=True)
    nonce_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ISSUED)
    # Fernet ciphertext of the authorization code, only between `complete` and
    # the task's "take code".
    code_enc = models.TextField(null=True, blank=True)
    failure_code = models.CharField(max_length=48, choices=FailureCode.choices, null=True, blank=True)
    google_connection = models.ForeignKey(
        GoogleConnection, on_delete=models.PROTECT, null=True, blank=True, related_name="oauth_attempts"
    )
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantScopedManager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(code_enc__isnull=True) | Q(status="PENDING"),
                name="google_oauth_code_only_pending",
            ),
            models.CheckConstraint(
                condition=(Q(status="FAILED") & Q(failure_code__isnull=False))
                | (~Q(status="FAILED") & Q(failure_code__isnull=True)),
                name="google_oauth_failure_code_iff_failed",
            ),
            models.CheckConstraint(
                condition=~Q(status="COMPLETED") | Q(google_connection__isnull=False),
                name="google_oauth_completed_has_connection",
            ),
        ]
        indexes = [models.Index(fields=["merchant", "status", "expires_at"], name="google_oauth_merch_st_exp_idx")]


class GoogleLocation(BaseModel):
    """Maps one internal Location to one Business Profile location. LOCATION
    table: the tenant path is through the Location. google_location_id is the
    full resource name `accounts/{accountId}/locations/{locationId}` (spec
    OD-12) and google_place_id the Place ID; they are never substituted for one
    another, and review_link is Google's `newReviewUri`, never built from
    either (spec RL-1)."""

    tenant_field = "location__merchant_id"

    location = models.OneToOneField(
        "locations.Location", on_delete=models.PROTECT, related_name="google_location"
    )
    google_connection = models.ForeignKey(
        GoogleConnection, on_delete=models.PROTECT, related_name="google_locations"
    )
    google_location_id = models.CharField(max_length=255)
    google_place_id = models.CharField(max_length=255, null=True, blank=True)
    review_link = models.URLField(max_length=2048)
    average_rating = models.DecimalField(max_digits=2, decimal_places=1, null=True, blank=True)
    review_count = models.IntegerField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    objects = TenantScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["google_connection", "google_location_id"],
                name="google_location_unique_per_connection",
            )
        ]


class GoogleReview(BaseModel):
    """One synced review, upserted on (google_location, google_review_id).
    LOCATION table, two hops from the merchant (GoogleLocation -> Location)."""

    tenant_field = "google_location__location__merchant_id"

    google_location = models.ForeignKey(
        GoogleLocation, on_delete=models.PROTECT, related_name="reviews"
    )
    google_review_id = models.CharField(max_length=255)
    reviewer_name = models.CharField(max_length=255, null=True, blank=True)
    rating = models.SmallIntegerField()
    text = models.TextField(null=True, blank=True)
    review_created_at = models.DateTimeField()
    fetched_at = models.DateTimeField()

    objects = TenantScopedManager()

    class Meta:
        # No condition and no expressions: PostgreSQL accepts an ON CONFLICT
        # target only when it matches a plain unique index (spec "Review
        # upsert").
        constraints = [
            models.UniqueConstraint(
                fields=["google_location", "google_review_id"],
                name="google_review_location_review_uniq",
            ),
            models.CheckConstraint(
                condition=Q(rating__gte=1) & Q(rating__lte=5), name="google_review_rating_1_5"
            ),
        ]
        indexes = [
            models.Index(fields=["google_location", "review_created_at"], name="google_review_loc_created_idx")
        ]
