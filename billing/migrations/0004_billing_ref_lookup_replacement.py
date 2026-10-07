"""billing_ref_lookup now matches the replacement ref too (spec
07-plan-change-replacement, "Models & database changes").

LOCKED DECISION CHANGE, approved in principle 2026-10-05 (formal sign-off at PR
time): the SELECT-only pre-tenant Subscription lookup matches
payment_provider_ref OR replacement_provider_ref. It stays SELECT-only; no write
policy is keyed on app.current_billing_ref, and billing_subscription keeps
exactly two policies (tenant_isolation, billing_ref_lookup).

Reversible: the reverse restores the single-column policy. Depends on 0003, so a
reverse undoes this policy before the column is dropped.
"""
from django.db import migrations

from core.rls import rls_replace_select_by_billing_ref


class Migration(migrations.Migration):

    dependencies = [("billing", "0003_subscription_replacement")]

    operations = [
        rls_replace_select_by_billing_ref(
            "billing_subscription",
            old_columns=("payment_provider_ref",),
            new_columns=("payment_provider_ref", "replacement_provider_ref"),
        ),
    ]
