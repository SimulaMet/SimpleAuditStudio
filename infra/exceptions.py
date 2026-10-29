"""API exception handling with stable error codes."""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import exception_handler


class StableAPIError(APIException):
    default_code = "error"
    default_detail = "An unexpected error occurred."

    def __init__(self, detail=None, code=None, http_status=status.HTTP_400_BAD_REQUEST):
        super().__init__(detail or self.default_detail)
        self.status_code = http_status
        if code:
            self.code = code


def _stable_payload(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def api_exception_handler(exc, context):
    # Django's core ValidationError (e.g. from validate_password) is not
    # handled by DRF's default handler. Map it to a clean 400 response.
    if isinstance(exc, DjangoValidationError):
        messages = list(exc.messages)
        message = " ".join(messages) if messages else str(exc)
        return Response(
            _stable_payload("invalid_password", message),
            status=status.HTTP_400_BAD_REQUEST,
        )
    # A unique-constraint violation (e.g. duplicate scenario key or endpoint
    # display name) is a client conflict, not a server fault. Map it to a
    # clean 409 instead of leaking a raw 500 with a stack trace.
    if isinstance(exc, IntegrityError):
        return Response(
            _stable_payload("conflict", "A record with these values already exists."),
            status=status.HTTP_409_CONFLICT,
        )
    response = exception_handler(exc, context)
    if response is not None:
        payload = {
            "error": {
                "code": getattr(exc, "code", None) or getattr(exc, "default_code", "error"),
                "message": response.data.get("detail", str(response.data)),
            }
        }
        if isinstance(response.data, dict) and "detail" in response.data:
            payload["error"]["message"] = response.data["detail"]
        response.data = payload
    return response
