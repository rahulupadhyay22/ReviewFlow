from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0003_integration_provider_api"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="integration",
            constraint=models.UniqueConstraint(
                fields=["merchant"],
                condition=Q(provider="api", status="CONNECTED"),
                name="uniq_integration_merchant_connected_api",
            ),
        ),
    ]
