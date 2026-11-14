"""Transactional business operations. Write workflows through these functions."""
import re
import secrets
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework.exceptions import PermissionDenied

from .access import Capability, ROLE_CAPABILITIES
from .models import (
    AIAnalysis, AIReportAssist, AccountSecurityState, ClinicalHistoryEntry, Appointment, AuditEvent, AuthenticationEvent, Clinic, Examination, ExaminationAssignment, ImagingInstance,
    ImagingSeries, ImagingStudy, Invoice, Membership, MembershipRole,
    Patient, Payment, Report, ChargeItem, ClinicalService, InvoiceLineItem, Refund,
)
from .reporting import REPORT_TEMPLATES, template_for_modality
from .ai_assistance import build_assist
from .symptom_extraction import extract_symptoms


def _authorize(membership, actor, clinic_id, capability):
    if not membership or not actor:
        raise ValidationError("An active membership for the acting user is required.")
    current = Membership.objects.select_for_update().select_related("clinic").filter(
        pk=membership.pk, user_id=actor.pk, clinic_id=clinic_id,
        is_active=True, clinic__is_active=True,
    ).first()
    if current is None:
        raise ValidationError("Membership does not authorize this clinic.")
    if not any(capability in ROLE_CAPABILITIES.get(role, set()) for role in current.roles()):
        raise ValidationError("Clinic role does not permit this operation.")
    return current


def _audit(clinic_id, actor, action, obj, details=None):
    event = AuditEvent(
        clinic_id=clinic_id, actor=actor, action=action,
        object_type=obj.__class__.__name__, object_id=str(obj.pk), details=details or {},
    )
    event._domain_service_write = True
    event.save()
    return event


def record_authentication_event(*, user, action):
    event = AuthenticationEvent(user=user, action=action)
    event._auth_audit_write = True
    event.save()
    return event


@transaction.atomic
def select_active_clinic(*, membership, actor):
    current = Membership.objects.select_for_update().select_related("clinic").filter(
        pk=membership.pk, user_id=actor.pk, is_active=True, clinic__is_active=True,
    ).first()
    if current is None:
        raise ValidationError("Active clinic membership is required.")
    _audit(current.clinic_id, actor, "clinic.context_selected", current)
    return current


@transaction.atomic
def assign_role(*, membership_id, clinic_id, role, actor, admin_membership):
    target = Membership.objects.select_for_update().get(pk=membership_id, clinic_id=clinic_id)
    _authorize(admin_membership, actor, clinic_id, Capability.CLINIC_ADMIN)
    if role == MembershipRole.Role.OWNER:
        _authorize(admin_membership, actor, clinic_id, Capability.OWNER_MANAGE)
    grant = MembershipRole.objects.filter(clinic_id=clinic_id, membership=target, role=role).first()
    if grant is None:
        grant = MembershipRole(clinic_id=clinic_id, membership=target, role=role)
        grant._domain_service_write = True
        grant.save()
    else:
        if grant.is_active:
            raise ValidationError("This role is already active for the member.")
        grant.is_active = True
        grant._role_service = True
        grant.save(update_fields=["is_active", "updated_at"])
    _audit(clinic_id, actor, "membership.role_assigned", grant, {"membership_id": str(target.pk), "role": role})
    return grant


@transaction.atomic
def revoke_role(*, membership_id, clinic_id, role, actor, admin_membership):
    Clinic.objects.select_for_update().get(pk=clinic_id)
    target = Membership.objects.select_for_update().get(pk=membership_id, clinic_id=clinic_id)
    _authorize(admin_membership, actor, clinic_id, Capability.CLINIC_ADMIN)
    if role == MembershipRole.Role.OWNER:
        _authorize(admin_membership, actor, clinic_id, Capability.OWNER_MANAGE)
        other_owner_exists = Membership.objects.filter(
            clinic_id=clinic_id, is_active=True,
            role_grants__role=MembershipRole.Role.OWNER,
            role_grants__is_active=True,
        ).exclude(pk=target.pk).exists()
        if not other_owner_exists:
            raise ValidationError("The clinic must retain at least one active owner.")
    grant = MembershipRole.objects.select_for_update().get(clinic_id=clinic_id, membership=target, role=role, is_active=True)
    grant.is_active = False
    grant._role_service = True
    grant.save(update_fields=["is_active", "updated_at"])
    _audit(clinic_id, actor, "membership.role_revoked", grant, {"membership_id": str(target.pk), "role": role})
    return grant


@transaction.atomic
def create_membership(*, clinic_id, user_id, roles, actor, admin_membership):
    _authorize(admin_membership, actor, clinic_id, Capability.CLINIC_ADMIN)
    if MembershipRole.Role.OWNER in roles:
        _authorize(admin_membership, actor, clinic_id, Capability.OWNER_MANAGE)
    user = get_user_model().objects.get(pk=user_id)
    if not user.is_active:
        raise ValidationError("An inactive account cannot be added to a clinic.")
    if not roles:
        raise ValidationError("Assign at least one clinic role.")
    membership = Membership(clinic_id=clinic_id, user=user)
    membership.save()
    for role in set(roles):
        grant = MembershipRole(clinic_id=clinic_id, membership=membership, role=role)
        grant._domain_service_write = True
        grant.save()
    _audit(clinic_id, actor, "membership.created", membership, {"user_id": str(user.pk), "roles": sorted(set(roles))})
    return membership


@transaction.atomic
def provision_staff_account(*, clinic_id, actor, admin_membership, username, email, full_name, roles):
    """Create a login and clinic membership as one audited transaction."""
    _authorize(admin_membership, actor, clinic_id, Capability.CLINIC_ADMIN)
    roles = sorted(set(roles))
    if not roles or MembershipRole.Role.OWNER in roles:
        raise ValidationError("Assign one or more supported staff roles.")
    user_model = get_user_model()
    if user_model.objects.filter(username__iexact=username).exists():
        raise ValidationError({"username": "This username is already in use."})
    # The configured Django user model's email field is not unique by default.
    temporary_password = secrets.token_urlsafe(24)
    first_name, _, last_name = full_name.strip().partition(" ")
    user = user_model.objects.create_user(
        username=username, email=email, first_name=first_name,
        last_name=last_name, password=temporary_password,
    )
    AccountSecurityState.objects.create(user=user, must_change_password=True)
    membership = create_membership(
        clinic_id=clinic_id, user_id=user.pk, roles=roles,
        actor=actor, admin_membership=admin_membership,
    )
    _audit(clinic_id, actor, "staff.account_created", membership, {
        "membership_id": str(membership.pk), "user_id": str(user.pk),
        "username": user.get_username(), "roles": roles,
    })
    return membership, temporary_password


@transaction.atomic
def change_provisioned_account_password(*, user, current_password, new_password):
    if not user.check_password(current_password):
        raise ValidationError({"current_password": "Current password is incorrect."})
    validate_password(new_password, user=user)
    user.set_password(new_password)
    user.save(update_fields=["password"])
    state = AccountSecurityState.objects.select_for_update().filter(user=user, must_change_password=True).first()
    if state:
        state.must_change_password = False
        state.save(update_fields=["must_change_password", "updated_at"])
    record_authentication_event(user=user, action="password_changed")


