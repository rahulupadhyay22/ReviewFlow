from django.db import migrations

from core.rls import rls_shared_pool


class Migration(migrations.Migration):
    dependencies = [("whatsapp", "0001_initial")]

    operations = [rls_shared_pool("whatsapp_whatsappaccount")]
