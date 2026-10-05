from django.urls import path

from billing import receivers, views

urlpatterns = [
    path("billing/plans", views.PlanListView.as_view(), name="billing-plans"),
    path("billing/subscription", views.SubscriptionView.as_view(), name="billing-subscription"),
    path(
        "billing/subscription/cancel",
        views.SubscriptionCancelView.as_view(),
        name="billing-subscription-cancel",
    ),
    path("billing/checkout", views.CheckoutView.as_view(), name="billing-checkout"),
    path(
        "billing/webhooks/razorpay",
        receivers.RazorpayWebhookView.as_view(),
        name="webhook-billing-razorpay",
    ),
]
