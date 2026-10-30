import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Spec 09-google-business-profile OD-2 / AM-1 / AM-2: one connect attempt.
    Only sha256(state) and sha256(nonce) are stored. The three CHECK
    constraints make the unsafe shapes unrepresentable: a stored code outside
    PENDING, FAILED without a failure_code (or the reverse), and COMPLETED
    without a connection.

    Rollback: reversing drops the table; only attempts still in flight are lost
    (merchants restart the connect flow)."""

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("accounts", "0005_teammemberlocation_rls"),
        ("google_reviews", "0002_googleconnection_rls"),
    ]

    operations = [
        migrations.CreateModel(
            name="GoogleOAuthState",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("state_hash", models.CharField(max_length=64, unique=True)),
                ("nonce_hash", models.CharField(max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("ISSUED", "Issued"),
                            ("PENDING", "Pending"),
                            ("COMPLETED", "Completed"),
                            ("FAILED", "Failed"),
                        ],
                        default="ISSUED",
                        max_length=16,
                    ),
                ),
                ("code_enc", models.TextField(blank=True, null=True)),
                (
                    "failure_code",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("OAUTH_EXPIRED", "Oauth Expired"),
                            ("EXCHANGE_FAILED", "Exchange Failed"),
                            ("EXCHANGE_INTERRUPTED", "Exchange Interrupted"),
                            ("SCOPE_MISSING", "Scope Missing"),
                            ("IDENTITY_MISSING", "Identity Missing"),
                            ("REFRESH_TOKEN_MISSING", "Refresh Token Missing"),
                            ("ID_TOKEN_INVALID", "Id Token Invalid"),
                            ("ID_TOKEN_UNVERIFIABLE", "Id Token Unverifiable"),
                            ("ACTOR_NOT_MEMBER", "Actor Not Member"),
                            ("GOOGLE_ACCOUNT_ALREADY_CONNECTED", "Google Account Already Connected"),
                        ],
                        max_length=48,
                        null=True,
                    ),
                ),
                ("expires_at", models.DateTimeField()),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "google_connection",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="oauth_attempts",
                        to="google_reviews.googleconnection",
                    ),
                ),
                (
                    "merchant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="google_oauth_states",
                        to="accounts.merchant",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="google_oauth_states",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="googleoauthstate",
            index=models.Index(fields=["merchant", "status", "expires_at"], name="google_oauth_merch_st_exp_idx"),
        ),
        migrations.AddConstraint(
            model_name="googleoauthstate",
            constraint=models.CheckConstraint(
                condition=models.Q(("code_enc__isnull", True), ("status", "PENDING"), _connector="OR"),
                name="google_oauth_code_only_pending",
            ),
        ),
        migrations.AddConstraint(
            model_name="googleoauthstate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "FAILED"), ("failure_code__isnull", False)),
                    models.Q(models.Q(("status", "FAILED"), _negated=True), ("failure_code__isnull", True)),
                    _connector="OR",
                ),
                name="google_oauth_failure_code_iff_failed",
            ),
        ),
        migrations.AddConstraint(
            model_name="googleoauthstate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "COMPLETED"), _negated=True),
                    ("google_connection__isnull", False),
                    _connector="OR",
                ),
                name="google_oauth_completed_has_connection",
            ),
        ),
    ]
