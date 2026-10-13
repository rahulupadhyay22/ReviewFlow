"""Request/response shapes only (Coding-Standards.md §1). Provider ids
(phone_number_id, business_account_id) are never serialized."""
from rest_framework import serializers

from whatsapp.models import MessageTemplate, WhatsAppAccount


class SenderSerializer(serializers.ModelSerializer):
    class Meta:
        model = WhatsAppAccount
        fields = ("id", "sender_type", "status")


class TemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = MessageTemplate
        fields = ("id", "name", "language", "body", "status", "created_at", "updated_at")


class TemplateCreateSerializer(serializers.Serializer):
    # Shape only; every content rule is whatsapp.services.validate_template.
    name = serializers.CharField(max_length=255)
    language = serializers.CharField(max_length=16)
    body = serializers.CharField()
    # Accepted only so the service can refuse an own-number template with 422
    # until E-7b is verified; otherwise DRF would silently drop it and create
    # a shared template.
    whatsapp_account_id = serializers.UUIDField(required=False, allow_null=True, default=None)


class SetSenderSerializer(serializers.Serializer):
    whatsapp_account_id = serializers.UUIDField()
