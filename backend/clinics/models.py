import uuid
import re
from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone

DICOM_UID_VALIDATOR = RegexValidator(
    regex=r"^(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))*$",
    message="DICOM UIDs must contain numeric components separated by periods.",
)


class ProtectedQuerySet(models.QuerySet):
    """Prevent bulk APIs from skipping model lifecycle and audit checks."""

    def update(self, **kwargs):
        raise ValidationError("Bulk update is disabled for protected records; use a domain service.")

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError("Bulk update is disabled for protected records; use a domain service.")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("Bulk creation is disabled for protected records; use validated saves.")

    def delete(self):
        raise ValidationError("Bulk deletion is disabled for protected records.")


class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TenantModel(UUIDModel):
    objects = ProtectedQuerySet.as_manager()
    require_service_create = False

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self._state.adding and self.require_service_create and not getattr(self, "_domain_service_write", False):
            raise ValidationError("Creation must use the audited domain service.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Protected records are retained; use archive, void, or a domain transition.")


class Clinic(UUIDModel):
    name = models.CharField(max_length=180)
    timezone = models.CharField(max_length=64, default="Asia/Kolkata")
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    default_currency = models.CharField(max_length=3, default="INR")
    invoice_prefix = models.CharField(max_length=16, blank=True)
    is_active = models.BooleanField(default=True)
    next_invoice_number = models.PositiveBigIntegerField(default=1)

    def clean(self):
        if len(self.default_currency) != 3 or not self.default_currency.isalpha() or self.default_currency != self.default_currency.upper():
            raise ValidationError({"default_currency": "Currency must be a three-letter uppercase code."})
        if self.invoice_prefix and not re.fullmatch(r"[A-Z0-9_-]+", self.invoice_prefix):
            raise ValidationError({"invoice_prefix": "Use uppercase letters, numbers, underscores or hyphens."})

    def __str__(self):
        return self.name


class Membership(TenantModel):
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="clinic_memberships")
    professional_registration_number = models.CharField(max_length=120, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["clinic", "user"], name="uniq_clinic_user_membership")]

    def clean(self):
        if self.pk and not getattr(self, "_membership_service", False):
            old = Membership.objects.filter(pk=self.pk).values_list("is_active", flat=True).first()
            if old is not None and old != self.is_active:
                raise ValidationError("Membership activation changes must use the administration service.")

    def has_role(self, role):
        return self.is_active and self.role_grants.filter(role=role, is_active=True).exists()

    def roles(self):
        return tuple(self.role_grants.filter(is_active=True).values_list("role", flat=True))


class MembershipRole(TenantModel):
    require_service_create = True
    class Role(models.TextChoices):
        OWNER = "owner", "Clinic owner"
        ADMIN = "admin", "Clinic administrator"
        RECEPTIONIST = "receptionist", "Receptionist"
        TECHNICIAN = "technician", "Radiology technician"
        RADIOLOGIST = "radiologist", "Radiologist"

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="role_grants")
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="role_grants")
    role = models.CharField(max_length=20, choices=Role.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["membership", "role"], name="uniq_membership_role")]

    def clean(self):
        if self.membership_id and self.clinic_id and self.membership.clinic_id != self.clinic_id:
            raise ValidationError("Role grant and membership must belong to the same clinic.")
        if self.pk and not getattr(self, "_role_service", False):
            old = MembershipRole.objects.filter(pk=self.pk).values_list("role", "is_active").first()
            if old and (old[0] != self.role or old[1] != self.is_active):
                raise ValidationError("Role grants must be changed through the role administration service.")


