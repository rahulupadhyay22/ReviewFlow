from django.db import migrations

from core.rls import rls_direct, rls_select_by_key_hash


class Migration(migrations.Migration):
    dependencies = [("apikeys", "0001_initial")]

    operations = [
        rls_direct("apikeys_apikey"),
        # Signed-off 2026-09-24: SELECT-only API key lookup policy.
        rls_select_by_key_hash("apikeys_apikey"),
    ]
