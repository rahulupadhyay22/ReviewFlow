"""Provider webhook receivers (spec 06 Decisions 1, 11, 15).

Thin: every rule lives in integrations.services.receive_webhook. These
views only wire up the always-unauthenticated posture every provider
webhook needs (Authentication.md §3: verified in the adapter, never via
session or API key) and the per-IP throttle.
"""
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations import services
from integrations.throttling import WebhookIpRateThrottle


@method_decorator(csrf_exempt, name="dispatch")
class GenericWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [WebhookIpRateThrottle]

    def post(self, request, integration_id):
        services.receive_webhook(provider="webhook", integration_id=integration_id, request=request)
        return Response({}, status=200)
