from django.db import migrations

from core.rls import rls_select_by_integration_id


class Migration(migrations.Migration):

    dependencies = [("integrations", "0004_integration_uniq_connected_api")]

    operations = [
        # LOCKED DECISION CHANGE, approved (spec 06 Decision 1): SELECT-only
        # pre-tenant Integration lookup policy, mirroring self_membership /
        # api_key_lookup. No write policy is ever keyed on
        # app.current_integration_id.
        rls_select_by_integration_id("integrations_integration"),
    ]
