from django.urls import path

from accounts import views

urlpatterns = [
    path("auth/login", views.LoginView.as_view(), name="auth-login"),
    path("auth/logout", views.LogoutView.as_view(), name="auth-logout"),
    path("auth/refresh", views.RefreshView.as_view(), name="auth-refresh"),
    path("auth/accept-invite", views.AcceptInviteView.as_view(), name="auth-accept-invite"),
    path("merchant", views.MerchantView.as_view(), name="merchant"),
    path("team-members", views.TeamMemberListView.as_view(), name="team-members"),
    path(
        "team-members/<uuid:pk>",
        views.TeamMemberDetailView.as_view(),
        name="team-member-detail",
    ),
]
