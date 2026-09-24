from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("accounts.urls")),
    path("api/v1/", include("locations.urls")),
    path("api/v1/", include("integrations.urls")),
    path("api/v1/", include("transactions.urls")),
    path("api/v1/", include("apikeys.urls")),
]
