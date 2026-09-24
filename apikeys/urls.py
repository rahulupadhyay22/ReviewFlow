from django.urls import path

from apikeys import views

urlpatterns = [
    path("api-keys", views.ApiKeyListView.as_view(), name="api-keys"),
    path("api-keys/<uuid:pk>", views.ApiKeyDetailView.as_view(), name="api-key-detail"),
]
