"""Integrations API serializers (API-Specification.md §Integrations).

merchant_id is never accepted from the client; every write derives it from
the session (view -> service -> tenant context)."""
from rest_framework import serializers

from integrations.models import Integration, IntegrationLocationMapping


def _validate_json_object(value):
    if value is not None and not isinstance(value, dict):
        raise serializers.ValidationError("Must be a JSON object.")
    return value


class MappingSerializer(serializers.ModelSerializer):
    location_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = IntegrationLocationMapping
        fields = ["id", "location_id", "config_json", "is_active"]


class IntegrationSerializer(serializers.ModelSerializer):
    mappings = MappingSerializer(source="location_mappings", many=True, read_only=True)
    has_credentials = serializers.SerializerMethodField()

    class Meta:
        model = Integration
        fields = [
            "id",
            "provider",
            "status",
            "config_json",
            "has_credentials",
            "mappings",
            "created_at",
            "updated_at",
        ]

    def get_has_credentials(self, obj):
        return bool(obj.credentials_encrypted)


class ConnectSerializer(serializers.Serializer):
    credentials = serializers.JSONField(required=False, allow_null=True, validators=[_validate_json_object])
    config_json = serializers.JSONField(required=False, allow_null=True, validators=[_validate_json_object])


class IntegrationConfigSerializer(serializers.Serializer):
    config_json = serializers.JSONField(allow_null=True, validators=[_validate_json_object])


class AddMappingSerializer(serializers.Serializer):
    location_id = serializers.UUIDField()
    config_json = serializers.JSONField(required=False, allow_null=True, validators=[_validate_json_object])
    is_active = serializers.BooleanField(required=False, default=True)


class MappingItemSerializer(serializers.Serializer):
    location_id = serializers.UUIDField()
    config_json = serializers.JSONField(required=False, allow_null=True, validators=[_validate_json_object])
    is_active = serializers.BooleanField(required=False, default=True)


class ReplaceMappingsSerializer(serializers.Serializer):
    mappings = MappingItemSerializer(many=True)
