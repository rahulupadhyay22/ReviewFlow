from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """Spec 08-whatsapp-embedded-signup ES-2: UNIQUE(merchant, name, language)
    becomes two partial uniques, one per WABA scope. Partial indexes work on
    every PostgreSQL version (NULLS NOT DISTINCT needs 15+).

    Every existing row has a NULL account and was unique under the old
    constraint, so it is unique under the shared partial index.

    Rollback WARNING: reversing this recreates UNIQUE(merchant, name,
    language), which FAILS if the same name and language exist on the shared
    WABA and on an own account. Resolve those rows first."""

    dependencies = [("whatsapp", "0006_messagetemplate_whatsapp_account")]

    operations = [
        migrations.RemoveConstraint(
            model_name="messagetemplate",
            name="whatsapp_template_merchant_name_lang_uniq",
        ),
        migrations.AddConstraint(
            model_name="messagetemplate",
            constraint=models.UniqueConstraint(
                fields=("merchant", "name", "language"),
                condition=Q(("whatsapp_account__isnull", True)),
                name="whatsapp_template_shared_name_lang_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="messagetemplate",
            constraint=models.UniqueConstraint(
                fields=("merchant", "whatsapp_account", "name", "language"),
                condition=Q(("whatsapp_account__isnull", False)),
                name="whatsapp_template_own_name_lang_uniq",
            ),
        ),
    ]
