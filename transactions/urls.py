from django.urls import path

from transactions import views

urlpatterns = [
    path("transactions", views.TransactionListView.as_view(), name="transactions"),
    path("transactions/<uuid:pk>", views.TransactionDetailView.as_view(), name="transaction-detail"),
]