@transaction.atomic
def register_patient(*, clinic_id, actor, membership, **patient_fields):
    _authorize(membership, actor, clinic_id, Capability.PATIENT_ADMIN)
    patient = Patient(clinic_id=clinic_id, **patient_fields)
    patient._domain_service_write = True
    patient.save()
    _audit(clinic_id, actor, "patient.registered", patient)
    return patient


@transaction.atomic
def update_patient(*, patient_id, clinic_id, actor, membership, changes):
    patient = Patient.objects.select_for_update().get(pk=patient_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.PATIENT_ADMIN)
    allowed = {"given_name", "family_name", "date_of_birth", "sex", "phone", "email", "archived_at"}
    if not changes or not set(changes).issubset(allowed):
        raise ValidationError("Only supported patient demographics and archive state may be changed.")
    for field, value in changes.items():
        setattr(patient, field, value)
    patient._patient_service = True
    patient.save()
    _audit(clinic_id, actor, "patient.updated", patient, {"fields": sorted(changes)})
    return patient


@transaction.atomic
def record_patient_history_access(*, patient_id, clinic_id, actor, membership):
    patient = Patient.objects.get(pk=patient_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.PATIENT_CLINICAL_HISTORY)
    _audit(clinic_id, actor, "patient.clinical_history_accessed", patient)
    return patient


@transaction.atomic
def create_clinical_history_entry(*, patient_id, clinic_id, actor, membership, original_text):
    _authorize(membership, actor, clinic_id, Capability.PATIENT_CLINICAL_HISTORY)
    patient = Patient.objects.get(pk=patient_id, clinic_id=clinic_id)
    if not original_text.strip() or len(original_text) > 10000:
        raise ValidationError({"original_text": "Enter clinical history text up to 10,000 characters."})
    entry = ClinicalHistoryEntry(
        clinic_id=clinic_id, patient=patient, original_text=original_text,
        extracted_mentions=extract_symptoms(original_text),
        extractor_name="rule-based-symptom-extractor", extractor_version="1.0.0",
        provenance={"approach": "explicit_lexicon_and_local_context_rules", "model_loaded": False, "confidence_calibrated": False},
        created_by=actor,
    )
    entry._domain_service_write = True
    entry.save()
    _audit(clinic_id, actor, "clinical_history.extraction_created", entry, {"patient_id": str(patient.pk), "mention_count": len(entry.extracted_mentions), "extractor": entry.extractor_name, "version": entry.extractor_version})
    return entry


def list_clinical_history_entries(*, patient_id, clinic_id, actor, membership):
    patient = record_patient_history_access(patient_id=patient_id, clinic_id=clinic_id, actor=actor, membership=membership)
    return ClinicalHistoryEntry.objects.filter(clinic_id=clinic_id, patient=patient).select_related("created_by", "reviewed_by").order_by("-created_at")


@transaction.atomic
def review_clinical_history_entry(*, patient_id, entry_id, clinic_id, actor, membership, decision, confirmed_mentions=None):
    _authorize(membership, actor, clinic_id, Capability.PATIENT_CLINICAL_HISTORY)
    entry = ClinicalHistoryEntry.objects.select_for_update().get(pk=entry_id, patient_id=patient_id, clinic_id=clinic_id)
    if entry.status != ClinicalHistoryEntry.ReviewStatus.PENDING:
        raise ValidationError("Only pending history extraction can be reviewed.")
    if decision not in {ClinicalHistoryEntry.ReviewStatus.CONFIRMED, ClinicalHistoryEntry.ReviewStatus.REJECTED}:
        raise ValidationError("Choose confirmed or rejected.")
    if decision == ClinicalHistoryEntry.ReviewStatus.CONFIRMED:
        confirmed_mentions = confirmed_mentions if confirmed_mentions is not None else entry.extracted_mentions
        if not isinstance(confirmed_mentions, list):
            raise ValidationError({"confirmed_mentions": "Expected a list of clinician-reviewed mentions."})
        for index, mention in enumerate(confirmed_mentions):
            if not isinstance(mention, dict) or set(mention) - {"text", "normalized", "start", "end", "assertion", "duration_text", "confidence"}:
                raise ValidationError({"confirmed_mentions": f"Mention {index + 1} has an invalid structure."})
            if not isinstance(mention.get("text"), str) or not mention["text"].strip() or len(mention["text"]) > 120:
                raise ValidationError({"confirmed_mentions": f"Mention {index + 1} requires symptom text up to 120 characters."})
            if not isinstance(mention.get("normalized"), str) or not mention["normalized"].strip() or len(mention["normalized"]) > 120:
                raise ValidationError({"confirmed_mentions": f"Mention {index + 1} requires a normalized symptom up to 120 characters."})
            if mention.get("assertion") not in {"present", "absent", "uncertain"}:
                raise ValidationError({"confirmed_mentions": f"Mention {index + 1} has an unsupported assertion."})
            if not isinstance(mention.get("duration_text", ""), str):
                raise ValidationError({"confirmed_mentions": f"Mention {index + 1} duration must be text."})
    else:
        confirmed_mentions = None
    entry.status = decision
    entry.confirmed_mentions = confirmed_mentions
    entry.reviewed_by = actor
    entry.reviewed_at = timezone.now()
    entry._history_review_transition = True
    entry.save(update_fields=["status", "confirmed_mentions", "reviewed_by", "reviewed_at", "updated_at"])
    _audit(clinic_id, actor, "clinical_history.extraction_reviewed", entry, {"decision": decision, "patient_id": str(patient_id), "confirmed_mention_count": len(confirmed_mentions or [])})
    return entry


@transaction.atomic
def schedule_appointment(*, clinic_id, actor, membership, patient_id, starts_at, reason=""):
    _authorize(membership, actor, clinic_id, Capability.APPOINTMENT_ADMIN)
    Patient.objects.get(pk=patient_id, clinic_id=clinic_id)
    appointment = Appointment(clinic_id=clinic_id, patient_id=patient_id, starts_at=starts_at, reason=reason)
    appointment._domain_service_write = True
    appointment.save()
    _audit(clinic_id, actor, "appointment.scheduled", appointment)
    return appointment


@transaction.atomic
def reschedule_appointment(*, appointment_id, clinic_id, actor, membership, starts_at, reason=None):
    appointment = Appointment.objects.select_for_update().get(pk=appointment_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.APPOINTMENT_ADMIN)
    if appointment.status != Appointment.Status.SCHEDULED:
        raise ValidationError("Only scheduled appointments can be rescheduled.")
    before = appointment.starts_at.isoformat()
    appointment.starts_at = starts_at
    if reason is not None:
        appointment.reason = reason
    appointment._appointment_change = True
    appointment.save(update_fields=["starts_at", "reason", "updated_at"])
    _audit(clinic_id, actor, "appointment.rescheduled", appointment, {"from": before, "to": starts_at.isoformat()})
    return appointment


