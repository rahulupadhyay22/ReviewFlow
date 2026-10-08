from django.db import migrations

from core.rls import rls_platform_insert


class Migration(migrations.Migration):
    dependencies = [("auditlog", "0002_auditlog_rls")]

    operations = [rls_platform_insert("auditlog_auditlog")]
