"""
DRF exception handler: every API error uses the standard shape
{"error": {"code", "message", "field_errors"?}} (API-Specification.md
§General Conventions).
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.serializers import as_serializer_error
from rest_framework.views import exception_handler as drf_exception_handler

from core.exceptions import ReviewFlowError


def _error(code, message, http_status, field_errors=None):
    body = {"code": code, "message": message}
    if field_errors is not None:
        body["field_errors"] = field_errors
    return Response({"error": body}, status=http_status)


def exception_handler(exc, context):
    # Domain errors that declare an HTTP mapping (e.g. InvalidCredentials -> 401).
    # Handled here because DRF downgrades AuthenticationFailed to 403 under
    # session auth.
    if isinstance(exc, ReviewFlowError) and hasattr(exc, "http_status"):
        return _error(exc.code, str(exc), exc.http_status)

    if isinstance(exc, DjangoValidationError):
        exc = exceptions.ValidationError(as_serializer_error(exc))

    response = drf_exception_handler(exc, context)
    if response is None:
        return None  # unhandled -> 500

    if isinstance(exc, exceptions.ValidationError):
        detail = exc.detail if isinstance(exc.detail, dict) else {"non_field_errors": exc.detail}
        return _error(
            "validation_error", "Invalid input.", status.HTTP_422_UNPROCESSABLE_ENTITY, detail
        )

    error = _error(exc.default_code, str(exc.detail), response.status_code)
    for header in ("Retry-After", "Allow", "WWW-Authenticate"):
        if header in response:
            error[header] = response[header]
    return error