@transaction.atomic
def order_examination(*, clinic_id, actor, membership, patient_id, appointment_id=None, modality="", procedure_name="", clinical_indication="", clinical_service_id=None):
    _authorize(membership, actor, clinic_id, Capability.INTERPRET)
    patient = Patient.objects.get(pk=patient_id, clinic_id=clinic_id)
    appointment = None
    if appointment_id:
        appointment = Appointment.objects.get(pk=appointment_id, clinic_id=clinic_id)
        if appointment.patient_id != patient.pk:
            raise ValidationError("Appointment and patient must match.")
    service = None
    if clinical_service_id:
        service = ClinicalService.objects.get(pk=clinical_service_id, clinic_id=clinic_id, is_active=True)
        modality = modality or service.modality
        procedure_name = procedure_name or service.name
    if not modality.strip() or not procedure_name.strip():
        raise ValidationError("Choose a clinical service or provide modality and procedure name.")
    exam = Examination(
        clinic_id=clinic_id, patient=patient, appointment=appointment,
        clinical_service=service, service_code_snapshot=service.code if service else "",
        modality=modality, procedure_name=procedure_name,
        clinical_indication=clinical_indication, ordering_user=actor,
    )
    exam._domain_service_write = True
    exam.save()
    _audit(clinic_id, actor, "examination.ordered", exam)
    assign_examination(
        examination_id=exam.pk, clinic_id=clinic_id, actor=actor, membership=membership,
        technician_id=None, radiologist_id=actor.pk,
    )
    return exam


@transaction.atomic
def assign_examination(*, examination_id, clinic_id, actor, membership, radiologist_id, technician_id=None):
    exam = Examination.objects.select_for_update().get(pk=examination_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.ASSIGN_EXAMINATION)
    if not MembershipRole.objects.filter(
        membership__clinic_id=clinic_id, membership__is_active=True,
        membership__clinic__is_active=True, membership__user_id=radiologist_id,
        clinic_id=clinic_id, role=MembershipRole.Role.RADIOLOGIST, is_active=True,
    ).exists():
        raise ValidationError("The selected radiologist is not an active radiologist in this clinic.")
    if technician_id is not None and not MembershipRole.objects.filter(
        membership__clinic_id=clinic_id, membership__is_active=True,
        membership__clinic__is_active=True, membership__user_id=technician_id,
        clinic_id=clinic_id, role=MembershipRole.Role.TECHNICIAN, is_active=True,
    ).exists():
        raise ValidationError("The selected technician is not an active technician in this clinic.")
    assignment = ExaminationAssignment.objects.select_for_update().filter(
        examination=exam, clinic_id=clinic_id,
    ).first()
    previous = {
        "technician_id": assignment.technician_id if assignment else None,
        "radiologist_id": assignment.radiologist_id if assignment else None,
    }
    if assignment is None:
        assignment = ExaminationAssignment(clinic_id=clinic_id, examination=exam, assigned_by=actor)
        assignment._domain_service_write = True
    assignment.technician_id = technician_id
    assignment.radiologist_id = radiologist_id
    assignment.assigned_by = actor
    assignment._assignment_service_write = True
    assignment._domain_service_write = True
    assignment.save()
    _audit(clinic_id, actor, "examination.assignment_changed", assignment, {
        "previous": previous,
        "current": {"technician_id": technician_id, "radiologist_id": radiologist_id},
    })
    return assignment


def _require_assigned_operator(exam, actor):
    assignment = getattr(exam, "assignment", None)
    if assignment:
        expected_id = assignment.technician_id or assignment.radiologist_id
        if expected_id != actor.pk:
            raise PermissionDenied("This examination is assigned to another acquisition operator.")


def _require_assigned_radiologist(exam, actor):
    assignment = getattr(exam, "assignment", None)
    if assignment and assignment.radiologist_id != actor.pk:
        raise PermissionDenied("This examination is assigned to another radiologist.")


@transaction.atomic
def update_examination_order(*, examination_id, clinic_id, actor, membership, changes):
    exam = Examination.objects.select_for_update().get(pk=examination_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.INTERPRET)
    if exam.status != Examination.Status.ORDERED:
        raise ValidationError("Examination orders can only be corrected before check-in.")
    allowed = {"modality", "procedure_name", "clinical_indication", "appointment_id"}
    if not changes or not set(changes).issubset(allowed):
        raise ValidationError("Unsupported examination order fields.")
    for field, value in changes.items():
        setattr(exam, field, value)
    exam._exam_update = True
    exam.save()
    _audit(clinic_id, actor, "examination.order_updated", exam, {"fields": sorted(changes)})
    return exam


@transaction.atomic
def set_membership_active(*, membership_id, clinic_id, active, actor, admin_membership):
    Clinic.objects.select_for_update().get(pk=clinic_id)
    target = Membership.objects.select_for_update().get(pk=membership_id, clinic_id=clinic_id)
    _authorize(admin_membership, actor, clinic_id, Capability.CLINIC_ADMIN)
    if target.is_active == active:
        raise ValidationError("Membership already has the requested active state.")
    if not active and target.has_role(MembershipRole.Role.OWNER):
        other_owner_exists = Membership.objects.filter(
            clinic_id=clinic_id, is_active=True,
            role_grants__role=MembershipRole.Role.OWNER,
            role_grants__is_active=True,
        ).exclude(pk=target.pk).exists()
        if not other_owner_exists:
            raise ValidationError("The clinic must retain at least one active owner.")
    target.is_active = active
    target._membership_service = True
    target.save(update_fields=["is_active", "updated_at"])
    _audit(clinic_id, actor, "membership.activated" if active else "membership.deactivated", target)
    return target


@transaction.atomic
def update_membership_registration(*, membership_id, clinic_id, professional_registration_number, actor, admin_membership):
    target = Membership.objects.select_for_update().get(pk=membership_id, clinic_id=clinic_id)
    _authorize(admin_membership, actor, clinic_id, Capability.CLINIC_ADMIN)
    if not target.has_role(MembershipRole.Role.RADIOLOGIST):
        raise ValidationError("Professional registration details apply to radiologist memberships.")
    target.professional_registration_number = professional_registration_number.strip()
    target.save(update_fields=["professional_registration_number", "updated_at"])
    _audit(clinic_id, actor, "membership.professional_registration_updated", target, {"configured": bool(professional_registration_number.strip())})
    return target


@transaction.atomic
def update_clinic_profile(*, clinic_id, changes, actor, membership):
    clinic = Clinic.objects.select_for_update().get(pk=clinic_id, is_active=True)
    _authorize(membership, actor, clinic_id, Capability.CLINIC_ADMIN)
    allowed = {"name", "address", "phone", "email", "default_currency", "invoice_prefix"}
    if not changes or not set(changes).issubset(allowed):
        raise ValidationError("Only clinic identity and contact fields may be changed.")
    for field, value in changes.items():
        setattr(clinic, field, value)
    clinic.full_clean()
    clinic.save(update_fields=[*changes.keys()])
    _audit(clinic_id, actor, "clinic.profile_updated", clinic, {"fields": sorted(changes)})
    return clinic


