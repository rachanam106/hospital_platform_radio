from django.db import IntegrityError
from rest_framework import status
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.response import Response


def _validation_message(data):
    """Summarize safe serializer/domain validation messages for the UI."""
    messages = []

    def collect(value, field=None):
        if isinstance(value, dict):
            for key, child in value.items():
                collect(child, str(key))
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child, field)
        elif value is not None:
            message = str(value).strip()
            if message:
                messages.append(f"{field}: {message}" if field and field not in {"non_field_errors", "__all__"} else message)

    collect(data)
    return "; ".join(dict.fromkeys(messages))[:400]


def api_exception_handler(exc, context):
    if isinstance(exc, IntegrityError):
        return Response(
            {"error": {"code": "conflict", "message": "The request conflicts with current resource state."}},
            status=status.HTTP_409_CONFLICT,
        )
    response = drf_exception_handler(exc, context)
    if response is None:
        return None
    data = response.data
    if isinstance(data, dict) and "detail" in data:
        message = str(data["detail"])
        details = None
    else:
        message = _validation_message(data) or "The request could not be validated."
        details = data
    detail_code = getattr(data.get("detail"), "code", None) if isinstance(data, dict) else None
    code = detail_code or getattr(exc, "default_code", "request_error")
    body = {"error": {"code": str(code), "message": message}}
    if details is not None:
        body["error"]["details"] = details
    response.data = body
    return response
