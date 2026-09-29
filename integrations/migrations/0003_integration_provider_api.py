from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0002_integrations_rls"),
    ]

    operations = [
        migrations.AlterField(
            model_name="integration",
            name="provider",
            field=models.CharField(
                choices=[
                    ("shopify", "Shopify"),
                    ("woocommerce", "WooCommerce"),
                    ("petpooja", "Petpooja"),
                    ("gofrugal", "GoFrugal"),
                    ("webhook", "Generic Webhook"),
                    ("csv", "CSV Import"),
                    ("zapier", "Zapier"),
                    ("make", "Make"),
                    ("api", "Generic REST API"),
                ],
                max_length=32,
            ),
        ),
    ]