@transaction.atomic
def transfer_clinic_ownership(*, clinic_id, target_membership_id, actor, admin_membership):
    clinic = Clinic.objects.select_for_update().get(pk=clinic_id, is_active=True)
    current = _authorize(admin_membership, actor, clinic_id, Capability.OWNER_MANAGE)
    target = Membership.objects.select_for_update().get(pk=target_membership_id, clinic_id=clinic_id, is_active=True)
    if target.pk == current.pk:
        raise ValidationError("Choose another active clinic member as the new owner.")
    grant = MembershipRole.objects.select_for_update().filter(clinic_id=clinic_id, membership=target, role=MembershipRole.Role.OWNER).first()
    if grant is None:
        grant = MembershipRole(clinic_id=clinic_id, membership=target, role=MembershipRole.Role.OWNER)
        grant._domain_service_write = True
        grant.save()
    if not grant.is_active:
        grant.is_active = True
        grant._role_service = True
        grant.save(update_fields=["is_active", "updated_at"])
    old_grant = MembershipRole.objects.select_for_update().filter(
        clinic_id=clinic_id, membership=current, role=MembershipRole.Role.OWNER, is_active=True,
    ).first()
    if not old_grant:
        raise ValidationError("The acting owner grant is not active.")
    old_grant.is_active = False
    old_grant._role_service = True
    old_grant.save(update_fields=["is_active", "updated_at"])
    _audit(clinic_id, actor, "membership.ownership_transferred", target, {"from_membership_id": str(current.pk), "to_membership_id": str(target.pk)})
    return target


@transaction.atomic
def register_imaging_instance(*, clinic_id, actor, membership, examination_id, kind, modality, storage_key, media_type, original_name, byte_size, checksum_sha256, study_id=None, series_id=None, study_instance_uid="", series_instance_uid="", series_number=None, sop_instance_uid="", description="", series_description="", metadata=None):
    _authorize(membership, actor, clinic_id, Capability.UPLOAD_IMAGE)
    exam = Examination.objects.get(pk=examination_id, clinic_id=clinic_id)
    if study_id:
        study = ImagingStudy.objects.get(pk=study_id, clinic_id=clinic_id, examination=exam)
        if study.kind != kind:
            raise ValidationError("An imaging study cannot mix raster and DICOM instances.")
    else:
        study = ImagingStudy(clinic_id=clinic_id, examination=exam, kind=kind, modality=modality, study_instance_uid=study_instance_uid, description=description[:255])
        study._domain_service_write = True
        study.save()
    if series_id:
        series = ImagingSeries.objects.get(pk=series_id, clinic_id=clinic_id, study=study)
    else:
        series = ImagingSeries(clinic_id=clinic_id, study=study, series_instance_uid=series_instance_uid, modality=modality, series_number=series_number, description=series_description[:255])
        series._domain_service_write = True
        series.save()
    instance = ImagingInstance(
        clinic_id=clinic_id, series=series, sop_instance_uid=sop_instance_uid,
        storage_key=storage_key, media_type=media_type, original_name=original_name,
        byte_size=byte_size, checksum_sha256=checksum_sha256,
        uploaded_by=actor, metadata=metadata or {},
    )
    instance._domain_service_write = True
    instance.save()
    _audit(clinic_id, actor, "imaging.instance_registered", instance, {"study_id": str(study.pk), "series_id": str(series.pk), "kind": kind})
    return instance


@transaction.atomic
def create_charge_item(*, clinic_id, actor, membership, code, name, category, unit_price_minor, currency=None, tax_rate_bps=0, description=""):
    _authorize(membership, actor, clinic_id, Capability.BILLING_CONFIGURE)
    currency = (currency or Clinic.objects.get(pk=clinic_id).default_currency).upper()
    item = ChargeItem(clinic_id=clinic_id, configured_by=actor, code=code.strip().upper(), name=name.strip(), description=description.strip(), category=category, unit_price_minor=unit_price_minor, currency=currency, tax_rate_bps=tax_rate_bps)
    item._domain_service_write = True
    item.save()
    _audit(clinic_id, actor, "billing.charge_created", item, {"code": item.code, "category": category, "unit_price_minor": unit_price_minor, "currency": currency})
    return item


@transaction.atomic
def update_charge_item(*, charge_item_id, clinic_id, actor, membership, changes):
    _authorize(membership, actor, clinic_id, Capability.BILLING_CONFIGURE)
    item = ChargeItem.objects.select_for_update().get(pk=charge_item_id, clinic_id=clinic_id)
    allowed = {"code", "name", "description", "category", "unit_price_minor", "currency", "tax_rate_bps", "is_active"}
    if not changes or not set(changes).issubset(allowed):
        raise ValidationError("Unsupported charge catalogue fields.")
    before = {key: getattr(item, key) for key in changes}
    for key, value in changes.items():
        if key == "code":
            value = value.strip().upper()
        setattr(item, key, value)
    item._billing_service = True
    item.save()
    _audit(clinic_id, actor, "billing.charge_updated", item, {"before": before, "after": changes})
    return item


@transaction.atomic
def save_clinical_service(*, clinic_id, actor, membership, service_id=None, changes):
    _authorize(membership, actor, clinic_id, Capability.BILLING_CONFIGURE)
    if service_id:
        service = ClinicalService.objects.select_for_update().get(pk=service_id, clinic_id=clinic_id)
        allowed = {"code", "name", "modality", "description", "linked_charge_id", "is_active"}
        if not changes or not set(changes).issubset(allowed):
            raise ValidationError("Unsupported clinical service fields.")
        for key, value in changes.items():
            setattr(service, key, value)
        service._service_catalogue_write = True
        service.save()
        action = "clinic.service_updated"
    else:
        service = ClinicalService(clinic_id=clinic_id, configured_by=actor, **changes)
        service._domain_service_write = True
        service.save()
        action = "clinic.service_created"
    _audit(clinic_id, actor, action, service, {"code": service.code, "active": service.is_active})
    return service


def _billing_tax(taxable_minor, rate_bps):
    return (taxable_minor * rate_bps + 5000) // 10000


