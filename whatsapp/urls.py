from django.urls import path

from whatsapp import receivers, views

urlpatterns = [
    path("whatsapp/senders", views.SendersView.as_view(), name="whatsapp-senders"),
    path(
        "locations/<uuid:pk>/whatsapp-sender",
        views.LocationSenderView.as_view(),
        name="location-whatsapp-sender",
    ),
    path("whatsapp/templates", views.TemplatesView.as_view(), name="whatsapp-templates"),
    # One endpoint for Meta's GET handshake and POST delivery (spec 08, M-3).
    path("webhooks/whatsapp", receivers.WhatsAppWebhookView.as_view(), name="webhook-whatsapp"),
]
