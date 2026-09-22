import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_user_totp'),
        ('locations', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='TeamMemberLocation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('location', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='team_member_assignments', to='locations.location')),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.merchant')),
                ('team_member', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='location_assignments', to='accounts.teammember')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('team_member', 'location'), name='uniq_teammemberlocation_member_location')],
            },
        ),
    ]