@transaction.atomic
def create_invoice(*, clinic_id, actor, membership, patient_id, line_items=None, examination_id=None, discount_minor=0, amount_minor=None, currency="INR"):
    _authorize(membership, actor, clinic_id, Capability.BILLING)
    patient = Patient.objects.get(pk=patient_id, clinic_id=clinic_id)
    examination = Examination.objects.get(pk=examination_id, clinic_id=clinic_id, patient=patient) if examination_id else None
    if not line_items:
        # Compatibility for internal callers while the API requires catalogue-backed lines.
        if not amount_minor or amount_minor <= 0:
            raise ValidationError("At least one valid charge line is required.")
        currency = currency.upper()
        prepared = [{"charge_item": None, "description": "Legacy service", "quantity": 1, "unit_price_minor": amount_minor, "tax_rate_bps": 0}]
    else:
        prepared = []
        for row in line_items:
            charge = ChargeItem.objects.select_for_update().filter(pk=row["charge_item_id"], clinic_id=clinic_id, is_active=True).first()
            if charge is None:
                raise ValidationError("A selected charge is unavailable in this clinic.")
            prepared.append({"charge_item": charge, "description": charge.name, "quantity": row["quantity"], "unit_price_minor": charge.unit_price_minor, "tax_rate_bps": charge.tax_rate_bps})
        currencies = {row["charge_item"].currency for row in prepared}
        if len(currencies) != 1:
            raise ValidationError("All invoice charges must use the same currency.")
        currency = currencies.pop()
    gross = sum(row["quantity"] * row["unit_price_minor"] for row in prepared)
    if discount_minor < 0 or discount_minor >= gross:
        raise ValidationError("Discount must be less than the invoice subtotal.")
    if discount_minor:
        _authorize(membership, actor, clinic_id, Capability.BILLING_DISCOUNT)
    clinic = Clinic.objects.select_for_update().get(pk=clinic_id)
    sequence = clinic.next_invoice_number
    clinic.next_invoice_number = sequence + 1
    clinic.save(update_fields=["next_invoice_number"])
    prefix = clinic.invoice_prefix.strip().upper() or clinic.pk.hex[:8].upper()
    invoice_number = f"{prefix}-{timezone.localdate():%Y}-{sequence:06d}"
    remaining_discount = discount_minor
    total_tax = 0
    line_values = []
    for index, row in enumerate(prepared):
        subtotal = row["quantity"] * row["unit_price_minor"]
        allocated = remaining_discount if index == len(prepared) - 1 else (discount_minor * subtotal // gross)
        remaining_discount -= allocated
        tax = _billing_tax(subtotal - allocated, row["tax_rate_bps"])
        total_tax += tax
        line_values.append({**row, "subtotal_minor": subtotal, "discount_minor": allocated, "tax_minor": tax})
    total = gross - discount_minor + total_tax
    invoice = Invoice(
        clinic_id=clinic_id, patient=patient, examination=examination,
        invoice_number=invoice_number, gross_amount_minor=gross,
        discount_minor=discount_minor, tax_minor=total_tax, amount_minor=total,
        currency=currency, status=Invoice.Status.DRAFT,
        clinic_context={"name": clinic.name, "address": clinic.address, "phone": clinic.phone, "email": clinic.email},
        patient_context={"name": " ".join(x for x in (patient.given_name, patient.family_name) if x), "medical_record_number": patient.medical_record_number, "date_of_birth": patient.date_of_birth.isoformat() if patient.date_of_birth else ""},
    )
    invoice._domain_service_write = True
    invoice.save()
    for row in line_values:
        line = InvoiceLineItem(clinic_id=clinic_id, invoice=invoice, charge_item=row["charge_item"], description=row["description"], quantity=row["quantity"], unit_price_minor=row["unit_price_minor"], tax_rate_bps=row["tax_rate_bps"], subtotal_minor=row["subtotal_minor"], discount_minor=row["discount_minor"], tax_minor=row["tax_minor"])
        line._billing_service = True
        line._domain_service_write = True
        line.save()
    invoice.status = Invoice.Status.ISSUED
    invoice.issued_at = timezone.now()
    invoice.issued_by = actor
    invoice._invoice_transition = True
    invoice.save(update_fields=["status", "issued_at", "issued_by", "updated_at"])
    _audit(clinic_id, actor, "invoice.created", invoice, {"amount_minor": total, "gross_minor": gross, "discount_minor": discount_minor, "tax_minor": total_tax, "currency": currency})
    _audit(clinic_id, actor, "invoice.issued", invoice, {"invoice_number": invoice_number})
    return invoice


@transaction.atomic
def void_invoice(*, invoice_id, clinic_id, actor, membership, reason="Cancelled by authorized clinic user"):
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.BILLING_CANCEL)
    if invoice.status not in {Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID}:
        raise ValidationError("Only an active invoice can be cancelled.")
    if invoice.payments.exists():
        raise ValidationError("An invoice with payment history cannot be cancelled; refund payments and issue a correction.")
    invoice.status = Invoice.Status.CANCELLED
    invoice.cancelled_by = actor
    invoice.cancelled_at = timezone.now()
    invoice.cancellation_reason = reason.strip()
    invoice._invoice_transition = True
    invoice.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancellation_reason", "updated_at"])
    _audit(clinic_id, actor, "invoice.cancelled", invoice, {"reason": invoice.cancellation_reason})
    return invoice


@transaction.atomic
def record_ai_analysis(*, clinic_id, actor, membership, examination_id, imaging_study_id, provider, model_name, model_version, task, output, uncertainty=None, provenance=None):
    _authorize(membership, actor, clinic_id, Capability.INTERPRET)
    examination = Examination.objects.get(pk=examination_id, clinic_id=clinic_id)
    imaging_study = ImagingStudy.objects.get(pk=imaging_study_id, clinic_id=clinic_id, examination=examination)
    analysis = AIAnalysis(
        clinic_id=clinic_id, examination=examination, imaging_study=imaging_study,
        provider=provider, model_name=model_name, model_version=model_version, task=task,
        output=output, uncertainty=uncertainty, provenance=provenance or {},
    )
    analysis._ai_ingest = True
    analysis.save()
    _audit(clinic_id, actor, "ai_analysis.created", analysis, {"provider": provider, "model_name": model_name, "model_version": model_version, "task": task})
    return analysis


