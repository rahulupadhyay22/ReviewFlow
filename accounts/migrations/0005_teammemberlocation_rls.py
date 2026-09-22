from django.db import migrations

from core.rls import rls_direct


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_teammemberlocation")]

    operations = [rls_direct("accounts_teammemberlocation")]
