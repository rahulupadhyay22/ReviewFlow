from django.db import migrations

from core.rls import rls_direct, rls_select_by_user


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [
        rls_direct("accounts_teammember"),
        # Signed-off 2026-09-19: SELECT-only login lookup policy.
        rls_select_by_user("accounts_teammember"),
    ]