@transaction.atomic
def create_ai_report_assist(*, clinic_id, report_id, actor, membership, evidence_input):
    report = Report.objects.select_for_update().select_related("examination").get(pk=report_id, clinic_id=clinic_id)
    exam = Examination.objects.select_for_update().get(pk=report.examination_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.INTERPRET)
    _require_assigned_radiologist(exam, actor)
    if report.status != Report.Status.DRAFT or exam.status != Examination.Status.REPORTING:
        raise ValidationError("AI assistance is available only on an editable report draft in reporting.")
    modality = (exam.modality or "").strip().upper()
    if modality not in {"CR", "DX", "DR", "XR", "X-RAY"}:
        raise ValidationError("Evidence assistance currently supports chest X-ray text only; use the manual report workflow for other modalities.")
    if not any(word in exam.procedure_name.lower() for word in ("chest", "thorax")):
        raise ValidationError("Evidence assistance is limited to examinations identified as chest or thorax.")
    if template_for_modality(modality) != report.template:
        raise ValidationError("The report template does not match the examination modality.")
    observations = evidence_input.get("observations")
    if not isinstance(observations, list) or not observations or len(observations) > 40:
        raise ValidationError({"observations": "Provide 1 to 40 explicit radiologist observations."})
    for index, item in enumerate(observations):
        if not isinstance(item, dict) or set(item) - {"anatomy", "text", "polarity"}:
            raise ValidationError({"observations": f"Observation {index + 1} has an invalid structure."})
        for field in ("anatomy", "text"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > (120 if field == "anatomy" else 2000):
                raise ValidationError({"observations": f"Observation {index + 1} requires a valid {field}."})
        if item.get("polarity") not in {"positive", "negative", "uncertain"}:
            raise ValidationError({"observations": f"Observation {index + 1} polarity must be positive, negative or uncertain."})
    for field in ("clinical_indication", "technique"):
        if field in evidence_input and (not isinstance(evidence_input[field], str) or len(evidence_input[field]) > 2000):
            raise ValidationError({field: "Use plain text up to 2000 characters."})
    sections, verification = build_assist(evidence_input, "CHEST_XRAY")
    assist = AIReportAssist(
        clinic_id=clinic_id, examination=exam, report=report,
        provider="local", model_name="rule-based-evidence-structurer",
        model_version="1.0.0", evidence_input=evidence_input,
        generated_sections=sections, verification=verification,
        provenance={"task": "radiologist_observation_structuring", "observation_source": "radiologist_provided_text", "context_source": "exam_indication_and_current_draft_technique", "image_processing": False, "created_at": timezone.now().isoformat()},
    )
    assist._domain_service_write = True
    assist.save()
    _audit(clinic_id, actor, "ai_report_assist.generated", assist, {"report_id": str(report.pk), "modality": verification["modality"], "model": assist.model_name, "version": assist.model_version})
    return assist


@transaction.atomic
def review_ai_report_assist(*, clinic_id, report_id, assist_id, actor, membership, decision):
    assist = AIReportAssist.objects.select_for_update().select_related("examination", "report").get(pk=assist_id, clinic_id=clinic_id, report_id=report_id)
    _authorize(membership, actor, clinic_id, Capability.INTERPRET)
    _require_assigned_radiologist(assist.examination, actor)
    if assist.report.status != Report.Status.DRAFT:
        raise ValidationError("AI output cannot be reviewed against a signed report.")
    if decision not in {AIReportAssist.ReviewStatus.ACCEPTED, AIReportAssist.ReviewStatus.REJECTED}:
        raise ValidationError("Choose accepted or rejected.")
    if assist.review_status != AIReportAssist.ReviewStatus.PENDING:
        raise ValidationError("This AI suggestion has already been reviewed.")
    assist.review_status = decision
    assist.reviewed_by = actor
    assist.reviewed_at = timezone.now()
    assist._ai_report_review_transition = True
    assist.save(update_fields=["review_status", "reviewed_by", "reviewed_at", "updated_at"])
    _audit(clinic_id, actor, "ai_report_assist.reviewed", assist, {"decision": decision, "report_id": str(assist.report_id)})
    return assist


def list_ai_report_assists(*, clinic_id, report_id, actor, membership):
    report = Report.objects.select_related("examination").get(pk=report_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.INTERPRET)
    _require_assigned_radiologist(report.examination, actor)
    return AIReportAssist.objects.filter(clinic_id=clinic_id, report_id=report_id).order_by("-created_at")


APPOINTMENT_TRANSITIONS = {
    Appointment.Status.SCHEDULED: {
        Appointment.Status.CHECKED_IN: Capability.CHECK_IN,
        Appointment.Status.CANCELLED: Capability.APPOINTMENT_ADMIN,
        Appointment.Status.NO_SHOW: Capability.APPOINTMENT_ADMIN,
    },
}

EXAMINATION_TRANSITIONS = {
    Examination.Status.ORDERED: {
        Examination.Status.CHECKED_IN: Capability.CHECK_IN,
        Examination.Status.CANCELLED: Capability.APPOINTMENT_ADMIN,
    },
    Examination.Status.CHECKED_IN: {
        Examination.Status.IN_PROGRESS: Capability.ACQUIRE,
        Examination.Status.CANCELLED: Capability.APPOINTMENT_ADMIN,
    },
    Examination.Status.IN_PROGRESS: {
        Examination.Status.ACQUIRED: Capability.ACQUIRE,
        Examination.Status.CANCELLED: Capability.APPOINTMENT_ADMIN,
    },
    Examination.Status.ACQUIRED: {Examination.Status.REPORTING: Capability.INTERPRET},
}


@transaction.atomic
def transition_appointment(*, appointment_id, clinic_id, target, actor, membership):
    obj = Appointment.objects.select_for_update().get(pk=appointment_id, clinic_id=clinic_id)
    allowed = APPOINTMENT_TRANSITIONS.get(obj.status, {})
    capability = allowed.get(target)
    if not capability:
        raise ValidationError(f"Appointment cannot transition from {obj.status} to {target}.")
    _authorize(membership, actor, clinic_id, capability)
    old = obj.status
    obj.status = target
    obj._workflow_transition = True
    obj.save(update_fields=["status", "updated_at"])
    _audit(clinic_id, actor, "appointment.transition", obj, {"from": old, "to": target})
    return obj


@transaction.atomic
def transition_examination(*, examination_id, clinic_id, target, actor, membership):
    obj = Examination.objects.select_for_update().get(pk=examination_id, clinic_id=clinic_id)
    capability = EXAMINATION_TRANSITIONS.get(obj.status, {}).get(target)
    if not capability:
        raise ValidationError(f"Examination cannot transition from {obj.status} to {target}.")
    _authorize(membership, actor, clinic_id, capability)
    if target in {Examination.Status.IN_PROGRESS, Examination.Status.ACQUIRED}:
        _require_assigned_operator(obj, actor)
    if target == Examination.Status.ACQUIRED:
        from .private_storage import PrivateImageStorage

        instances = ImagingInstance.objects.filter(clinic_id=clinic_id, series__study__examination=obj)
        if not instances.exists():
            raise ValidationError("Acquisition cannot be completed before at least one validated image is uploaded.")
        storage = PrivateImageStorage()
        if not all(storage.exists(key) for key in instances.values_list("storage_key", flat=True)):
            raise ValidationError("One or more uploaded image files are unavailable; acquisition cannot be completed.")
    if target == Examination.Status.CHECKED_IN and obj.appointment_id:
        if obj.appointment.status != Appointment.Status.CHECKED_IN:
            raise ValidationError("An examination linked to an appointment can be checked in only after the appointment.")
    old = obj.status
    obj.status = target
    obj._workflow_transition = True
    obj.save(update_fields=["status", "updated_at"])
    _audit(clinic_id, actor, "examination.transition", obj, {"from": old, "to": target})
    return obj


@transaction.atomic
def create_report_draft(*, examination_id, clinic_id, actor, membership, amends_id=None, template=None):
    exam = Examination.objects.select_for_update().get(pk=examination_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.INTERPRET)
    _require_assigned_radiologist(exam, actor)
    template = template or template_for_modality(exam.modality)
    if template not in REPORT_TEMPLATES or (template != "general" and template != template_for_modality(exam.modality)):
        raise ValidationError("Select a report template appropriate for the examination modality.")
    if amends_id:
        if exam.status not in {Examination.Status.RELEASED, Examination.Status.DELIVERED, Examination.Status.REPORTED, Examination.Status.REPORTING}:
            raise ValidationError("An amendment requires an already reported examination.")
    elif exam.status not in {Examination.Status.ACQUIRED, Examination.Status.REPORTING}:
        raise ValidationError("A report draft requires an acquired examination.")
    latest = exam.reports.order_by("-version").first()
    if amends_id:
        prior = Report.objects.get(pk=amends_id, clinic_id=clinic_id, examination=exam)
        if prior.status == Report.Status.DRAFT:
            raise ValidationError("Only a signed or released report can be amended.")
        if latest and prior.pk != latest.pk:
            raise ValidationError("Amend the latest report version to preserve a linear history.")
        report = Report(
            clinic_id=clinic_id, examination=exam, version=(latest.version + 1 if latest else 1),
            amends=prior, template=template, technique=prior.technique,
            sequences=prior.sequences, comparison=prior.comparison,
            findings=prior.findings, impression=prior.impression,
            additional_comments=prior.additional_comments, created_by=actor,
        )
    else:
        if latest:
            raise ValidationError("A later report version must explicitly amend the latest version.")
        report = Report(clinic_id=clinic_id, examination=exam, version=1, template=template, created_by=actor)
    report._domain_service_write = True
    try:
        # The examination lock serializes version assignment on databases with
        # row locks; the unique constraint remains the final concurrency guard.
        with transaction.atomic():
            report.save()
    except IntegrityError as exc:
        raise ValidationError("A report version was created concurrently. Reload report history and try again.") from exc
    if exam.status in {Examination.Status.ACQUIRED, Examination.Status.RELEASED, Examination.Status.DELIVERED, Examination.Status.REPORTED}:
        previous_status = exam.status
        exam.status = Examination.Status.REPORTING
        exam._workflow_transition = True
        exam.save(update_fields=["status", "updated_at"])
        action = "examination.amendment_reopened" if amends_id else "examination.transition"
        _audit(clinic_id, actor, action, exam, {"from": previous_status, "to": "reporting"})
    _audit(clinic_id, actor, "report.draft_created", report, {"version": report.version, "amends": str(report.amends_id) if report.amends_id else None})
    return report


@transaction.atomic
def edit_report_draft(*, report_id, clinic_id, actor, membership, findings, impression, expected_updated_at=None, technique=None, sequences=None, comparison=None, additional_comments=None):
    report = Report.objects.select_for_update().get(pk=report_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.INTERPRET)
    _require_assigned_radiologist(report.examination, actor)
    if report.status != Report.Status.DRAFT:
        raise ValidationError("Only draft reports can be edited.")
    if expected_updated_at is not None and report.updated_at != expected_updated_at:
        raise ValidationError("This draft changed since it was opened. Reload it before saving to avoid overwriting another editor's changes.")
    if technique is not None:
        report.technique = technique
    if sequences is not None:
        report.sequences = sequences
    if comparison is not None:
        report.comparison = comparison
    report.findings = findings
    report.impression = impression
    if additional_comments is not None:
        report.additional_comments = additional_comments
    report._report_content_edit = True
    fields = ["findings", "impression"]
    if technique is not None:
        fields.append("technique")
    if sequences is not None:
        fields.append("sequences")
    if comparison is not None:
        fields.append("comparison")
    if additional_comments is not None:
        fields.append("additional_comments")
    report.save(update_fields=[*fields, "updated_at"])
    _audit(clinic_id, actor, "report.draft_updated", report, {"version": report.version, "fields": fields})
    return report


@transaction.atomic
def sign_report(*, report_id, clinic_id, actor, membership, expected_updated_at=None):
    report = Report.objects.select_for_update().get(pk=report_id, clinic_id=clinic_id)
    exam = Examination.objects.select_for_update().get(pk=report.examination_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.SIGN_REPORT)
    _require_assigned_radiologist(exam, actor)
    if expected_updated_at is not None and report.updated_at != expected_updated_at:
        raise ValidationError("This draft changed since it was opened. Reload and review the latest content before signing.")
    if report.status != Report.Status.DRAFT or exam.status != Examination.Status.REPORTING:
        raise ValidationError("Only a draft for an examination in reporting can be signed.")
    if exam.reports.order_by("-version").values_list("pk", flat=True).first() != report.pk:
        raise ValidationError("Only the latest report version can be signed.")
    if not report.findings.strip() or not report.impression.strip():
        raise ValidationError("Findings and impression must be completed before signing.")
    patient = exam.patient
    clinic = exam.clinic
    signed_at = timezone.now()
    reporting_membership = Membership.objects.filter(
        clinic_id=clinic_id, user=actor, is_active=True,
        role_grants__role=MembershipRole.Role.RADIOLOGIST,
        role_grants__is_active=True,
    ).select_related("clinic").first()
    report.signed_context = {
        "patient": {
            "name": " ".join(part for part in (patient.given_name, patient.family_name) if part),
            "medical_record_number": patient.medical_record_number,
            "date_of_birth": patient.date_of_birth.isoformat() if patient.date_of_birth else "",
        },
        "examination": {
            "modality": exam.modality,
            "name": exam.procedure_name,
            "clinical_indication": exam.clinical_indication,
            "created_at": exam.created_at.isoformat(),
        },
        "clinic": {
            "name": clinic.name, "address": clinic.address,
            "phone": clinic.phone, "email": clinic.email,
        },
        "radiologist": {
            "name": actor.get_full_name(),
            "registration_number": reporting_membership.professional_registration_number if reporting_membership else "",
        },
        "report_date": signed_at.isoformat(),
    }
    report.status = Report.Status.SIGNED
    report.signed_by = actor
    report.signed_at = signed_at
    report._report_transition = True
    report.save(update_fields=["status", "signed_by", "signed_at", "signed_context", "updated_at"])
    exam.status = Examination.Status.REPORTED
    exam._workflow_transition = True
    exam.save(update_fields=["status", "updated_at"])
    _audit(clinic_id, actor, "report.signed", report, {"version": report.version})
    _audit(clinic_id, actor, "examination.transition", exam, {"from": "reporting", "to": "reported"})
    return report


@transaction.atomic
def release_report(*, report_id, clinic_id, actor, membership):
    report = Report.objects.select_for_update().get(pk=report_id, clinic_id=clinic_id)
    exam = Examination.objects.select_for_update().get(pk=report.examination_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.RELEASE_REPORT)
    _require_assigned_radiologist(exam, actor)
    if report.status != Report.Status.SIGNED:
        raise ValidationError("Report must be signed before it can be released.")
    if not report.signed_by_id or not report.signed_at or not report.signed_context:
        raise ValidationError("The signed report snapshot is incomplete and cannot be released.")
    if exam.reports.order_by("-version").values_list("pk", flat=True).first() != report.pk:
        raise ValidationError("Only the latest report version can be released.")
    if exam.status != Examination.Status.REPORTED:
        raise ValidationError("The examination must be in reported status before release.")
    report.status = Report.Status.RELEASED
    report.released_by = actor
    report.released_at = timezone.now()
    report._report_transition = True
    report.save(update_fields=["status", "released_by", "released_at", "updated_at"])
    exam.status = Examination.Status.RELEASED
    exam._workflow_transition = True
    exam.save(update_fields=["status", "updated_at"])
    _audit(clinic_id, actor, "report.released", report, {"version": report.version})
    _audit(clinic_id, actor, "examination.transition", exam, {"from": "reported", "to": "released"})
    return report


@transaction.atomic
def deliver_report(*, examination_id, clinic_id, actor, membership):
    exam = Examination.objects.select_for_update().get(pk=examination_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.DELIVER_REPORT)
    if exam.status != Examination.Status.RELEASED:
        raise ValidationError("Only released reports can be delivered.")
    report = exam.reports.filter(status=Report.Status.RELEASED).order_by("-version").first()
    if report is None:
        raise ValidationError("Released report record is missing.")
    report.status = Report.Status.DELIVERED
    report.delivered_by = actor
    report.delivered_at = timezone.now()
    report._report_transition = True
    report.save(update_fields=["status", "delivered_by", "delivered_at", "updated_at"])
    exam.status = Examination.Status.DELIVERED
    exam._workflow_transition = True
    exam.save(update_fields=["status", "updated_at"])
    _audit(clinic_id, actor, "report.delivered", report, {"version": report.version})
    _audit(clinic_id, actor, "examination.transition", exam, {"from": "released", "to": "delivered"})
    return exam


@transaction.atomic
def authorize_report_pdf(*, report_id, clinic_id, actor, membership):
    report = Report.objects.select_for_update().select_related("examination__patient", "clinic", "signed_by").get(pk=report_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.REPORT_READ)
    if report.status == Report.Status.DRAFT or not report.signed_context:
        raise ValidationError("PDF export is available only for a signed report version.")
    _audit(clinic_id, actor, "report.pdf_exported", report, {"version": report.version, "status": report.status})
    return report


@transaction.atomic
def record_payment(*, invoice_id, clinic_id, amount_minor, currency=None, method, reference="", confirmed_received=False, idempotency_key=None, actor, membership):
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.BILLING)
    currency = currency or invoice.currency
    method = method.strip().lower()
    reference = reference.strip()
    if idempotency_key:
        existing = Payment.objects.filter(clinic_id=clinic_id, idempotency_key=idempotency_key).first()
        if existing:
            same_request = (
                existing.invoice_id == invoice.pk
                and existing.amount_minor == amount_minor
                and existing.currency == currency
                and existing.method == method
                and existing.reference == reference
                and existing.confirmed_received == bool(confirmed_received)
                and existing.recorded_by_id == actor.pk
            )
            if same_request:
                return existing
            raise ValidationError("This payment request key has already been used for different payment details.")
    if invoice.status not in {Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID}:
        raise ValidationError("Payments can be recorded only against an active issued invoice.")
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", method):
        raise ValidationError("Payment method must be a short lowercase code.")
    if method == "upi" and not confirmed_received:
        raise ValidationError("Confirm UPI receipt manually before recording it as received.")
    if amount_minor <= 0 or amount_minor > invoice.balance_minor:
        raise ValidationError("Payment must be positive and cannot exceed the outstanding balance.")
    if currency != invoice.currency:
        raise ValidationError("Payment currency must match the invoice.")
    payment = Payment(
        clinic_id=clinic_id, invoice=invoice, amount_minor=amount_minor,
        currency=currency, method=method, reference=reference, idempotency_key=idempotency_key, recorded_by=actor,
        confirmed_received=bool(confirmed_received), confirmed_by=actor if confirmed_received else None,
        confirmed_at=timezone.now() if confirmed_received else None,
    )
    payment._billing_service = True
    payment.save()
    _audit(clinic_id, actor, "payment.recorded", payment, {"amount_minor": amount_minor, "invoice_id": str(invoice.pk)})
    _refresh_invoice_financial_status(invoice, actor)
    return payment


