from rest_framework import serializers
from django.db.models import Sum

from clinics.models import (
    AIAnalysis, AIReportAssist, ClinicalHistoryEntry, Appointment, AuditEvent, Clinic,
    Examination, ExaminationAssignment, ImagingInstance, ImagingSeries, ImagingStudy, Invoice,
    Membership, MembershipRole, Patient, Payment, Report, ChargeItem, ClinicalService, InvoiceLineItem, Refund,
)


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, trim_whitespace=False)
    password = serializers.CharField(max_length=256, trim_whitespace=False, write_only=True)


class ClinicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Clinic
        fields = ("id", "name", "timezone", "address", "phone", "email")


class ClinicProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Clinic
        fields = ("name", "address", "phone", "email", "default_currency", "invoice_prefix", "timezone")
        read_only_fields = ("timezone",)

    def validate_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Clinic name cannot be blank.")
        return value.strip()

    def validate_default_currency(self, value):
        return value.upper()

    def validate_invoice_prefix(self, value):
        value = value.strip().upper()
        if value and not __import__("re").fullmatch(r"[A-Z0-9_-]{1,16}", value):
            raise serializers.ValidationError("Use up to 16 uppercase letters, numbers, underscores or hyphens.")
        return value


class MembershipSerializer(serializers.ModelSerializer):
    clinic = ClinicSerializer(read_only=True)
    roles = serializers.SerializerMethodField()
    user_id = serializers.IntegerField(read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = Membership
        fields = ("id", "clinic", "user_id", "username", "email", "full_name", "roles", "professional_registration_number", "is_active")

    def get_roles(self, obj):
        return list(obj.roles())

    def get_full_name(self, obj):
        return obj.user.get_full_name().strip() or obj.user.get_username()


class ActiveClinicSerializer(serializers.Serializer):
    clinic_id = serializers.UUIDField()


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = ("id", "medical_record_number", "given_name", "family_name", "date_of_birth", "sex", "phone", "email", "archived_at", "created_at", "updated_at")
        read_only_fields = ("id", "archived_at", "created_at", "updated_at")


class PatientCreateSerializer(serializers.Serializer):
    medical_record_number = serializers.CharField(max_length=64)
    given_name = serializers.CharField(max_length=100)
    family_name = serializers.CharField(max_length=100)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    sex = serializers.CharField(max_length=32, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=40, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)


class PatientUpdateSerializer(serializers.Serializer):
    given_name = serializers.CharField(max_length=100, required=False)
    family_name = serializers.CharField(max_length=100, required=False)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    sex = serializers.CharField(max_length=32, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=40, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)


class PatientSearchSerializer(serializers.Serializer):
    search = serializers.CharField(required=False, allow_blank=True, max_length=120)


class ClinicalHistoryEntryCreateSerializer(serializers.Serializer):
    original_text = serializers.CharField(max_length=10000, trim_whitespace=False)


class ClinicalHistoryEntryReviewSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=("confirmed", "rejected"))
    confirmed_mentions = serializers.ListField(child=serializers.DictField(), required=False, allow_empty=True, max_length=80)


class ClinicalHistoryEntrySerializer(serializers.ModelSerializer):
    patient_id = serializers.UUIDField(read_only=True)
    created_by_id = serializers.IntegerField(read_only=True)
    reviewed_by_id = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = ClinicalHistoryEntry
        fields = ("id", "patient_id", "original_text", "extracted_mentions", "confirmed_mentions", "extractor_name", "extractor_version", "provenance", "status", "created_by_id", "reviewed_by_id", "reviewed_at", "created_at")
        read_only_fields = fields


class PotentialDuplicateSearchSerializer(serializers.Serializer):
    medical_record_number = serializers.CharField(required=False, allow_blank=True, max_length=64)
    given_name = serializers.CharField(required=False, allow_blank=True, max_length=100)
    family_name = serializers.CharField(required=False, allow_blank=True, max_length=100)
    date_of_birth = serializers.DateField(required=False)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=40)

    def validate(self, attrs):
        by_name = attrs.get("given_name") and attrs.get("family_name") and (attrs.get("date_of_birth") or attrs.get("phone"))
        if not attrs.get("medical_record_number") and not by_name:
            raise serializers.ValidationError("Provide a medical record number or a name with date of birth or phone.")
        return attrs


class ClinicalHistoryImagingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImagingStudy
        fields = ("id", "kind", "modality", "description", "created_at")


class ClinicalHistoryReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = Report
        fields = ("id", "version", "template", "technique", "sequences", "comparison", "findings", "impression", "additional_comments", "status", "released_at", "delivered_at")


class ClinicalHistoryExaminationSerializer(serializers.ModelSerializer):
    released_reports = serializers.SerializerMethodField()
    imaging_studies = ClinicalHistoryImagingSerializer(many=True, read_only=True)

    class Meta:
        model = Examination
        fields = ("id", "modality", "procedure_name", "status", "created_at", "released_reports", "imaging_studies")

    def get_released_reports(self, obj):
        return ClinicalHistoryReportSerializer(obj.reports.filter(status__in=[Report.Status.RELEASED, Report.Status.DELIVERED]).order_by("-version"), many=True).data


class AppointmentSerializer(serializers.ModelSerializer):
    patient_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Appointment
        fields = ("id", "patient_id", "starts_at", "status", "reason", "created_at", "updated_at")
        read_only_fields = fields


class AppointmentCreateSerializer(serializers.Serializer):
    patient_id = serializers.UUIDField()
    starts_at = serializers.DateTimeField()
    reason = serializers.CharField(max_length=240, required=False, allow_blank=True)


class AppointmentActionSerializer(serializers.Serializer):
    target = serializers.ChoiceField(choices=Appointment.Status.choices)


class AppointmentRescheduleSerializer(serializers.Serializer):
    starts_at = serializers.DateTimeField()
    reason = serializers.CharField(max_length=240, required=False, allow_blank=True)


class ExaminationSerializer(serializers.ModelSerializer):
    patient_id = serializers.UUIDField(read_only=True)
    appointment_id = serializers.UUIDField(read_only=True, allow_null=True)
    assignment = serializers.SerializerMethodField()

    class Meta:
        model = Examination
        fields = ("id", "patient_id", "appointment_id", "clinical_service_id", "service_code_snapshot", "modality", "procedure_name", "clinical_indication", "status", "assignment", "created_at", "updated_at")
        read_only_fields = fields

    def get_assignment(self, obj):
        assignment = getattr(obj, "assignment", None)
        if not assignment:
            return None
        return {"technician_id": assignment.technician_id, "radiologist_id": assignment.radiologist_id}


class ExaminationAssignmentWriteSerializer(serializers.Serializer):
    technician_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    radiologist_id = serializers.IntegerField(min_value=1)


class AssigneeSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    role = serializers.CharField()


class ExaminationStatusSerializer(serializers.ModelSerializer):
    """Safe operational fields for reception; excludes indication and procedure."""
    patient_id = serializers.UUIDField(read_only=True)
    appointment_id = serializers.UUIDField(read_only=True, allow_null=True)

    class Meta:
        model = Examination
        fields = ("id", "patient_id", "appointment_id", "modality", "status", "created_at", "updated_at")
        read_only_fields = fields


class ExaminationCreateSerializer(serializers.Serializer):
    patient_id = serializers.UUIDField()
    appointment_id = serializers.UUIDField(required=False, allow_null=True)
    clinical_service_id = serializers.UUIDField(required=False, allow_null=True)
    modality = serializers.CharField(max_length=32, required=False, allow_blank=True)
    procedure_name = serializers.CharField(max_length=180, required=False, allow_blank=True)
    clinical_indication = serializers.CharField(required=False, allow_blank=True)


class ExaminationActionSerializer(serializers.Serializer):
    target = serializers.ChoiceField(choices=Examination.Status.choices)


class ImagingStudySerializer(serializers.ModelSerializer):
    examination_id = serializers.UUIDField(read_only=True)
    instance_count = serializers.IntegerField(source="instance_total", read_only=True)

    class Meta:
        model = ImagingStudy
        fields = ("id", "examination_id", "kind", "modality", "study_instance_uid", "description", "instance_count", "created_at")
        read_only_fields = fields


