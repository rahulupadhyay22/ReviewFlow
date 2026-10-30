import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Spec 09-google-business-profile: GoogleLocation. UNIQUE(location) comes
    from the OneToOneField; UNIQUE(google_connection, google_location_id) is
    OD-7. google_location_id and google_place_id are separate columns, and
    review_link is the newReviewUri that Google returns (RL-1).

    Rollback: reversing drops the table; mappings are lost and merchants map
    their locations again."""

    dependencies = [
        ("locations", "0002_location_rls"),
        ("google_reviews", "0004_googleoauthstate_rls"),
    ]

    operations = [
        migrations.CreateModel(
            name="GoogleLocation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("google_location_id", models.CharField(max_length=255)),
                ("google_place_id", models.CharField(blank=True, max_length=255, null=True)),
                ("review_link", models.URLField(max_length=2048)),
                ("average_rating", models.DecimalField(blank=True, decimal_places=1, max_digits=2, null=True)),
                ("review_count", models.IntegerField(blank=True, null=True)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                (
                    "google_connection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="google_locations",
                        to="google_reviews.googleconnection",
                    ),
                ),
                (
                    "location",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="google_location",
                        to="locations.location",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="googlelocation",
            constraint=models.UniqueConstraint(
                fields=("google_connection", "google_location_id"), name="google_location_unique_per_connection"
            ),
        ),
    ]
