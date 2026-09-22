import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('accounts', '0003_user_totp'),
    ]

    operations = [
        migrations.CreateModel(
            name='Location',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=255)),
                ('address', models.CharField(blank=True, max_length=500, null=True)),
                ('phone', models.CharField(blank=True, max_length=32, null=True)),
                ('timezone', models.CharField(blank=True, max_length=64, null=True)),
                ('is_active', models.BooleanField(default=True)),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='locations', to='accounts.merchant')),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
