from rest_framework import serializers


class OptOutSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    opted_out = serializers.BooleanField()
    opted_out_at = serializers.DateTimeField()
