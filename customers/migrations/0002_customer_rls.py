from django.db import migrations

from core.rls import rls_direct


class Migration(migrations.Migration):
    dependencies = [("customers", "0001_initial")]

    operations = [rls_direct("customers_customer")]
