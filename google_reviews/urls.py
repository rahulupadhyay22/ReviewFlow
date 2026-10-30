from django.urls import path

from google_reviews import views

urlpatterns = [
    path("google/oauth/start", views.OAuthStartView.as_view(), name="google-oauth-start"),
    path("google/oauth/complete", views.OAuthCompleteView.as_view(), name="google-oauth-complete"),
    path("google/oauth/attempts/<uuid:pk>", views.OAuthAttemptView.as_view(), name="google-oauth-attempt"),
    path("google/connections", views.ConnectionsView.as_view(), name="google-connections"),
    # GET is exempt from the request-wide transaction (core.middleware, P-1).
    path(
        "google/connections/<uuid:pk>/locations",
        views.GoogleConnectionLocationsView.as_view(),
        name="google-connection-locations",
    ),
    # PUT is exempt from the request-wide transaction (core.middleware, P-1).
    path(
        "locations/<uuid:pk>/google-location",
        views.LocationGoogleLocationView.as_view(),
        name="location-google-location",
    ),
    path("reviews", views.ReviewsView.as_view(), name="reviews"),
]
