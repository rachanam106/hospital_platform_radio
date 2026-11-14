from rest_framework.exceptions import NotAuthenticated, PermissionDenied, ValidationError
from rest_framework.permissions import BasePermission

from clinics.access import clinic_membership_or_deny


class ActiveClinicContext(BasePermission):
    """Resolve the session's active clinic from persisted membership on every request."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            raise NotAuthenticated()
        clinic_id = request.session.get("active_clinic_id")
        if not clinic_id:
            raise ValidationError({"active_clinic": "Select an active clinic before using clinic resources."})
        try:
            membership = clinic_membership_or_deny(request.user, clinic_id)
        except PermissionDenied:
            request.session.pop("active_clinic_id", None)
            raise PermissionDenied("Active clinic context is unavailable.")
        request.membership = membership
        request.clinic = membership.clinic
        return True


class CapabilityPermission(BasePermission):
    def has_permission(self, request, view):
        required = view.get_required_capability(request) if hasattr(view, "get_required_capability") else getattr(view, "required_capability", None)
        if required is None:
            return True
        try:
            clinic_membership_or_deny(request.user, request.clinic.pk, required)
        except PermissionDenied:
            raise
        return True
