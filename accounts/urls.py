from django.urls import path

from accounts import views

urlpatterns = [
    path("auth/login", views.LoginView.as_view(), name="auth-login"),
    path("auth/login/totp", views.LoginTotpView.as_view(), name="auth-login-totp"),
    path("auth/logout", views.LogoutView.as_view(), name="auth-logout"),
    path("auth/refresh", views.RefreshView.as_view(), name="auth-refresh"),
    path("auth/accept-invite", views.AcceptInviteView.as_view(), name="auth-accept-invite"),
    path("auth/2fa/setup", views.TotpSetupView.as_view(), name="auth-2fa-setup"),
    path("auth/2fa/confirm", views.TotpConfirmView.as_view(), name="auth-2fa-confirm"),
    path("auth/2fa/disable", views.TotpDisableView.as_view(), name="auth-2fa-disable"),
    path("merchant", views.MerchantView.as_view(), name="merchant"),
    path("team-members", views.TeamMemberListView.as_view(), name="team-members"),
    path(
        "team-members/<uuid:pk>",
        views.TeamMemberDetailView.as_view(),
        name="team-member-detail",
    ),
]
