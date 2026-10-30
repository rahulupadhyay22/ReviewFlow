import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Spec 09-google-business-profile: GoogleConnection (merchant-level OAuth
    authorization, Fernet-encrypted tokens). UNIQUE(merchant, google_account_sub)
    is the reconnect key (OD-4).

    Rollback: reversing drops the table, so stored connections are lost and
    merchants reconnect. Nothing else references it yet at this migration."""

    initial = True

    dependencies = [("accounts", "0005_teammemberlocation_rls")]

    operations = [
        migrations.CreateModel(
            name="GoogleConnection",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("google_account_sub", models.CharField(max_length=255)),
                ("google_account_email", models.CharField(max_length=254)),
                ("access_token_enc", models.TextField()),
                ("refresh_token_enc", models.TextField()),
                ("expires_at", models.DateTimeField()),
                (
                    "status",
                    models.CharField(
                        choices=[("ACTIVE", "Active"), ("NEEDS_REAUTH", "Needs reauth")],
                        default="ACTIVE",
                        max_length=16,
                    ),
                ),
                ("connected_at", models.DateTimeField(blank=True, null=True)),
                (
                    "merchant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="google_connections",
                        to="accounts.merchant",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="googleconnection",
            index=models.Index(fields=["merchant", "status"], name="google_conn_merch_status_idx"),
        ),
        migrations.AddConstraint(
            model_name="googleconnection",
            constraint=models.UniqueConstraint(
                fields=("merchant", "google_account_sub"), name="google_connection_merchant_sub_uniq"
            ),
        ),
    ]
