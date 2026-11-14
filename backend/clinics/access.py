from rest_framework.exceptions import PermissionDenied

from .models import Membership, MembershipRole


class Capability:
    PATIENT_ADMIN = "patient_admin"
    PATIENT_READ = "patient_read"
    APPOINTMENT_ADMIN = "appointment_admin"
    APPOINTMENT_READ = "appointment_read"
    CHECK_IN = "check_in"
    EXAMINATION_READ = "examination_read"
    EXAMINATION_STATUS_READ = "examination_status_read"
    PATIENT_CLINICAL_HISTORY = "patient_clinical_history"
    ASSIGN_EXAMINATION = "assign_examination"
    ACQUIRE = "acquire"
    UPLOAD_IMAGE = "upload_image"
    VIEW_IMAGE = "view_image"
    INTERPRET = "interpret"
    SIGN_REPORT = "sign_report"
    RELEASE_REPORT = "release_report"
    REPORT_READ = "report_read"
    DELIVER_REPORT = "deliver_report"
    BILLING = "billing"
    BILLING_READ = "billing_read"
    BILLING_CONFIGURE = "billing_configure"
    BILLING_DISCOUNT = "billing_discount"
    BILLING_REFUND = "billing_refund"
    BILLING_CANCEL = "billing_cancel"
    CLINIC_ADMIN = "clinic_admin"
    OWNER_MANAGE = "owner_manage"
    MEMBERSHIP_READ = "membership_read"
    AUDIT_READ = "audit_read"
    SERVICE_CATALOGUE_READ = "service_catalogue_read"


ROLE_CAPABILITIES = {
    MembershipRole.Role.OWNER: {Capability.PATIENT_ADMIN, Capability.PATIENT_READ, Capability.APPOINTMENT_ADMIN, Capability.APPOINTMENT_READ, Capability.CHECK_IN, Capability.BILLING, Capability.BILLING_READ, Capability.BILLING_CONFIGURE, Capability.BILLING_DISCOUNT, Capability.BILLING_REFUND, Capability.BILLING_CANCEL, Capability.CLINIC_ADMIN, Capability.OWNER_MANAGE, Capability.MEMBERSHIP_READ, Capability.AUDIT_READ, Capability.ASSIGN_EXAMINATION, Capability.SERVICE_CATALOGUE_READ},
    MembershipRole.Role.ADMIN: {Capability.PATIENT_ADMIN, Capability.PATIENT_READ, Capability.APPOINTMENT_ADMIN, Capability.APPOINTMENT_READ, Capability.CHECK_IN, Capability.BILLING, Capability.BILLING_READ, Capability.BILLING_CONFIGURE, Capability.BILLING_DISCOUNT, Capability.BILLING_REFUND, Capability.BILLING_CANCEL, Capability.CLINIC_ADMIN, Capability.MEMBERSHIP_READ, Capability.AUDIT_READ, Capability.ASSIGN_EXAMINATION, Capability.SERVICE_CATALOGUE_READ},
    MembershipRole.Role.RECEPTIONIST: {Capability.PATIENT_ADMIN, Capability.PATIENT_READ, Capability.APPOINTMENT_ADMIN, Capability.APPOINTMENT_READ, Capability.CHECK_IN, Capability.EXAMINATION_STATUS_READ, Capability.BILLING, Capability.BILLING_READ, Capability.DELIVER_REPORT},
    MembershipRole.Role.TECHNICIAN: {Capability.ACQUIRE, Capability.UPLOAD_IMAGE, Capability.VIEW_IMAGE, Capability.EXAMINATION_READ},
    MembershipRole.Role.RADIOLOGIST: {Capability.PATIENT_READ, Capability.PATIENT_CLINICAL_HISTORY, Capability.APPOINTMENT_READ, Capability.ACQUIRE, Capability.UPLOAD_IMAGE, Capability.VIEW_IMAGE, Capability.EXAMINATION_READ, Capability.INTERPRET, Capability.SIGN_REPORT, Capability.RELEASE_REPORT, Capability.REPORT_READ, Capability.DELIVER_REPORT, Capability.ASSIGN_EXAMINATION, Capability.SERVICE_CATALOGUE_READ},
}


def clinic_membership_or_deny(user, clinic_id, capability=None):
    """Resolve active membership and capabilities from persisted server-side grants."""
    membership = Membership.objects.filter(
        user=user, clinic_id=clinic_id, clinic__is_active=True, is_active=True
    ).prefetch_related("role_grants").first()
    if membership is None:
        raise PermissionDenied("No active membership for this clinic.")
    if capability and not any(capability in ROLE_CAPABILITIES.get(grant.role, set()) for grant in membership.role_grants.filter(is_active=True)):
        raise PermissionDenied("Your clinic roles do not permit this action.")
    return membership


def clinic_queryset(queryset, membership):
    """Apply a tenant boundary before lookup, serialization, or storage-key access."""
    if not membership.is_active:
        raise PermissionDenied("Clinic membership is inactive.")
    return queryset.filter(clinic_id=membership.clinic_id)


def tenant_object_for_worker(model, *, clinic_id, object_id):
    """Background jobs must carry clinic_id and resolve objects through this scoped lookup."""
    return model.objects.get(clinic_id=clinic_id, pk=object_id)


def imaging_instance_for_user(*, user, clinic_id, instance_id):
    """Resolve a private storage reference only after membership and view authorization."""
    from .models import AuditEvent, ImagingInstance

    membership = clinic_membership_or_deny(user, clinic_id, Capability.VIEW_IMAGE)
    instance = clinic_queryset(ImagingInstance.objects.select_related("series__study", "uploaded_by"), membership).get(pk=instance_id)
    event = AuditEvent(
        clinic_id=clinic_id, actor=user, action="imaging.instance_accessed",
        object_type="ImagingInstance", object_id=str(instance.pk),
    )
    event._domain_service_write = True
    event.save()
    return instance