class ImagingSeriesSerializer(serializers.ModelSerializer):
    study_id = serializers.UUIDField(read_only=True)
    instance_count = serializers.IntegerField(source="instance_total", read_only=True)

    class Meta:
        model = ImagingSeries
        fields = ("id", "study_id", "series_instance_uid", "modality", "series_number", "description", "instance_count", "created_at")
        read_only_fields = fields


class ImagingInstanceSerializer(serializers.ModelSerializer):
    series_id = serializers.UUIDField(read_only=True)
    uploaded_by_id = serializers.IntegerField(read_only=True)
    content_url = serializers.SerializerMethodField()
    upload_status = serializers.SerializerMethodField()

    def get_upload_status(self, obj):
        return "ready"

    def get_content_url(self, obj):
        request = self.context.get("request")
        if request is None:
            return None
        return request.build_absolute_uri(f"/api/v1/imaging-instances/{obj.pk}/content/")

    class Meta:
        model = ImagingInstance
        fields = ("id", "series_id", "sop_instance_uid", "media_type", "original_name", "byte_size", "checksum_sha256", "uploaded_by_id", "content_url", "metadata", "upload_status", "created_at")
        read_only_fields = fields


class ReportSerializer(serializers.ModelSerializer):
    examination_id = serializers.UUIDField(read_only=True)
    amends_id = serializers.UUIDField(read_only=True, allow_null=True)
    created_by_id = serializers.IntegerField(read_only=True)
    signed_by_id = serializers.IntegerField(read_only=True, allow_null=True)
    released_by_id = serializers.IntegerField(read_only=True, allow_null=True)
    delivered_by_id = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = Report
        fields = ("id", "examination_id", "version", "amends_id", "template", "technique", "sequences", "comparison", "findings", "impression", "additional_comments", "signed_context", "status", "created_by_id", "signed_by_id", "signed_at", "released_by_id", "released_at", "delivered_by_id", "delivered_at", "created_at", "updated_at")
        read_only_fields = fields


class ReportCreateSerializer(serializers.Serializer):
    examination_id = serializers.UUIDField()
    amends_id = serializers.UUIDField(required=False, allow_null=True)
    template = serializers.ChoiceField(choices=Report.Template.choices, required=False)


class ReportDraftSerializer(serializers.Serializer):
    expected_updated_at = serializers.DateTimeField(required=False)
    technique = serializers.CharField(allow_blank=True, required=False)
    sequences = serializers.CharField(allow_blank=True, required=False)
    comparison = serializers.CharField(allow_blank=True, required=False)
    findings = serializers.CharField(allow_blank=True, required=False)
    impression = serializers.CharField(allow_blank=True, required=False)
    additional_comments = serializers.CharField(allow_blank=True, required=False)


class ReportVersionActionSerializer(serializers.Serializer):
    expected_updated_at = serializers.DateTimeField()


class MembershipRegistrationSerializer(serializers.Serializer):
    professional_registration_number = serializers.CharField(max_length=120, allow_blank=True)


class AIAnalysisSerializer(serializers.ModelSerializer):
    examination_id = serializers.UUIDField(read_only=True)
    imaging_study_id = serializers.UUIDField(read_only=True)
    reviewed_by_id = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = AIAnalysis
        fields = ("id", "examination_id", "imaging_study_id", "provider", "model_name", "model_version", "task", "output", "uncertainty", "provenance", "status", "reviewed_by_id", "reviewed_at", "created_at")
        read_only_fields = fields


class AIReportAssistInputSerializer(serializers.Serializer):
    clinical_indication = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    technique = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    observations = serializers.ListField(child=serializers.DictField(), min_length=1, max_length=40)


class AIReportAssistReviewSerializer(serializers.Serializer):
    assist_id = serializers.UUIDField()
    decision = serializers.ChoiceField(choices=("accepted", "rejected"))


class AIReportAssistSerializer(serializers.ModelSerializer):
    examination_id = serializers.UUIDField(read_only=True)
    report_id = serializers.UUIDField(read_only=True)
    reviewed_by_id = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = AIReportAssist
        fields = ("id", "examination_id", "report_id", "provider", "model_name", "model_version", "evidence_input", "generated_sections", "verification", "provenance", "review_status", "reviewed_by_id", "reviewed_at", "created_at")
        read_only_fields = fields


