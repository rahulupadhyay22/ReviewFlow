"""Plan-change replacement columns and constraints on billing_subscription
(spec 07-plan-change-replacement, "Models & database changes").

Six nullable columns and seven named constraints. Nothing is backfilled and no
existing row changes: every new column is NULL for every existing row, so every
new constraint holds for them.

Reversing this migration drops the six columns, so any replacement or retired
data held in them is discarded. The reverse round-trip test runs with those
columns empty.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("billing", "0002_rls")]

    operations = [
        migrations.AddField(
            model_name="subscription",
            name="replacement_provider_ref",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="subscription",
            name="replacement_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="subscription",
            name="replacement_committed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="subscription",
            name="replacement_cancel_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="subscription",
            name="retired_provider_ref",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="subscription",
            name="retired_kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("SWITCHED_OLD", "Old subscription after a switch"),
                    ("ABANDONED_REPLACEMENT", "Abandoned replacement"),
                ],
                max_length=24,
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="subscription",
            constraint=models.UniqueConstraint(
                condition=models.Q(("replacement_provider_ref__isnull", False)),
                fields=("replacement_provider_ref",),
                name="billing_subscription_replacement_ref_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="subscription",
            constraint=models.UniqueConstraint(
                condition=models.Q(("retired_provider_ref__isnull", False)),
                fields=("retired_provider_ref",),
                name="billing_subscription_retired_ref_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="subscription",
            constraint=models.CheckConstraint(
                condition=models.Q(replacement_provider_ref__isnull=True)
                | ~models.Q(replacement_provider_ref=models.F("payment_provider_ref")),
                name="billing_subscription_replacement_ref_differs",
            ),
        ),
        migrations.AddConstraint(
            model_name="subscription",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(replacement_provider_ref__isnull=True)
                    & models.Q(replacement_expires_at__isnull=True)
                )
                | (
                    models.Q(replacement_provider_ref__isnull=False)
                    & models.Q(replacement_expires_at__isnull=False)
                ),
                name="billing_subscription_replacement_expiry_set",
            ),
        ),
        migrations.AddConstraint(
            model_name="subscription",
            constraint=models.CheckConstraint(
                condition=models.Q(replacement_committed_at__isnull=True)
                | models.Q(replacement_provider_ref__isnull=False),
                name="billing_subscription_replacement_committed_needs_ref",
            ),
        ),
        migrations.AddConstraint(
            model_name="subscription",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(retired_provider_ref__isnull=True)
                    & models.Q(retired_kind__isnull=True)
                )
                | (
                    models.Q(retired_provider_ref__isnull=False)
                    & models.Q(retired_kind__isnull=False)
                ),
                name="billing_subscription_retired_kind_set",
            ),
        ),
        migrations.AddConstraint(
            model_name="subscription",
            constraint=models.CheckConstraint(
                condition=models.Q(replacement_cancel_confirmed_at__isnull=True)
                | models.Q(replacement_committed_at__isnull=False),
                name="billing_subscription_replacement_confirmed_needs_committed",
            ),
        ),
    ]
