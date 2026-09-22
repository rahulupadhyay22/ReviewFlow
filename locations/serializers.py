from rest_framework import serializers

from locations.models import Location


class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ["id", "name", "address", "phone", "timezone", "is_active", "created_at", "updated_at"]


class LocationCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, allow_blank=False, trim_whitespace=True)
    address = serializers.CharField(max_length=500, required=False, allow_null=True, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_null=True, allow_blank=True)
    timezone = serializers.CharField(max_length=64, required=False, allow_null=True, allow_blank=True)


class LocationUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(
        max_length=255, allow_blank=False, trim_whitespace=True, required=False
    )
    address = serializers.CharField(
        max_length=500, required=False, allow_null=True, allow_blank=True
    )
    phone = serializers.CharField(
        max_length=32, required=False, allow_null=True, allow_blank=True
    )
    timezone = serializers.CharField(
        max_length=64, required=False, allow_null=True, allow_blank=True
    )
