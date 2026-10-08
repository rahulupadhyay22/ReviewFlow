from django.urls import path

from customers import views

urlpatterns = [
    path("customers/<uuid:pk>/opt-out", views.CustomerOptOutView.as_view(), name="customer-opt-out"),
]
