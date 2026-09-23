from django.db import migrations

from core.rls import rls_direct


class Migration(migrations.Migration):
    dependencies = [("integrations", "0001_initial")]

    operations = [
        rls_direct("integrations_integration"),
        rls_direct("integrations_integrationlocationmapping"),
    ]
