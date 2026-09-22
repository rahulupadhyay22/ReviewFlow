"""core.api.exception_handler: standard error shape, and only the Retry-After
and Allow headers are carried over from DRF's response."""
from rest_framework import exceptions

from core.api import exception_handler


def test_throttled_keeps_retry_after_header_only():
    resp = exception_handler(exceptions.Throttled(wait=42), {})
    assert resp.status_code == 429
    assert resp["Retry-After"] == "42"
    assert "WWW-Authenticate" not in resp
    assert resp.data["error"]["code"] == "throttled"


def test_other_drf_headers_are_not_copied(monkeypatch):
    from rest_framework import views

    original = views.exception_handler

    def with_extra_header(exc, context):
        response = original(exc, context)
        response["X-Leaky"] = "1"
        response["Allow"] = "GET"
        return response

    monkeypatch.setattr("core.api.drf_exception_handler", with_extra_header)
    resp = exception_handler(exceptions.MethodNotAllowed("POST"), {})
    assert resp.status_code == 405
    assert resp["Allow"] == "GET"
    assert "X-Leaky" not in resp
