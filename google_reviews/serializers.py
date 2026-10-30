"""Request/response shapes only (Coding-Standards.md section 1). Tokens, the
authorization code, the ID token and google_account_sub are never serialized.

The location serializer reads only fields already loaded on the instance: it is
used by a view that has no request-wide transaction, where a lazy foreign-key
query would run without SET LOCAL and find nothing."""
from rest_framework import serializers

from google_reviews.models import GoogleConnection, GoogleLocation, GoogleOAuthState, GoogleReview


class OAuthCompleteSerializer(serializers.Serializer):
    # Shape only. Every rule about the state is google_reviews.services.
    code = serializers.CharField(max_length=2048)
    state = serializers.CharField(max_length=512)


class OAuthAttemptSerializer(serializers.ModelSerializer):
    connection_id = serializers.UUIDField(source="google_connection_id", allow_null=True)

    class Meta:
        model = GoogleOAuthState
        fields = ("id", "status", "failure_code", "connection_id")


class ConnectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = GoogleConnection
        fields = ("id", "google_account_email", "status", "connected_at")


class MapLocationSerializer(serializers.Serializer):
    google_connection_id = serializers.UUIDField()
    google_location_id = serializers.CharField(max_length=255)


class GoogleLocationSerializer(serializers.ModelSerializer):
    connection_status = serializers.CharField(source="google_connection.status")
    average_rating = serializers.DecimalField(
        max_digits=2, decimal_places=1, coerce_to_string=False, allow_null=True
    )

    class Meta:
        model = GoogleLocation
        fields = (
            "id",
            "google_location_id",
            "google_place_id",
            "review_link",
            "average_rating",
            "review_count",
            "last_synced_at",
            "connection_status",
        )


class ReviewFilterSerializer(serializers.Serializer):
    location_id = serializers.UUIDField(required=False)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    rating_min = serializers.IntegerField(required=False, min_value=1, max_value=5)


class ReviewSerializer(serializers.ModelSerializer):
    # No attribution field, and no reply_text / reply_status (V2).
    location_id = serializers.UUIDField(source="google_location.location_id")

    class Meta:
        model = GoogleReview
        fields = (
            "id",
            "location_id",
            "google_review_id",
            "reviewer_name",
            "rating",
            "text",
            "review_created_at",
        )
