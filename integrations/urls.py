from django.urls import path

from integrations import receivers, views

urlpatterns = [
    path(
        "integrations/<str:provider>/connect",
        views.IntegrationConnectView.as_view(),
        name="integration-connect",
    ),
    path("integrations", views.IntegrationListView.as_view(), name="integrations"),
    path("integrations/<uuid:pk>", views.IntegrationDetailView.as_view(), name="integration-detail"),
    path(
        "integrations/<uuid:pk>/locations",
        views.IntegrationLocationsView.as_view(),
        name="integration-locations",
    ),
    path(
        "integrations/<uuid:pk>/csv-imports",
        views.CsvImportView.as_view(),
        name="integration-csv-imports",
    ),
    # <str:integration_id>, not <uuid:...>: a malformed id must reach the
    # view and get the identical 401 (spec 06 Decision 1), not a generic
    # Django 404 from the URL converter rejecting it first.
    path(
        "webhooks/generic/<str:integration_id>",
        receivers.GenericWebhookView.as_view(),
        name="webhook-generic",
    ),
    # <str:integration_id>, same reasoning as webhook-generic above: a
    # malformed id must reach the view and get the identical 401 (spec
    # 06-shopify-app Decision 1), not a generic 404 from the URL converter.
    path(
        "webhooks/shopify/<str:integration_id>",
        receivers.ShopifyWebhookView.as_view(),
        name="webhook-shopify",
    ),
    path("sales", views.SalesView.as_view(), name="sales"),
    # Shopify app installation (spec 06-shopify-app Decision 3). No
    # merchant-entered shop domain anywhere -- `shop` comes only from
    # Shopify's own redirect to /install.
    path("integrations/shopify/install", views.ShopifyInstallView.as_view(), name="shopify-install"),
    path("integrations/shopify/callback", views.ShopifyCallbackView.as_view(), name="shopify-callback"),
    path("integrations/shopify/pending", views.ShopifyPendingView.as_view(), name="shopify-pending"),
    path("integrations/shopify/link", views.ShopifyLinkView.as_view(), name="shopify-link"),
]
