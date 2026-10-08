"""Manual opt-out (spec 08, Business-Rules §3). Logic is in customers.services."""
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerAdminOrManager
from customers import services
from customers.serializers import OptOutSerializer


class CustomerOptOutView(APIView):
    # Merchant-wide for a MANAGER by design (OD-7): Customer has no location
    # link, and this endpoint can only opt a customer out, never back in.
    permission_classes = [IsOwnerAdminOrManager]

    def post(self, request, pk):
        customer = services.get_customer(pk)
        customer = services.opt_out_customer(customer=customer, source="MANUAL", actor=request.user)
        return Response(OptOutSerializer(customer).data)
