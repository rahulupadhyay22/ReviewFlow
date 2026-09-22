from rest_framework import serializers

from accounts.models import Merchant, TeamMember


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
        "user": {
            "id": str(team_member.user_id),
            "email": team_member.user.email,
            "totp_enabled": team_member.user.is_totp_enabled,
        },
        "merchant": {"id": str(team_member.merchant_id), "name": team_member.merchant.name},
        "role": team_member.role,
    }


class TotpCodeSerializer(serializers.Serializer):
    code = serializers.CharField(trim_whitespace=False)


class TotpSetupSerializer(serializers.Serializer):
    password = serializers.CharField(trim_whitespace=False)


class TotpDisableSerializer(serializers.Serializer):
    password = serializers.CharField(trim_whitespace=False)
    code = serializers.CharField(trim_whitespace=False)


class TeamMemberUserSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    email = serializers.EmailField()


class TeamMemberSerializer(serializers.ModelSerializer):
    user = TeamMemberUserSerializer()

    class Meta:
        model = TeamMember
        fields = ["id", "user", "role", "invited_at", "accepted_at"]


class TeamMemberInviteSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=TeamMember.Role.choices)


class TeamMemberRoleSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=TeamMember.Role.choices)


class AcceptInviteSerializer(serializers.Serializer):
    token = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)
