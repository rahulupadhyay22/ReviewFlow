import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Spec 08-whatsapp-embedded-signup ES-2: the WABA a template belongs to.
    NULL means the shared-pool WABA, so every existing template is unchanged.

    Rollback: reversing this drops the template-to-account bindings."""

    dependencies = [("whatsapp", "0005_whatsappaccount_access_token_enc")]

    operations = [
        migrations.AddField(
            model_name="messagetemplate",
            name="whatsapp_account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="message_templates",
                to="whatsapp.whatsappaccount",
            ),
        ),
    ]