class InvoiceLineItemSerializer(serializers.ModelSerializer):
    charge_item_id = serializers.UUIDField(read_only=True, allow_null=True)

    class Meta:
        model = InvoiceLineItem
        fields = ("id", "charge_item_id", "description", "quantity", "unit_price_minor", "tax_rate_bps", "subtotal_minor", "discount_minor", "tax_minor")
        read_only_fields = fields


class ChargeItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChargeItem
        fields = ("id", "code", "name", "description", "category", "unit_price_minor", "currency", "tax_rate_bps", "is_active")
        read_only_fields = ("id",)


class InvoiceSerializer(serializers.ModelSerializer):
    patient_id = serializers.UUIDField(read_only=True)
    examination_id = serializers.UUIDField(read_only=True, allow_null=True)
    paid_minor = serializers.IntegerField(read_only=True)
    balance_minor = serializers.IntegerField(read_only=True)
    line_items = InvoiceLineItemSerializer(many=True, read_only=True)

    class Meta:
        model = Invoice
        fields = ("id", "invoice_number", "patient_id", "examination_id", "invoice_date", "issued_at", "issued_by_id", "patient_context", "line_items", "gross_amount_minor", "discount_minor", "tax_minor", "amount_minor", "currency", "status", "paid_minor", "balance_minor", "cancelled_by_id", "cancelled_at", "cancellation_reason", "created_at")
        read_only_fields = fields


class InvoiceCreateSerializer(serializers.Serializer):
    patient_id = serializers.UUIDField()
    examination_id = serializers.UUIDField(required=False, allow_null=True)
    line_items = serializers.ListField(child=serializers.DictField(), allow_empty=False)
    discount_minor = serializers.IntegerField(min_value=0, default=0)

    def validate_line_items(self, rows):
        normalized = []
        for row in rows:
            serializer = InvoiceChargeLineInputSerializer(data=row)
            serializer.is_valid(raise_exception=True)
            normalized.append(serializer.validated_data)
        return normalized


class InvoiceChargeLineInputSerializer(serializers.Serializer):
    charge_item_id = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1, max_value=1000)


class PaymentSerializer(serializers.ModelSerializer):
    invoice_id = serializers.UUIDField(read_only=True)
    recorded_by_id = serializers.IntegerField(read_only=True)
    refunded_minor = serializers.SerializerMethodField()
    refunds = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = ("id", "invoice_id", "amount_minor", "currency", "method", "reference", "recorded_by_id", "confirmed_received", "confirmed_by_id", "confirmed_at", "refunded_minor", "refunds", "reversed_at", "reversed_by_id", "reversal_reason", "created_at")
        read_only_fields = fields

    def get_refunded_minor(self, obj):
        return obj.refunds.aggregate(total=Sum("amount_minor"))["total"] or 0

    def get_refunds(self, obj):
        return RefundSerializer(obj.refunds.all(), many=True).data


class PaymentCreateSerializer(serializers.Serializer):
    amount_minor = serializers.IntegerField(min_value=1)
    currency = serializers.RegexField(r"^[A-Z]{3}$", required=False)
    method = serializers.RegexField(r"^[a-z0-9_-]{1,32}$")
    reference = serializers.CharField(max_length=120, required=False, allow_blank=True)
    confirmed_received = serializers.BooleanField(default=False)
    idempotency_key = serializers.UUIDField()


class PaymentReverseSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=240)


class RefundCreateSerializer(serializers.Serializer):
    amount_minor = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(max_length=240)


class InvoiceCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=240)


class ChargeItemCreateSerializer(serializers.Serializer):
    code = serializers.RegexField(r"^[A-Za-z0-9_-]{1,40}$")
    name = serializers.CharField(max_length=180)
    description = serializers.CharField(max_length=240, required=False, allow_blank=True)
    category = serializers.ChoiceField(choices=ChargeItem.Category.choices)
    unit_price_minor = serializers.IntegerField(min_value=1)
    currency = serializers.RegexField(r"^[A-Z]{3}$", required=False)
    tax_rate_bps = serializers.IntegerField(min_value=0, max_value=10000, default=0)


