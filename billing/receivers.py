"""Razorpay billing webhook receiver (spec 07 "Webhook design").

Thin: every rule lives in billing.services.receive_webhook. This view only
wires up the unauthenticated posture a provider webhook needs
(Authentication.md §3: the signature is the only authentication, never a
session or an API key) and the per-IP throttle, which DRF applies before the
handler runs.
"""
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from billing import services
from integrations.throttling import WebhookIpRateThrottle


@method_decorator(csrf_exempt, name="dispatch")
class RazorpayWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [WebhookIpRateThrottle]

    def post(self, request):
        services.receive_webhook(request=request)
        return Response({}, status=200)
