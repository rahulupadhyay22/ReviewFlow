import uuid

import django.db.models.deletion
from django.db import migrations, models

from core.rls import rls_direct


class Migration(migrations.Migration):
    """Spec 08-whatsapp-embedded-signup ES-18: the single-use signup state.

    The table and its standard tenant_isolation policy are one logical change,
    so they are one migration: PostgreSQL DDL is transactional, so the pair
    applies or reverses together and the table never exists without RLS.

    Rollback: reversing this drops the policy, then the table. Only signup
    states still in flight are lost (merchants restart Embedded Signup)."""

    dependencies = [
        ("accounts", "0005_teammemberlocation_rls"),
        ("whatsapp", "0008_whatsappaccount_number_lookup"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmbeddedSignupState",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("state_hash", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField()),
                (
                    "merchant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="embedded_signup_states",
                        to="accounts.merchant",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="embedded_signup_states",
                        to="accounts.user",
                    ),
                ),
            ],
            options={"abstract": False},
        ),
        rls_direct("whatsapp_embeddedsignupstate"),
    ]
