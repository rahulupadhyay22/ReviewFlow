from django.urls import path

from locations import views

urlpatterns = [
    path("locations", views.LocationListView.as_view(), name="locations"),
    path("locations/<uuid:pk>", views.LocationDetailView.as_view(), name="location-detail"),
]