class AccountSecurityState(models.Model):
    """Security flags for accounts provisioned by clinic administration."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="account_security_state")
    must_change_password = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Patient(TenantModel):
    require_service_create = True
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="patients")
    medical_record_number = models.CharField(max_length=64)
    given_name = models.CharField(max_length=100)
    family_name = models.CharField(max_length=100)
    date_of_birth = models.DateField(null=True, blank=True)
    sex = models.CharField(max_length=32, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["clinic", "medical_record_number"], name="uniq_clinic_mrn")]
        indexes = [models.Index(fields=["clinic", "family_name", "given_name"])]

    def clean(self):
        if self.pk and not getattr(self, "_patient_service", False):
            old = Patient.objects.filter(pk=self.pk).first()
            fields = ("medical_record_number", "given_name", "family_name", "date_of_birth", "sex", "phone", "email", "archived_at", "clinic_id")
            if old and any(getattr(old, name) != getattr(self, name) for name in fields):
                raise ValidationError("Patient changes must use the patient service and create an audit event.")


class ClinicalHistoryEntry(TenantModel):
    """Append-only source text and clinician-reviewed symptom candidates."""
    require_service_create = True

    class ReviewStatus(models.TextChoices):
        PENDING = "pending_review", "Pending clinician review"
        CONFIRMED = "confirmed", "Clinician confirmed"
        REJECTED = "rejected", "Rejected"

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="clinical_history_entries")
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="clinical_history_entries")
    original_text = models.TextField(max_length=10000)
    extracted_mentions = models.JSONField(default=list)
    confirmed_mentions = models.JSONField(null=True, blank=True)
    extractor_name = models.CharField(max_length=120)
    extractor_version = models.CharField(max_length=40)
    provenance = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=ReviewStatus.choices, default=ReviewStatus.PENDING)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_clinical_history_entries")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="reviewed_clinical_history_entries")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    def clean(self):
        if self.patient_id and self.clinic_id and self.patient.clinic_id != self.clinic_id:
            raise ValidationError("Patient and clinical history must belong to the same clinic.")
        if self.status == self.ReviewStatus.PENDING and (self.reviewed_by_id or self.reviewed_at or self.confirmed_mentions is not None):
            raise ValidationError("Pending history cannot contain review data.")
        if self.status == self.ReviewStatus.CONFIRMED and (not self.reviewed_by_id or not self.reviewed_at or not isinstance(self.confirmed_mentions, list)):
            raise ValidationError("Confirmed history requires reviewer, time and confirmed mentions.")
        if self.status == self.ReviewStatus.REJECTED and (not self.reviewed_by_id or not self.reviewed_at or self.confirmed_mentions is not None):
            raise ValidationError("Rejected history requires reviewer and time and cannot contain confirmed mentions.")
        if self.pk:
            old = ClinicalHistoryEntry.objects.filter(pk=self.pk).first()
            immutable = ("clinic_id", "patient_id", "original_text", "extracted_mentions", "extractor_name", "extractor_version", "provenance", "created_by_id")
            if old and any(getattr(old, field) != getattr(self, field) for field in immutable):
                raise ValidationError("Original clinical text, extraction and provenance are immutable.")
            if old and old.status != self.status and not getattr(self, "_history_review_transition", False):
                raise ValidationError("History review state must change through the review service.")
            if old and old.status != self.ReviewStatus.PENDING:
                raise ValidationError("Reviewed clinical history is immutable; append a new entry to correct it.")


class Appointment(TenantModel):
    require_service_create = True
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        CHECKED_IN = "checked_in", "Checked in"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No show"

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="appointments")
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="appointments")
    starts_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    reason = models.CharField(max_length=240, blank=True)

    def clean(self):
        if self.patient_id and self.clinic_id and self.patient.clinic_id != self.clinic_id:
            raise ValidationError("Patient and appointment must belong to the same clinic.")
        if self.pk:
            old = Appointment.objects.filter(pk=self.pk).first()
            if old and old.status != self.status and not getattr(self, "_workflow_transition", False):
                raise ValidationError("Appointment status changes must use the workflow service.")
            if old and any(getattr(old, name) != getattr(self, name) for name in ("starts_at", "reason", "patient_id", "clinic_id")) and not getattr(self, "_appointment_change", False):
                raise ValidationError("Appointment changes must use the scheduling service and create an audit event.")


class Examination(TenantModel):
    require_service_create = True
    class Status(models.TextChoices):
        ORDERED = "ordered", "Ordered"
        CHECKED_IN = "checked_in", "Checked in"
        IN_PROGRESS = "in_progress", "In progress"
        ACQUIRED = "acquired", "Acquired"
        REPORTING = "reporting", "Reporting"
        REPORTED = "reported", "Reported"
        RELEASED = "released", "Released"
        DELIVERED = "delivered", "Delivered"
        CANCELLED = "cancelled", "Cancelled"

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="examinations")
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="examinations")
    appointment = models.ForeignKey(Appointment, null=True, blank=True, on_delete=models.PROTECT, related_name="examinations")
    clinical_service = models.ForeignKey("ClinicalService", null=True, blank=True, on_delete=models.PROTECT, related_name="examinations")
    service_code_snapshot = models.CharField(max_length=40, blank=True)
    modality = models.CharField(max_length=32)
    procedure_name = models.CharField(max_length=180)
    clinical_indication = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ORDERED)
    ordering_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="ordered_examinations")

    class Meta:
        indexes = [models.Index(fields=["clinic", "status", "created_at"])]

    def clean(self):
        if self.patient_id and self.clinic_id and self.patient.clinic_id != self.clinic_id:
            raise ValidationError("Patient and examination must belong to the same clinic.")
        if self.appointment_id and self.clinic_id:
            if self.appointment.clinic_id != self.clinic_id or self.appointment.patient_id != self.patient_id:
                raise ValidationError("Appointment, patient, and examination must agree within one clinic.")
        if self.clinical_service_id and self.clinic_id:
            if self.clinical_service.clinic_id != self.clinic_id:
                raise ValidationError("Clinical service and examination must belong to the same clinic.")
        if self.pk and not getattr(self, "_workflow_transition", False):
            old = Examination.objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if old and old != self.status:
                raise ValidationError("Examination status changes must use the workflow service.")
        if self.pk and not getattr(self, "_exam_update", False):
            old = Examination.objects.filter(pk=self.pk).first()
            fields = ("patient_id", "appointment_id", "clinical_service_id", "service_code_snapshot", "modality", "procedure_name", "clinical_indication", "ordering_user_id", "clinic_id")
            if old and any(getattr(old, name) != getattr(self, name) for name in fields):
                raise ValidationError("Examination order changes must use a clinical service and create an audit event.")


class ExaminationAssignment(TenantModel):
    """Current staff assignment; changes are service-only and audit logged."""
    require_service_create = True
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="examination_assignments")
    examination = models.OneToOneField(Examination, on_delete=models.PROTECT, related_name="assignment")
    technician = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="technician_examination_assignments")
    radiologist = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="radiologist_examination_assignments")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_examination_assignments")

    class Meta:
        indexes = [models.Index(fields=["clinic", "technician"]), models.Index(fields=["clinic", "radiologist"])]

    def clean(self):
        if not self.technician_id and not self.radiologist_id:
            raise ValidationError("Assign at least one technician or radiologist.")
        if self.examination_id and self.clinic_id and self.examination.clinic_id != self.clinic_id:
            raise ValidationError("Assignment and examination must belong to the same clinic.")
        if self.radiologist_id and self.clinic_id and not MembershipRole.objects.filter(
            clinic_id=self.clinic_id, membership__clinic_id=self.clinic_id,
            membership__user_id=self.radiologist_id, membership__is_active=True,
            membership__clinic__is_active=True, role=MembershipRole.Role.RADIOLOGIST, is_active=True,
        ).exists():
            raise ValidationError({"radiologist": "The assigned radiologist must have an active radiologist role in this clinic."})
        if self.technician_id and self.clinic_id and not MembershipRole.objects.filter(
            clinic_id=self.clinic_id, membership__clinic_id=self.clinic_id,
            membership__user_id=self.technician_id, membership__is_active=True,
            membership__clinic__is_active=True, role=MembershipRole.Role.TECHNICIAN, is_active=True,
        ).exists():
            raise ValidationError({"technician": "The assigned technician must have an active technician role in this clinic."})
        if self.assigned_by_id and self.clinic_id and not Membership.objects.filter(
            clinic_id=self.clinic_id, user_id=self.assigned_by_id, is_active=True, clinic__is_active=True,
        ).exists():
            raise ValidationError({"assigned_by": "The assigning user must have an active membership in this clinic."})
        if self.pk and not getattr(self, "_assignment_service_write", False):
            old = ExaminationAssignment.objects.filter(pk=self.pk).first()
            fields = ("clinic_id", "examination_id", "technician_id", "radiologist_id", "assigned_by_id")
            if old and old.status != self.Status.DRAFT and any(getattr(old, field) != getattr(self, field) for field in fields):
                raise ValidationError("Examination assignments must change through the audited assignment service.")


class ImagingStudy(TenantModel):
    """One imaging study/acquisition linked to an examination; not a DICOM viewer."""
    require_service_create = True
    class Kind(models.TextChoices):
        DICOM = "dicom", "DICOM"
        RASTER = "raster", "Raster image"

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="imaging_studies")
    examination = models.ForeignKey(Examination, on_delete=models.PROTECT, related_name="imaging_studies")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    modality = models.CharField(max_length=32, blank=True)
    study_instance_uid = models.CharField(max_length=64, blank=True, validators=[DICOM_UID_VALIDATOR])
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["clinic", "study_instance_uid"], condition=~models.Q(study_instance_uid=""), name="uniq_clinic_study_uid")]

    @property
    def instance_total(self):
        return ImagingInstance.objects.filter(clinic_id=self.clinic_id, series__study_id=self.pk).count()

    def clean(self):
        if self.examination_id and self.clinic_id and self.examination.clinic_id != self.clinic_id:
            raise ValidationError("Examination and imaging study must belong to the same clinic.")
        if self.kind == self.Kind.DICOM and not self.study_instance_uid:
            raise ValidationError("DICOM studies require a Study Instance UID.")
        if self.pk:
            old = ImagingStudy.objects.filter(pk=self.pk).first()
            identity = ("clinic_id", "examination_id", "kind", "study_instance_uid")
            if old and any(getattr(old, name) != getattr(self, name) for name in identity):
                raise ValidationError("Imaging study identity is immutable.")


class ImagingSeries(TenantModel):
    require_service_create = True
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="imaging_series")
    study = models.ForeignKey(ImagingStudy, on_delete=models.PROTECT, related_name="series")
    series_instance_uid = models.CharField(max_length=64, blank=True, validators=[DICOM_UID_VALIDATOR])
    modality = models.CharField(max_length=32, blank=True)
    series_number = models.PositiveIntegerField(null=True, blank=True)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["clinic", "series_instance_uid"], condition=~models.Q(series_instance_uid=""), name="uniq_clinic_series_uid")]

    @property
    def instance_total(self):
        return ImagingInstance.objects.filter(clinic_id=self.clinic_id, series_id=self.pk).count()

    def clean(self):
        if self.study_id and self.clinic_id and self.study.clinic_id != self.clinic_id:
            raise ValidationError("Series and study must belong to the same clinic.")
        if self.study_id and self.study.kind == ImagingStudy.Kind.DICOM and not self.series_instance_uid:
            raise ValidationError("DICOM series require a Series Instance UID.")
        if self.series_instance_uid and self.study_id and self.study.kind != ImagingStudy.Kind.DICOM:
            raise ValidationError("DICOM series UIDs require a DICOM imaging study.")
        if self.pk:
            old = ImagingSeries.objects.filter(pk=self.pk).first()
            identity = ("clinic_id", "study_id", "series_instance_uid")
            if old and any(getattr(old, name) != getattr(self, name) for name in identity):
                raise ValidationError("Imaging series identity is immutable.")


class ImagingInstance(TenantModel):
    require_service_create = True
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="imaging_instances")
    series = models.ForeignKey(ImagingSeries, on_delete=models.PROTECT, related_name="instances")
    sop_instance_uid = models.CharField(max_length=64, blank=True, validators=[DICOM_UID_VALIDATOR])
    storage_key = models.CharField(max_length=512, unique=True)
    media_type = models.CharField(max_length=120)
    original_name = models.CharField(max_length=255)
    byte_size = models.PositiveBigIntegerField()
    checksum_sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_imaging_instances")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["clinic", "sop_instance_uid"], condition=~models.Q(sop_instance_uid=""), name="uniq_clinic_sop_uid")]

    def clean(self):
        if self.series_id and self.clinic_id and self.series.clinic_id != self.clinic_id:
            raise ValidationError("Instance and series must belong to the same clinic.")
        if self.series_id and self.series.study.kind == ImagingStudy.Kind.DICOM and not self.sop_instance_uid:
            raise ValidationError("DICOM instances require a SOP Instance UID.")
        if not re.fullmatch(r"images/[0-9a-f]{32}", self.storage_key or ""):
            raise ValidationError({"storage_key": "Store an opaque generated private image key."})
        if self.byte_size <= 0:
            raise ValidationError({"byte_size": "Imaging file size must be greater than zero."})
        if len(self.checksum_sha256) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in self.checksum_sha256):
            raise ValidationError({"checksum_sha256": "Expected a 64-character SHA-256 hex digest."})
        if self.sop_instance_uid and self.series_id and self.series.study.kind != ImagingStudy.Kind.DICOM:
            raise ValidationError("DICOM instance UIDs require a DICOM imaging study.")
        if self.series_id:
            study_kind = self.series.study.kind
            if study_kind == ImagingStudy.Kind.DICOM and self.media_type != "application/dicom":
                raise ValidationError({"media_type": "DICOM studies must contain DICOM files."})
            if study_kind == ImagingStudy.Kind.RASTER and self.media_type not in {"image/jpeg", "image/png"}:
                raise ValidationError({"media_type": "Raster studies accept JPEG or PNG files."})
        if self.uploaded_by_id and self.clinic_id and not Membership.objects.filter(
            clinic_id=self.clinic_id, user_id=self.uploaded_by_id,
            is_active=True, clinic__is_active=True,
        ).exists():
            raise ValidationError({"uploaded_by": "Uploader must have an active membership in this clinic."})
        if self.pk:
            old = ImagingInstance.objects.filter(pk=self.pk).first()
            identity = ("clinic_id", "series_id", "sop_instance_uid", "storage_key", "media_type", "original_name", "byte_size", "checksum_sha256", "uploaded_by_id", "metadata")
            if old and any(getattr(old, name) != getattr(self, name) for name in identity):
                raise ValidationError("Imaging instance identity and file metadata are immutable.")


class Report(TenantModel):
    require_service_create = True
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SIGNED = "signed", "Signed"
        RELEASED = "released", "Released"
        DELIVERED = "delivered", "Delivered"

    class Template(models.TextChoices):
        XRAY = "xray", "X-ray"
        CT = "ct", "CT"
        MRI = "mri", "MRI"
        ULTRASOUND = "ultrasound", "Ultrasound"
        GENERAL = "general", "General"

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="reports")
    examination = models.ForeignKey(Examination, on_delete=models.PROTECT, related_name="reports")
    version = models.PositiveIntegerField()
    amends = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="amendments")
    template = models.CharField(max_length=16, choices=Template.choices, default=Template.GENERAL)
    technique = models.TextField(blank=True)
    sequences = models.TextField(blank=True)
    comparison = models.TextField(blank=True)
    findings = models.TextField(blank=True)
    impression = models.TextField(blank=True)
    additional_comments = models.TextField(blank=True)
    signed_context = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_reports")
    signed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="signed_reports")
    signed_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="released_reports")
    released_at = models.DateTimeField(null=True, blank=True)
    delivered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="delivered_reports")
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["examination", "version"], name="uniq_examination_report_version"),
            models.UniqueConstraint(fields=["amends"], condition=models.Q(amends__isnull=False), name="uniq_report_amendment_successor"),
        ]
        ordering = ["examination_id", "version"]

    def clean(self):
        if self.examination_id and self.clinic_id and self.examination.clinic_id != self.clinic_id:
            raise ValidationError("Examination and report must belong to the same clinic.")
        if self.amends_id and self.examination_id:
            if self.amends.examination_id != self.examination_id or self.amends.clinic_id != self.clinic_id or self.amends.version + 1 != self.version:
                raise ValidationError("An amendment must be the next version of the same examination and clinic.")
            if self.amends.status == self.Status.DRAFT:
                raise ValidationError("Only a signed or released report can be amended.")
        elif self.version != 1:
            raise ValidationError("Versions after the first must amend the immediately prior report.")
        if self.pk:
            previous = Report.objects.filter(pk=self.pk).first()
            if previous:
                signed_content = ("template", "technique", "sequences", "comparison", "findings", "impression", "additional_comments", "signed_context", "version", "amends_id", "signed_by_id", "signed_at", "created_by_id", "examination_id", "clinic_id")
                if previous.status in {self.Status.SIGNED, self.Status.RELEASED} and any(getattr(previous, name) != getattr(self, name) for name in signed_content):
                    raise ValidationError("Signed report versions are immutable; create an amendment.")
                if previous.status == self.Status.RELEASED and (previous.released_by_id != self.released_by_id or previous.released_at != self.released_at):
                    raise ValidationError("Released report metadata is immutable.")
                if previous.status == self.Status.DELIVERED and (previous.delivered_by_id != self.delivered_by_id or previous.delivered_at != self.delivered_at or self.status != self.Status.DELIVERED):
                    raise ValidationError("Delivered report versions are immutable.")
                if previous.status == self.Status.SIGNED and (previous.released_by_id != self.released_by_id or previous.released_at != self.released_at) and not getattr(self, "_report_transition", False):
                    raise ValidationError("Report release metadata must be set by the release service.")
                if previous.status == self.Status.RELEASED and self.status not in {self.Status.RELEASED, self.Status.DELIVERED}:
                    raise ValidationError("Released reports cannot return to an earlier state.")
                if previous.status == self.Status.DELIVERED and self.status != self.Status.DELIVERED:
                    raise ValidationError("Delivered reports cannot return to an earlier state.")
                if previous.status == self.Status.SIGNED and self.status not in {self.Status.SIGNED, self.Status.RELEASED}:
                    raise ValidationError("Signed reports cannot return to draft.")
                if previous.status != self.status and not getattr(self, "_report_transition", False):
                    raise ValidationError("Report status changes must use the reporting service.")
                draft_content = ("template", "technique", "sequences", "comparison", "findings", "impression", "additional_comments", "signed_context")
                if previous.status == self.Status.DRAFT and any(getattr(previous, name) != getattr(self, name) for name in draft_content) and not (getattr(self, "_report_content_edit", False) or getattr(self, "_report_transition", False)):
                    raise ValidationError("Draft report changes must use the reporting service and create an audit event.")
        elif self.status != self.Status.DRAFT:
            raise ValidationError("Reports must be created as drafts.")


class AIAnalysis(TenantModel):
    class ReviewStatus(models.TextChoices):
        PENDING = "pending_review", "Pending review"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="ai_analyses")
    examination = models.ForeignKey(Examination, on_delete=models.PROTECT, related_name="ai_analyses")
    imaging_study = models.ForeignKey(ImagingStudy, on_delete=models.PROTECT, related_name="ai_analyses")
    provider = models.CharField(max_length=120)
    model_name = models.CharField(max_length=120)
    model_version = models.CharField(max_length=120)
    task = models.CharField(max_length=120)
    output = models.JSONField()
    uncertainty = models.JSONField(null=True, blank=True)
    provenance = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=ReviewStatus.choices, default=ReviewStatus.PENDING)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="reviewed_ai_analyses")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.pk and not getattr(self, "_ai_ingest", False):
            raise ValidationError("AI results must be recorded through the provenance and audit service.")
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self):
        if self.examination_id and self.clinic_id and self.examination.clinic_id != self.clinic_id:
            raise ValidationError("Examination and AI analysis must belong to the same clinic.")
        if self.imaging_study_id and self.clinic_id and self.imaging_study.clinic_id != self.clinic_id:
            raise ValidationError("Imaging study and AI analysis must belong to the same clinic.")
        if self.imaging_study_id and self.examination_id and self.imaging_study.examination_id != self.examination_id:
            raise ValidationError("AI analysis must reference imaging from its examination.")
        if not self.provenance:
            raise ValidationError({"provenance": "Model and input provenance are required for AI output."})
        if self.pk:
            old = AIAnalysis.objects.filter(pk=self.pk).first()
            if old and (old.output != self.output or old.provider != self.provider or old.model_name != self.model_name or old.model_version != self.model_version or old.uncertainty != self.uncertainty or old.provenance != self.provenance):
                raise ValidationError("AI output and provenance are immutable; create a new analysis.")
            if old and old.status != self.status and not getattr(self, "_ai_review_transition", False):
                raise ValidationError("AI review changes must use the review service.")


class AIReportAssist(TenantModel):
    """Immutable, non-authoritative evidence-assisted draft and verification record."""
    require_service_create = True

    class ReviewStatus(models.TextChoices):
        PENDING = "pending_review", "Pending review"
        ACCEPTED = "accepted", "Accepted for manual draft review"
        REJECTED = "rejected", "Rejected"

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="ai_report_assists")
    examination = models.ForeignKey(Examination, on_delete=models.PROTECT, related_name="ai_report_assists")
    report = models.ForeignKey(Report, on_delete=models.PROTECT, related_name="ai_assists")
    provider = models.CharField(max_length=120, default="local")
    model_name = models.CharField(max_length=120)
    model_version = models.CharField(max_length=120)
    evidence_input = models.JSONField()
    generated_sections = models.JSONField()
    verification = models.JSONField()
    provenance = models.JSONField(default=dict)
    review_status = models.CharField(max_length=20, choices=ReviewStatus.choices, default=ReviewStatus.PENDING)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="reviewed_ai_report_assists")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    def clean(self):
        if self.examination_id and self.clinic_id and self.examination.clinic_id != self.clinic_id:
            raise ValidationError("Examination and AI report assist must belong to the same clinic.")
        if self.report_id and (self.report.clinic_id != self.clinic_id or self.report.examination_id != self.examination_id):
            raise ValidationError("AI report assist must reference its clinic examination report.")
        if self.report_id and self.report.status != Report.Status.DRAFT:
            raise ValidationError("AI assistance is available only for editable report drafts.")
        if self.review_status == self.ReviewStatus.PENDING and (self.reviewed_by_id or self.reviewed_at):
            raise ValidationError("Pending AI assistance cannot contain review metadata.")
        if self.review_status != self.ReviewStatus.PENDING and (not self.reviewed_by_id or not self.reviewed_at):
            raise ValidationError("Reviewed AI assistance requires its reviewer and review time.")
        if self.pk:
            old = AIReportAssist.objects.filter(pk=self.pk).first()
            if old and any(getattr(old, key) != getattr(self, key) for key in ("clinic_id", "examination_id", "report_id", "provider", "model_name", "model_version", "evidence_input", "generated_sections", "verification", "provenance")):
                raise ValidationError("AI report evidence, output and provenance are immutable.")
            if old and old.review_status != self.review_status and not getattr(self, "_ai_report_review_transition", False):
                raise ValidationError("AI assistance review changes must use the review service.")
            if old and old.review_status != self.ReviewStatus.PENDING:
                raise ValidationError("AI assistance review is final.")
            if old and (old.reviewed_by_id != self.reviewed_by_id or old.reviewed_at != self.reviewed_at) and not getattr(self, "_ai_report_review_transition", False):
                raise ValidationError("AI review metadata must be set through the review service.")


class ChargeItem(TenantModel):
    require_service_create = True
    class Category(models.TextChoices):
        CONSULTATION = "consultation", "Consultation"
        XRAY = "xray", "X-ray"
        CT = "ct", "CT"
        MRI = "mri", "MRI"
        ULTRASOUND = "ultrasound", "Ultrasound"
        OTHER = "other", "Other service"

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="charge_items")
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=180)
    description = models.CharField(max_length=240, blank=True)
    category = models.CharField(max_length=20, choices=Category.choices)
    unit_price_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default="INR")
    tax_rate_bps = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    configured_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="configured_charge_items")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["clinic", "code"], name="uniq_clinic_charge_code")]
        ordering = ["category", "name"]

    def clean(self):
        if self.unit_price_minor <= 0:
            raise ValidationError({"unit_price_minor": "Charge must be greater than zero."})
        if self.tax_rate_bps > 10000:
            raise ValidationError({"tax_rate_bps": "Tax rate cannot exceed 100 percent."})
        if len(self.currency) != 3 or not self.currency.isalpha() or self.currency != self.currency.upper():
            raise ValidationError({"currency": "Currency must be a three-letter uppercase code."})

    def save(self, *args, **kwargs):
        old = ChargeItem.objects.filter(pk=self.pk).first() if self.pk else None
        fields = ("code", "name", "description", "category", "unit_price_minor", "currency", "tax_rate_bps", "is_active", "clinic_id")
        if old and any(getattr(old, field) != getattr(self, field) for field in fields) and not getattr(self, "_billing_service", False):
            raise ValidationError("Charge catalogue changes must use the audited billing service.")
        super().save(*args, **kwargs)


class ClinicalService(TenantModel):
    """Clinic procedure catalogue; distinct from its optional financial charge."""
    require_service_create = True
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="clinical_services")
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=180)
    modality = models.CharField(max_length=32)
    description = models.CharField(max_length=240, blank=True)
    linked_charge = models.ForeignKey(ChargeItem, null=True, blank=True, on_delete=models.PROTECT, related_name="clinical_services")
    is_active = models.BooleanField(default=True)
    configured_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="configured_clinical_services")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["clinic", "code"], name="uniq_clinic_clinical_service_code")]
        ordering = ["name"]

    def clean(self):
        if self.linked_charge_id and self.linked_charge.clinic_id != self.clinic_id:
            raise ValidationError("Linked charge must belong to this clinic.")
        if self.pk:
            old = ClinicalService.objects.filter(pk=self.pk).first()
            fields = ("code", "name", "modality", "description", "linked_charge_id", "is_active", "clinic_id")
            if old and any(getattr(old, field) != getattr(self, field) for field in fields) and not getattr(self, "_service_catalogue_write", False):
                raise ValidationError("Clinical services must be changed through the audited service.")


class Invoice(TenantModel):
    require_service_create = True
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ISSUED = "issued", "Issued"
        PARTIALLY_PAID = "partially_paid", "Partially paid"
        PAID = "paid", "Paid"
        CANCELLED = "cancelled", "Cancelled"
        REFUNDED = "refunded", "Refunded"

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="invoices")
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="invoices")
    examination = models.ForeignKey(Examination, null=True, blank=True, on_delete=models.PROTECT, related_name="invoices")
    invoice_number = models.CharField(max_length=48, blank=True)
    invoice_date = models.DateField(default=timezone.localdate)
    issued_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="issued_invoices")
    gross_amount_minor = models.PositiveBigIntegerField(default=0)
    discount_minor = models.PositiveBigIntegerField(default=0)
    tax_minor = models.PositiveBigIntegerField(default=0)
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    clinic_context = models.JSONField(default=dict, blank=True)
    patient_context = models.JSONField(default=dict, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="cancelled_invoices")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=240, blank=True)

    @property
    def paid_minor(self):
        payments = self.payments.filter(reversed_at__isnull=True).aggregate(total=Sum("amount_minor"))["total"] or 0
        refunds = Refund.objects.filter(payment__invoice_id=self.pk).aggregate(total=Sum("amount_minor"))["total"] or 0
        return max(0, payments - refunds)

    @property
    def balance_minor(self):
        if self.status in {self.Status.CANCELLED, self.Status.REFUNDED}:
            return 0
        return max(0, self.amount_minor - self.paid_minor)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["clinic", "invoice_number"], condition=~models.Q(invoice_number=""), name="uniq_clinic_invoice_number")]
        ordering = ["-invoice_date", "-created_at"]

    def clean(self):
        if self.amount_minor <= 0:
            raise ValidationError({"amount_minor": "Invoice amount must be greater than zero."})
        if len(self.currency) != 3 or not self.currency.isalpha() or self.currency != self.currency.upper():
            raise ValidationError({"currency": "Currency must be a three-letter uppercase code."})
        if self.patient_id and self.clinic_id and self.patient.clinic_id != self.clinic_id:
            raise ValidationError("Patient and invoice must belong to the same clinic.")
        if self.examination_id and self.clinic_id:
            if self.examination.clinic_id != self.clinic_id or self.examination.patient_id != self.patient_id:
                raise ValidationError("Examination, patient, and invoice must agree within one clinic.")
        if self.pk:
            old = Invoice.objects.filter(pk=self.pk).first()
            if old and old.status != self.status and not getattr(self, "_invoice_transition", False):
                raise ValidationError("Invoice status changes must use the billing service.")
            fields = ("invoice_number", "invoice_date", "issued_at", "issued_by_id", "gross_amount_minor", "discount_minor", "tax_minor", "amount_minor", "currency", "patient_id", "examination_id", "clinic_id", "clinic_context", "patient_context")
            if old and old.status != self.Status.DRAFT and any(getattr(old, field) != getattr(self, field) for field in fields):
                raise ValidationError("Issued invoice terms are immutable; use an audited cancellation and replacement.")
            if old and old.status != self.Status.DRAFT and self.status == self.Status.DRAFT:
                raise ValidationError("Issued invoices cannot return to draft.")


class InvoiceLineItem(TenantModel):
    require_service_create = True
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="invoice_line_items")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="line_items")
    charge_item = models.ForeignKey(ChargeItem, null=True, blank=True, on_delete=models.PROTECT, related_name="invoice_lines")
    description = models.CharField(max_length=180)
    quantity = models.PositiveSmallIntegerField(default=1)
    unit_price_minor = models.PositiveBigIntegerField()
    tax_rate_bps = models.PositiveIntegerField(default=0)
    subtotal_minor = models.PositiveBigIntegerField()
    discount_minor = models.PositiveBigIntegerField(default=0)
    tax_minor = models.PositiveBigIntegerField(default=0)

    def clean(self):
        if self.invoice_id and self.clinic_id and self.invoice.clinic_id != self.clinic_id:
            raise ValidationError("Invoice line and invoice must belong to the same clinic.")
        if self.charge_item_id and self.charge_item.clinic_id != self.clinic_id:
            raise ValidationError("Charge item and invoice line must belong to the same clinic.")
        if self.quantity <= 0 or self.unit_price_minor <= 0:
            raise ValidationError("Invoice line quantity and unit price must be positive.")
        if self.invoice_id and self.invoice.status != Invoice.Status.DRAFT and self._state.adding:
            raise ValidationError("Lines cannot be added after an invoice is issued.")
        if self.invoice_id and self.invoice.status != Invoice.Status.DRAFT and not self._state.adding:
            old = InvoiceLineItem.objects.filter(pk=self.pk).first()
            values = ("description", "quantity", "unit_price_minor", "tax_rate_bps", "subtotal_minor", "discount_minor", "tax_minor", "invoice_id", "clinic_id")
            if old and any(getattr(old, field) != getattr(self, field) for field in values):
                raise ValidationError("Issued invoice lines are immutable.")

    def save(self, *args, **kwargs):
        if self._state.adding and not getattr(self, "_billing_service", False):
            raise ValidationError("Invoice lines must be created through the billing service.")
        self.full_clean()
        super().save(*args, **kwargs)


class Payment(TenantModel):
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="payments")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default="INR")
    method = models.CharField(max_length=32)
    reference = models.CharField(max_length=120, blank=True)
    idempotency_key = models.UUIDField(null=True, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="recorded_payments")
    confirmed_received = models.BooleanField(default=False)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="confirmed_payments")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_payments")
    reversal_reason = models.CharField(max_length=240, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["clinic", "idempotency_key"], condition=models.Q(idempotency_key__isnull=False), name="uniq_payment_idempotency_per_clinic")]

    def clean(self):
        if self.invoice_id and self.clinic_id:
            if self.invoice.clinic_id != self.clinic_id or self.currency != self.invoice.currency:
                raise ValidationError("Payment and invoice must share clinic and currency.")
        if self.amount_minor <= 0:
            raise ValidationError("Payment amount must be greater than zero.")
        if len(self.currency) != 3 or not self.currency.isalpha() or self.currency != self.currency.upper():
            raise ValidationError({"currency": "Currency must be a three-letter uppercase code."})
        if self.pk:
            old = Payment.objects.filter(pk=self.pk).first()
            immutable = ("invoice_id", "clinic_id", "amount_minor", "currency", "method", "reference", "idempotency_key", "recorded_by_id", "confirmed_received", "confirmed_by_id", "confirmed_at")
            if old and any(getattr(old, name) != getattr(self, name) for name in immutable):
                raise ValidationError("Payment records are immutable; reverse and record a correction.")
            if old and old.reversed_at and old.reversed_at != self.reversed_at:
                raise ValidationError("A reversed payment cannot be reinstated or changed.")
            if old and (old.reversed_at != self.reversed_at or old.reversed_by_id != self.reversed_by_id) and not getattr(self, "_billing_service", False):
                raise ValidationError("Payment reversals must use the billing service.")
        elif not getattr(self, "_billing_service", False):
            raise ValidationError("Payments must be recorded through the billing service.")
        if self.reversed_at and not self.reversal_reason.strip():
            raise ValidationError({"reversal_reason": "A reason is required to reverse a payment."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class Refund(TenantModel):
    require_service_create = True
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="refunds")
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    amount_minor = models.PositiveBigIntegerField()
    reason = models.CharField(max_length=240)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="recorded_refunds")

    def clean(self):
        if self.payment_id and self.payment.clinic_id != self.clinic_id:
            raise ValidationError("Refund and payment must belong to the same clinic.")
        if self.amount_minor <= 0 or not self.reason.strip():
            raise ValidationError("Refund amount and reason are required.")
        if not self._state.adding:
            raise ValidationError("Refund records are immutable.")

    class Meta:
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        if self._state.adding and not getattr(self, "_billing_service", False):
            raise ValidationError("Refunds must be recorded through the billing service.")
        self.full_clean()
        super().save(*args, **kwargs)


class AuditEvent(TenantModel):
    require_service_create = True
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="audit_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="clinic_audit_events")
    action = models.CharField(max_length=120)
    object_type = models.CharField(max_length=120)
    object_id = models.CharField(max_length=64)
    details = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.pk and AuditEvent.objects.filter(pk=self.pk).exists():
            raise ValidationError("Audit events are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit events are append-only.")


class AuthenticationEvent(UUIDModel):
    """Platform-level authentication audit; deliberately stores no submitted identifier or IP."""
    objects = ProtectedQuerySet.as_manager()

    class Action(models.TextChoices):
        LOGIN_SUCCEEDED = "login_succeeded", "Login succeeded"
        LOGIN_FAILED = "login_failed", "Login failed"
        LOGOUT = "logout", "Logout"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="authentication_events")
    action = models.CharField(max_length=24, choices=Action.choices)

    def save(self, *args, **kwargs):
        if self._state.adding and not getattr(self, "_auth_audit_write", False):
            raise ValidationError("Authentication events must be recorded through the authentication service.")
        if not self._state.adding:
            raise ValidationError("Authentication events are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Authentication events are append-only.")
