from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.exceptions import ObjectDoesNotExist
from rest_framework.exceptions import NotFound, ValidationError


def call_domain(func, /, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except DjangoValidationError as exc:
        detail = getattr(exc, "message_dict", None) or getattr(exc, "messages", None) or str(exc)
        raise ValidationError(detail) from exc
    except ObjectDoesNotExist as exc:
        raise NotFound("Resource not found in the active clinic.") from exc