def _refresh_invoice_financial_status(invoice, actor):
    invoice.refresh_from_db()
    payments = invoice.payments.filter(reversed_at__isnull=True).aggregate(total=Sum("amount_minor"))["total"] or 0
    refunds = Refund.objects.filter(payment__invoice_id=invoice.pk).aggregate(total=Sum("amount_minor"))["total"] or 0
    paid = max(0, payments - refunds)
    if payments >= invoice.amount_minor and refunds >= invoice.amount_minor and paid == 0:
        target = Invoice.Status.REFUNDED
    elif paid >= invoice.amount_minor:
        target = Invoice.Status.PAID
    elif paid:
        target = Invoice.Status.PARTIALLY_PAID
    else:
        target = Invoice.Status.ISSUED
    if invoice.status != target:
        previous = invoice.status
        invoice.status = target
        invoice._invoice_transition = True
        invoice.save(update_fields=["status", "updated_at"])
        _audit(invoice.clinic_id, actor, "invoice.status_changed", invoice, {"from": previous, "to": target})


@transaction.atomic
def refund_payment(*, payment_id, clinic_id, amount_minor, reason, actor, membership, reverse_all=False):
    payment = Payment.objects.select_for_update().get(pk=payment_id, clinic_id=clinic_id)
    invoice = Invoice.objects.select_for_update().get(pk=payment.invoice_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.BILLING_REFUND)
    refunded = payment.refunds.aggregate(total=Sum("amount_minor"))["total"] or 0
    remaining = payment.amount_minor - refunded
    if payment.reversed_at or amount_minor <= 0 or amount_minor > remaining:
        raise ValidationError("Refund amount must be positive and cannot exceed the unrefunded payment.")
    refund = Refund(clinic_id=clinic_id, payment=payment, amount_minor=amount_minor, reason=reason.strip(), recorded_by=actor)
    refund._billing_service = True
    refund._domain_service_write = True
    refund.save()
    _audit(clinic_id, actor, "payment.refunded", refund, {"payment_id": str(payment.pk), "invoice_id": str(invoice.pk), "amount_minor": amount_minor, "reason": reason.strip()})
    if reverse_all or amount_minor == remaining:
        _audit(clinic_id, actor, "payment.reversed", payment, {"invoice_id": str(invoice.pk), "refund_id": str(refund.pk)})
    _refresh_invoice_financial_status(invoice, actor)
    return refund


