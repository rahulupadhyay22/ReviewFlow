"""Test-only URLconf for the TenantMiddleware exemption tests (spec
08-whatsapp-embedded-signup P6a). Selected with
`@override_settings(ROOT_URLCONF="core.tests.urls_middleware")`.

It is never included by config/urls.py, so the production route
`whatsapp-embedded-signup-complete` does not exist until P6b. The middleware
tests call the middleware directly, so these views are never executed; they
exist only so `resolve()` can map a path to a url_name.
"""
from django.http import HttpResponse
from django.urls import path


def _view(request):  # pragma: no cover - never executed
    return HttpResponse("unused")


urlpatterns = [
    path("signup/complete", _view, name="whatsapp-embedded-signup-complete"),
    path("signup/other", _view, name="some-other-session-endpoint"),
]
