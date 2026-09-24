from rest_framework import serializers

from apikeys.models import ApiKey


class ApiKeySerializer(serializers.ModelSerializer):
    """Never includes key_hash or the plaintext -- both are excluded by
    listing exactly these fields (spec: "Never key_hash, never the
    plaintext")."""

    scopes = serializers.ListField(source="scopes_json", read_only=True)

    class Meta:
        model = ApiKey
        fields = ["id", "scopes", "is_active", "created_at", "last_used_at"]
        read_only_fields = fields


class ApiKeyCreateSerializer(serializers.Serializer):
    """Only scopes is writable; merchant_id/id/is_active/key_hash in the
    body are ignored (apikeys.views.ApiKeyListView.post never reads them).
    services.create_api_key() re-validates scopes independently -- this is
    a fast client-facing check, not the sole guard."""

    scopes = serializers.ListField(
        child=serializers.ChoiceField(choices=ApiKey.Scope.choices), allow_empty=False
    )

    def validate_scopes(self, scopes):
        if len(set(scopes)) != len(scopes):
            raise serializers.ValidationError("Duplicate scopes are not allowed.")
        return scopes
