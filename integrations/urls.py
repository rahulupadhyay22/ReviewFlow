from django.urls import path

from integrations import views

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
]
