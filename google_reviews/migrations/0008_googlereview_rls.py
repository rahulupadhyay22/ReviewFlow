from django.db import migrations

from core.rls import rls_via_parent


class Migration(migrations.Migration):
    dependencies = [("google_reviews", "0007_googlereview")]

    operations = [
        rls_via_parent("google_reviews_googlereview", "google_location_id", "google_reviews_googlelocation")
    ]
