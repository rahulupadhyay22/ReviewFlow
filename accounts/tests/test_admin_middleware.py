"""Admin requests are never tenant-wrapped by the dashboard session
(spec 16; plan step 2), while dashboard API requests still are."""
import uuid

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from accounts.middleware import SessionMerchantMiddleware


class _User(AnonymousUser):
    is_authenticated = True


def _run(path):
    seen = {}

    def get_response(request):
        seen["merchant_id"] = getattr(request, "merchant_id", None)

    request = RequestFactory().get(path)
    request.session = {"merchant_id": str(uuid.uuid4())}
    request.user = _User()
    SessionMerchantMiddleware(get_response)(request)
    return seen["merchant_id"]


@pytest.mark.parametrize("path", ["/admin/", "/admin/login/", "/admin/accounts/merchant/"])
def test_admin_paths_never_get_the_session_merchant(path):
    assert _run(path) is None


def test_dashboard_api_paths_still_get_the_session_merchant():
    assert _run("/api/v1/merchant") is not None