@transaction.atomic
def reverse_payment(*, payment_id, clinic_id, reason, actor, membership):
    payment = Payment.objects.select_for_update().get(pk=payment_id, clinic_id=clinic_id)
    remaining = payment.amount_minor - (payment.refunds.aggregate(total=Sum("amount_minor"))["total"] or 0)
    return refund_payment(payment_id=payment_id, clinic_id=clinic_id, amount_minor=remaining, reason=reason, actor=actor, membership=membership, reverse_all=True)


@transaction.atomic
def record_billing_document_access(*, clinic_id, actor, membership, invoice=None, payment=None):
    _authorize(actor=actor, membership=membership, clinic_id=clinic_id, capability=Capability.BILLING_READ)
    target = invoice or (payment.invoice if payment else None)
    if target is None or target.clinic_id != clinic_id:
        raise ValidationError("Billing document is not available in this clinic.")
    _audit(clinic_id, actor, "billing.document_exported", target, {"kind": "invoice" if invoice else "payment_receipt", "invoice_number": target.invoice_number})


@transaction.atomic
def review_ai_analysis(*, analysis_id, clinic_id, accepted, actor, membership):
    analysis = AIAnalysis.objects.select_for_update().get(pk=analysis_id, clinic_id=clinic_id)
    _authorize(membership, actor, clinic_id, Capability.INTERPRET)
    if analysis.status != AIAnalysis.ReviewStatus.PENDING:
        raise ValidationError("AI analysis has already been reviewed.")
    analysis.status = AIAnalysis.ReviewStatus.ACCEPTED if accepted else AIAnalysis.ReviewStatus.REJECTED
    analysis.reviewed_by = actor
    analysis.reviewed_at = timezone.now()
    analysis._ai_review_transition = True
    analysis.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    _audit(clinic_id, actor, "ai_analysis.reviewed", analysis, {"status": analysis.status})
    return analysis
