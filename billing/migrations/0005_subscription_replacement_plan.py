"""Subscription.replacement_plan (spec 07-plan-change-replacement, W5 amendment):
the plan a pending replacement is for, set exactly when replacement_provider_ref
is. Nullable; no existing row changes. Reversing drops the column (any value in
it is discarded; the round-trip test runs with it empty).
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_teammemberlocation_rls'),
        ('billing', '0004_billing_ref_lookup_replacement'),
    ]

    operations = [
        migrations.AddField(
            model_name='subscription',
            name='replacement_plan',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='billing.plan'),
        ),
        migrations.AddConstraint(
            model_name='subscription',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('replacement_provider_ref__isnull', True), ('replacement_plan__isnull', True)), models.Q(('replacement_provider_ref__isnull', False), ('replacement_plan__isnull', False)), _connector='OR'), name='billing_subscription_replacement_plan_set'),
        ),
    ]
