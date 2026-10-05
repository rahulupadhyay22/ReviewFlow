from django.db import migrations

from core.rls import rls_direct, rls_select_by_billing_ref


class Migration(migrations.Migration):

    dependencies = [("billing", "0001_initial")]

    operations = [
        # billing_plan is GLOBAL reference data: deliberately no RLS.
        rls_direct("billing_subscription"),
        # LOCKED DECISION CHANGE, signed off 2026-09-30 (spec 07 Change 1):
        # SELECT-only pre-tenant Subscription lookup by provider reference.
        # No write policy is ever keyed on app.current_billing_ref.
        rls_select_by_billing_ref("billing_subscription"),
        rls_direct("billing_usagerecord"),
        rls_direct("billing_paymentattempt"),
        rls_direct("billing_billingevent"),
    ]
