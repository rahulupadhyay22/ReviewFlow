"""Meta's single WhatsApp webhook endpoint (spec 08, amended 2026-10-07).

Meta delivers inbound messages and status notifications through one app-level
`messages` callback, verified with a GET handshake at the same address, so one
view serves both. Thin: every rule lives in whatsapp.services. This view only
wires up the unauthenticated posture a provider webhook needs (the signature
or verify token is the only authentication, never a session or an API key),
the per-IP throttle that DRF applies before the handler runs, and the raw
body the signature covers. csrf_exempt: Meta cannot send a CSRF token.
"""
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.throttling import WebhookIpRateThrottle
from whatsapp import services


@method_decorator(csrf_exempt, name="dispatch")
class WhatsAppWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [WebhookIpRateThrottle]

    def get(self, request):
        params = request.query_params
        challenge = services.verify_subscription(
            mode=params.get("hub.mode"),
            verify_token=params.get("hub.verify_token"),
            challenge=params.get("hub.challenge"),
        )
        # Meta expects the bare challenge value, not JSON.
        return HttpResponse(challenge, content_type="text/plain", status=200)

    def post(self, request):
        services.receive_webhook(request.body, request.headers.get("X-Hub-Signature-256"))
        return Response({}, status=200)
