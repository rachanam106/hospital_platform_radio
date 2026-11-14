import logging

from django.http import JsonResponse


security_logger = logging.getLogger("django.security.api")


class ApiExceptionMiddleware:
    """Return a generic API error and log only exception type, never request data."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except Exception as exc:
            if request.path.startswith("/api/v1/"):
                security_logger.error("Unhandled API exception type=%s", type(exc).__name__)
                return JsonResponse(
                    {"error": {"code": "internal_error", "message": "An internal error occurred."}},
                    status=500,
                )
            raise


class FirstLoginPasswordChangeMiddleware:
    """Restrict newly provisioned staff sessions to account setup until password rotation."""
    allowed_paths = {
        "/api/v1/auth/me/",
        "/api/v1/auth/logout/",
        "/api/v1/auth/password/change/",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/api/v1/") and request.path not in self.allowed_paths:
            user = getattr(request, "user", None)
            if user is not None and user.is_authenticated:
                from clinics.models import AccountSecurityState
                if AccountSecurityState.objects.filter(user_id=user.pk, must_change_password=True).exists():
                    return JsonResponse(
                        {"error": {"code": "password_change_required", "message": "Change your temporary password to continue."}},
                        status=403,
                    )
        return self.get_response(request)