class ChargeItemUpdateSerializer(serializers.Serializer):
    code = serializers.RegexField(r"^[A-Za-z0-9_-]{1,40}$", required=False)
    name = serializers.CharField(max_length=180, required=False)
    description = serializers.CharField(max_length=240, required=False, allow_blank=True)
    category = serializers.ChoiceField(choices=ChargeItem.Category.choices, required=False)
    unit_price_minor = serializers.IntegerField(min_value=1, required=False)
    currency = serializers.RegexField(r"^[A-Z]{3}$", required=False)
    tax_rate_bps = serializers.IntegerField(min_value=0, max_value=10000, required=False)
    is_active = serializers.BooleanField(required=False)


class RefundSerializer(serializers.ModelSerializer):
    payment_id = serializers.UUIDField(read_only=True)
    recorded_by_id = serializers.IntegerField(read_only=True)
    class Meta:
        model = Refund
        fields = ("id", "payment_id", "amount_minor", "reason", "recorded_by_id", "created_at")
        read_only_fields = fields


class RoleAssignmentSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=MembershipRole.Role.choices)


class MembershipActiveSerializer(serializers.Serializer):
    is_active = serializers.BooleanField()


class MembershipCreateSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(min_value=1)
    roles = serializers.ListField(
        child=serializers.ChoiceField(choices=MembershipRole.Role.choices),
        min_length=1, max_length=len(MembershipRole.Role.choices),
    )


class StaffAccountCreateSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, trim_whitespace=True)
    email = serializers.EmailField(max_length=254)
    full_name = serializers.CharField(max_length=300, trim_whitespace=True)
    roles = serializers.ListField(
        child=serializers.ChoiceField(choices=[
            (MembershipRole.Role.ADMIN, "Clinic administrator"),
            (MembershipRole.Role.RECEPTIONIST, "Receptionist"),
            (MembershipRole.Role.TECHNICIAN, "Radiology technician"),
            (MembershipRole.Role.RADIOLOGIST, "Radiologist"),
        ]), min_length=1, max_length=4,
    )

    def validate_username(self, value):
        return value.strip()

    def validate_full_name(self, value):
        value = " ".join(value.split())
        if not value:
            raise serializers.ValidationError("Enter the staff member's full name.")
        return value

    def validate_roles(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError("Roles must be unique.")
        return value


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(max_length=256, trim_whitespace=False, write_only=True)
    new_password = serializers.CharField(max_length=256, trim_whitespace=False, write_only=True)


class AuditEventSerializer(serializers.ModelSerializer):
    details = serializers.SerializerMethodField()
    class Meta:
        model = AuditEvent
        fields = ("id", "actor_id", "action", "object_type", "object_id", "details", "occurred_at")
        read_only_fields = fields

    def get_details(self, obj):
        # Audit metadata can contain identifiers or clinical data. Return only known
        # operational fields; omit arbitrary payloads rather than risk disclosure.
        allowed = {"fields", "role", "roles", "active", "code", "category", "unit_price_minor", "currency", "from_membership_id", "to_membership_id", "membership_id"}
        return {key: value for key, value in (obj.details or {}).items() if key in allowed}


class ClinicalServiceSerializer(serializers.ModelSerializer):
    linked_charge_id = serializers.UUIDField(read_only=True, allow_null=True)
    class Meta:
        model = ClinicalService
        fields = ("id", "code", "name", "modality", "description", "linked_charge_id", "is_active")
        read_only_fields = ("id",)


class ClinicalServiceWriteSerializer(serializers.Serializer):
    code = serializers.RegexField(r"^[A-Za-z0-9_-]{1,40}$")
    name = serializers.CharField(max_length=180)
    modality = serializers.CharField(max_length=32)
    description = serializers.CharField(max_length=240, required=False, allow_blank=True)
    linked_charge_id = serializers.UUIDField(required=False, allow_null=True)
    is_active = serializers.BooleanField(required=False, default=True)

