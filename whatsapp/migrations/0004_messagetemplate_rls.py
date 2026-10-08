from django.db import migrations

from core.rls import rls_direct


class Migration(migrations.Migration):
    dependencies = [("whatsapp", "0003_whatsapplocationmapping_rls")]

    operations = [rls_direct("whatsapp_messagetemplate")]
