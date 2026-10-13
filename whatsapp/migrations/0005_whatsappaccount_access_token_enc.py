from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """Spec 08-whatsapp-embedded-signup ES-1: the merchant's encrypted Meta
    business token. Nullable and additive, so every existing row is valid.

    Rollback WARNING: reversing this drops access_token_enc, which destroys
    every stored own-number credential. Those merchants must reconnect. Take a
    backup first."""

    dependencies = [("whatsapp", "0004_messagetemplate_rls")]

    operations = [
        migrations.AddField(
            model_name="whatsappaccount",
            name="access_token_enc",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="whatsappaccount",
            constraint=models.CheckConstraint(
                condition=Q(("sender_type", "OWN_NUMBER"), ("access_token_enc__isnull", True), _connector="OR"),
                name="whatsapp_account_token_own_number_only",
            ),
        ),
    ]
