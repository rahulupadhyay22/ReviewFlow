from django.db import migrations

from core.rls import rls_via_parent


class Migration(migrations.Migration):
    dependencies = [("google_reviews", "0005_googlelocation")]

    operations = [rls_via_parent("google_reviews_googlelocation", "location_id", "locations_location")]
