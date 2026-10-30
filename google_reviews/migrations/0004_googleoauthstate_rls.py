from django.db import migrations

from core.rls import rls_direct


class Migration(migrations.Migration):
    dependencies = [("google_reviews", "0003_googleoauthstate")]

    operations = [rls_direct("google_reviews_googleoauthstate")]
