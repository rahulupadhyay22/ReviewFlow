"""Transactions API serializers (API-Specification.md §Transactions).

customer is null for a transaction recorded without a customer phone
(spec Decision 17). There is no campaign_execution field until Phase 10."""
from rest_framework import serializers

from transactions.models import Transaction


class CustomerSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField(allow_null=True)
    phone = serializers.CharField()


class TransactionSerializer(serializers.ModelSerializer):
    location_id = serializers.UUIDField(read_only=True)
    integration_id = serializers.UUIDField(read_only=True, allow_null=True)
    customer = CustomerSummarySerializer(read_only=True)

    class Meta:
        model = Transaction
        fields = [
            "id",
            "location_id",
            "integration_id",
            "external_transaction_id",
            "amount",
            "currency",
            "payment_method",
            "status",
            "occurred_at",
            "created_at",
            "customer",
        ]


class TransactionFilterSerializer(serializers.Serializer):
    location_id = serializers.UUIDField(required=False)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    status = serializers.ChoiceField(choices=Transaction.Status.choices, required=False)
