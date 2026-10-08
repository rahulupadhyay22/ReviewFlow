from django.db import migrations

from core.rls import rls_via_parent


class Migration(migrations.Migration):
    dependencies = [("whatsapp", "0002_whatsappaccount_rls")]

    operations = [
        rls_via_parent("whatsapp_whatsapplocationmapping", "location_id", "locations_location")
    ]
