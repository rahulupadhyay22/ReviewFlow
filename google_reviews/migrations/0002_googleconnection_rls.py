from django.db import migrations

from core.rls import rls_direct


class Migration(migrations.Migration):
    dependencies = [("google_reviews", "0001_googleconnection")]

    operations = [rls_direct("google_reviews_googleconnection")]
