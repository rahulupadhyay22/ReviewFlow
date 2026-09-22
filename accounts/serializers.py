from rest_framework import serializers

from accounts.models import Merchant


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False)


class MerchantSerializer(serializers.ModelSerializer):
    plan = serializers.SerializerMethodField()  # Plan arrives in Phase 07

    class Meta:
        model = Merchant
        fields = ["id", "name", "business_type", "timezone", "plan", "status"]

    def get_plan(self, merchant):
        return None


class MerchantUpdateSerializer(serializers.Serializer):
    """Only these fields are writable; merchant_id/status/plan are ignored."""

    name = serializers.CharField(max_length=255, required=False)
    business_type = serializers.CharField(
        max_length=100, required=False, allow_null=True, allow_blank=True
    )
    timezone = serializers.CharField(max_length=64, required=False)


def session_body(team_member):
    return {
        "user": {"id": str(team_member.user_id), "email": team_member.user.email},
        "merchant": {"id": str(team_member.merchant_id), "name": team_member.merchant.name},
        "role": team_member.role,
    }
