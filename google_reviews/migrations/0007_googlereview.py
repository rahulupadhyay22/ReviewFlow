import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Spec 09-google-business-profile: GoogleReview. There are no reply_text /
    reply_status columns (Status: V2 - Planned. V1 Implementation: NO). The
    unique constraint has no condition so that it can be the ON CONFLICT target
    of the sync upsert.

    Rollback: reversing drops the table; synced reviews are lost and are
    fetched again by the next sync."""

    dependencies = [("google_reviews", "0006_googlelocation_rls")]

    operations = [
        migrations.CreateModel(
            name="GoogleReview",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("google_review_id", models.CharField(max_length=255)),
                ("reviewer_name", models.CharField(blank=True, max_length=255, null=True)),
                ("rating", models.SmallIntegerField()),
                ("text", models.TextField(blank=True, null=True)),
                ("review_created_at", models.DateTimeField()),
                ("fetched_at", models.DateTimeField()),
                (
                    "google_location",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reviews",
                        to="google_reviews.googlelocation",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="googlereview",
            index=models.Index(fields=["google_location", "review_created_at"], name="google_review_loc_created_idx"),
        ),
        migrations.AddConstraint(
            model_name="googlereview",
            constraint=models.UniqueConstraint(
                fields=("google_location", "google_review_id"), name="google_review_location_review_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="googlereview",
            constraint=models.CheckConstraint(
                condition=models.Q(("rating__gte", 1), ("rating__lte", 5)), name="google_review_rating_1_5"
            ),
        ),
    ]
