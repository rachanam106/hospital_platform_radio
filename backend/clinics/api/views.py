from datetime import date
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.db.models import Q, Value, Sum
from django.db.models.functions import Concat
from django.http import FileResponse, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.middleware.csrf import get_token
from rest_framework import mixins, status, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from clinics.access import Capability, clinic_membership_or_deny, clinic_queryset
from clinics.domain import (
    assign_examination, assign_role, change_provisioned_account_password, create_invoice, create_membership, create_report_draft, create_ai_report_assist, review_ai_report_assist, list_ai_report_assists, deliver_report,
    create_clinical_history_entry, list_clinical_history_entries, review_clinical_history_entry,
    create_charge_item, update_charge_item, refund_payment, record_billing_document_access,
    authorize_report_pdf, edit_report_draft, order_examination, record_authentication_event,
    record_payment, register_patient, release_report, reschedule_appointment,
    reverse_payment, revoke_role, schedule_appointment, select_active_clinic,
    set_membership_active, sign_report, transition_appointment, update_clinic_profile,
    update_membership_registration, transfer_clinic_ownership, save_clinical_service,
    transition_examination, update_patient, void_invoice, provision_staff_account,
    record_patient_history_access,
)
from clinics.imaging import ingest_uploads
from clinics.private_storage import PrivateImageStorage
from clinics.access import imaging_instance_for_user
from clinics.upload_handlers import PrivateImageUploadLimitHandler
from clinics.models import (
    AIAnalysis, AIReportAssist, AccountSecurityState, ClinicalHistoryEntry, Appointment, AuditEvent, Clinic, Examination, ExaminationAssignment, ImagingInstance,
    ImagingSeries, ImagingStudy, Invoice, Membership, Patient, Payment, Report, ChargeItem, ClinicalService, Refund,
)
from .permissions import ActiveClinicContext, CapabilityPermission
from .responses import success
from .serializers import (
    AIAnalysisSerializer, ActiveClinicSerializer, AppointmentActionSerializer,
    AppointmentCreateSerializer, AppointmentRescheduleSerializer,
    AppointmentSerializer, AuditEventSerializer, ClinicSerializer,
    ExaminationActionSerializer, ExaminationCreateSerializer,
    ExaminationAssignmentWriteSerializer, ExaminationSerializer, ExaminationStatusSerializer, ImagingInstanceSerializer, ImagingSeriesSerializer,
    ImagingStudySerializer, InvoiceCreateSerializer, InvoiceSerializer,
    LoginSerializer, MembershipActiveSerializer, MembershipSerializer, PasswordChangeSerializer, StaffAccountCreateSerializer,
    MembershipCreateSerializer,
    PatientCreateSerializer, PatientSerializer, PatientUpdateSerializer, PotentialDuplicateSearchSerializer,
    ClinicalHistoryExaminationSerializer, AssigneeSerializer,
    PaymentCreateSerializer, PaymentReverseSerializer, PaymentSerializer,
    ChargeItemSerializer, ChargeItemCreateSerializer, ChargeItemUpdateSerializer,
    RefundCreateSerializer, RefundSerializer, InvoiceCancelSerializer,
    ClinicProfileSerializer, MembershipRegistrationSerializer, ClinicalServiceSerializer, ClinicalServiceWriteSerializer,
    ReportCreateSerializer, ReportDraftSerializer, ReportSerializer, ReportVersionActionSerializer,
    AIReportAssistInputSerializer, AIReportAssistReviewSerializer, AIReportAssistSerializer,
    ClinicalHistoryEntryCreateSerializer, ClinicalHistoryEntryReviewSerializer, ClinicalHistoryEntrySerializer,
    RoleAssignmentSerializer,
)
from .utils import call_domain
from clinics.reporting import REPORT_TEMPLATES
from clinics.report_pdf import build_signed_report_pdf
from clinics.billing_pdf import build_invoice_pdf, build_payment_receipt_pdf, build_refund_receipt_pdf


class CsrfTokenView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        return success({"csrf_token": get_token(request)})


@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            request,
            username=serializer.validated_data["username"],
            password=serializer.validated_data["password"],
        )
        if user is None:
            call_domain(record_authentication_event, user=None, action="login_failed")
            return Response(
                {"error": {"code": "authentication_failed", "message": "Invalid username or password."}},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        login(request, user)
        call_domain(record_authentication_event, user=user, action="login_succeeded")
        must_change = AccountSecurityState.objects.filter(user=user, must_change_password=True).exists()
        return success({"user_id": user.pk, "must_change_password": must_change, "csrf_token": get_token(request)})


@method_decorator(csrf_protect, name="dispatch")
class LogoutView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        call_domain(record_authentication_event, user=user, action="logout")
        logout(request)
        return success({"logged_out": True})


class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        memberships = Membership.objects.filter(
            user=request.user, is_active=True, clinic__is_active=True,
        ).select_related("clinic").prefetch_related("role_grants")
        active_id = request.session.get("active_clinic_id")
        if active_id:
            try:
                call_domain(clinic_membership_or_deny, request.user, active_id)
            except PermissionDenied:
                request.session.pop("active_clinic_id", None)
                active_id = None
        data = {
            "user_id": request.user.pk,
            "full_name": request.user.get_full_name().strip() or request.user.get_username(),
            "username": request.user.get_username(),
            "must_change_password": AccountSecurityState.objects.filter(user=request.user, must_change_password=True).exists(),
            "memberships": MembershipSerializer(memberships, many=True).data,
            "active_clinic_id": str(active_id) if active_id else None,
        }
        return success(data)


@method_decorator(csrf_protect, name="dispatch")
class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        call_domain(
            change_provisioned_account_password, user=request.user,
            **serializer.validated_data,
        )
        update_session_auth_hash(request, request.user)
        return success({"password_changed": True, "csrf_token": get_token(request)})


class ClinicListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        memberships = Membership.objects.filter(
            user=request.user, is_active=True, clinic__is_active=True,
        ).select_related("clinic").prefetch_related("role_grants")
        return success(MembershipSerializer(memberships, many=True).data)


class ActiveClinicView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        clinic_id = request.session.get("active_clinic_id")
        if not clinic_id:
            return success(None)
        try:
            membership = call_domain(clinic_membership_or_deny, request.user, clinic_id)
        except PermissionDenied:
            request.session.pop("active_clinic_id", None)
            return success(None)
        return success({"clinic": ClinicSerializer(membership.clinic).data, "roles": list(membership.roles())})

    def post(self, request):
        serializer = ActiveClinicSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership = call_domain(
            clinic_membership_or_deny, request.user,
            serializer.validated_data["clinic_id"],
        )
        call_domain(select_active_clinic, membership=membership, actor=request.user)
        request.session["active_clinic_id"] = str(membership.clinic_id)
        return success({"clinic": ClinicSerializer(membership.clinic).data, "roles": list(membership.roles())})


class ClinicProfileView(APIView):
    permission_classes = [IsAuthenticated, ActiveClinicContext, CapabilityPermission]
    required_capability = Capability.CLINIC_ADMIN

    def get(self, request):
        return success(ClinicProfileSerializer(request.clinic).data)

    def patch(self, request):
        serializer = ClinicProfileSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        clinic = call_domain(
            update_clinic_profile, clinic_id=request.clinic.pk,
            changes=serializer.validated_data, actor=request.user,
            membership=request.membership,
        )
        return success(ClinicProfileSerializer(clinic).data)

class ClinicScopedViewSet(viewsets.GenericViewSet):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated, ActiveClinicContext, CapabilityPermission]
    required_capability = None
    read_capability = None

    def get_queryset(self):
        return clinic_queryset(self.model.objects.all(), self.request.membership).order_by("created_at", "pk")

    def get_required_capability(self, request):
        action_name = getattr(self, "action", "")
        return self.action_capability(request, action_name)

    def action_capability(self, request, action_name):
        return self.read_capability or self.required_capability

    def domain(self, func, **kwargs):
        return call_domain(func, **kwargs)


class PatientViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ClinicScopedViewSet):
    model = Patient
    read_capability = Capability.PATIENT_READ
    serializer_class = PatientSerializer

    def action_capability(self, request, action_name):
        if action_name in {"clinical_history", "clinical_history_entries", "clinical_history_entry_review"}:
            return Capability.PATIENT_CLINICAL_HISTORY
        if action_name in {"create", "update", "partial_update"}:
            return Capability.PATIENT_ADMIN
        return self.read_capability

    def get_queryset(self):
        queryset = super().get_queryset().filter(archived_at__isnull=True)
        term = self.request.query_params.get("search", "").strip()
        if term:
            full_name = Concat("given_name", Value(" "), "family_name")
            queryset = queryset.annotate(_full_name=full_name).filter(
                Q(medical_record_number__icontains=term)
                | Q(given_name__icontains=term)
                | Q(family_name__icontains=term)
                | Q(phone__icontains=term)
                | Q(_full_name__icontains=term)
            )
        return queryset

    @action(detail=False, methods=["get"], url_path="potential-duplicates")
    def potential_duplicates(self, request):
        serializer = PotentialDuplicateSearchSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        matches = Q()
        if values.get("medical_record_number"):
            matches |= Q(medical_record_number__iexact=values["medical_record_number"])
        identity = Q(given_name__iexact=values.get("given_name", ""), family_name__iexact=values.get("family_name", ""))
        if values.get("date_of_birth"):
            matches |= identity & Q(date_of_birth=values["date_of_birth"])
        if values.get("phone") and values.get("given_name") and values.get("family_name"):
            matches |= identity & Q(phone__iexact=values["phone"])
        queryset = self.get_queryset().filter(matches).order_by("family_name", "given_name")[:10]
        return success(PatientSerializer(queryset, many=True).data)

    @action(detail=True, methods=["get"], url_path="clinical-history")
    def clinical_history(self, request, pk=None):
        patient = self.get_object()
        self.domain(record_patient_history_access, patient_id=patient.pk, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership)
        examinations = patient.examinations.filter(clinic_id=request.clinic.pk).exclude(
            status__in=[Examination.Status.ORDERED, Examination.Status.CHECKED_IN, Examination.Status.IN_PROGRESS, Examination.Status.ACQUIRED, Examination.Status.REPORTING, Examination.Status.CANCELLED],
        ).prefetch_related("imaging_studies", "reports").order_by("-created_at")
        return success({"patient_id": str(patient.pk), "examinations": ClinicalHistoryExaminationSerializer(examinations, many=True).data})

    @action(detail=True, methods=["get", "post"], url_path="clinical-history/entries")
    def clinical_history_entries(self, request, pk=None):
        patient = self.get_object()
        if request.method == "GET":
            entries = self.domain(list_clinical_history_entries, patient_id=patient.pk, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership)
            return success(ClinicalHistoryEntrySerializer(entries, many=True).data)
        serializer = ClinicalHistoryEntryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry = self.domain(create_clinical_history_entry, patient_id=patient.pk, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, **serializer.validated_data)
        return success(ClinicalHistoryEntrySerializer(entry).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path=r"clinical-history/entries/(?P<entry_id>[^/.]+)/review")
    def clinical_history_entry_review(self, request, pk=None, entry_id=None):
        patient = self.get_object()
        serializer = ClinicalHistoryEntryReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry = self.domain(review_clinical_history_entry, patient_id=patient.pk, entry_id=entry_id, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, **serializer.validated_data)
        return success(ClinicalHistoryEntrySerializer(entry).data)

    def create(self, request, *args, **kwargs):
        serializer = PatientCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        patient = self.domain(
            register_patient, clinic_id=request.clinic.pk, actor=request.user,
            membership=request.membership, **serializer.validated_data,
        )
        return success(PatientSerializer(patient).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        return self.partial_update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        patient = self.get_object()
        serializer = PatientUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        patient = self.domain(
            update_patient, patient_id=patient.pk, clinic_id=request.clinic.pk,
            actor=request.user, membership=request.membership,
            changes=serializer.validated_data,
        )
        return success(PatientSerializer(patient).data)


class AppointmentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ClinicScopedViewSet):
    model = Appointment
    read_capability = Capability.APPOINTMENT_READ
    serializer_class = AppointmentSerializer

    def action_capability(self, request, action_name):
        if action_name == "create" or action_name == "reschedule":
            return Capability.APPOINTMENT_ADMIN
        if action_name == "transition":
            target = request.data.get("target")
            return Capability.CHECK_IN if target == Appointment.Status.CHECKED_IN else Capability.APPOINTMENT_ADMIN
        return self.read_capability

    def create(self, request, *args, **kwargs):
        serializer = AppointmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        appointment = self.domain(
            schedule_appointment, clinic_id=request.clinic.pk, actor=request.user,
            membership=request.membership, **serializer.validated_data,
        )
        return success(AppointmentSerializer(appointment).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        serializer = AppointmentActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        appointment = self.domain(
            transition_appointment, appointment_id=self.get_object().pk,
            clinic_id=request.clinic.pk, target=serializer.validated_data["target"],
            actor=request.user, membership=request.membership,
        )
        return success(AppointmentSerializer(appointment).data)

    @action(detail=True, methods=["post"])
    def reschedule(self, request, pk=None):
        serializer = AppointmentRescheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        appointment = self.domain(
            reschedule_appointment, appointment_id=self.get_object().pk,
            clinic_id=request.clinic.pk, actor=request.user,
            membership=request.membership, **serializer.validated_data,
        )
        return success(AppointmentSerializer(appointment).data)


class ExaminationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ClinicScopedViewSet):
    model = Examination
    read_capability = Capability.EXAMINATION_READ
    serializer_class = ExaminationSerializer

    def get_queryset(self):
        queryset = super().get_queryset().select_related("assignment")
        if self.request.membership.has_role("technician") and not self.request.membership.has_role("radiologist"):
            queryset = queryset.filter(assignment__technician_id=self.request.user.pk)
        if self.request.query_params.get("patient_id"):
            queryset = queryset.filter(patient_id=self.request.query_params["patient_id"])
        return queryset

    def get_serializer_class(self):
        if self.request.membership.has_role("receptionist") and not self.request.membership.has_role("technician") and not self.request.membership.has_role("radiologist"):
            return ExaminationStatusSerializer
        return self.serializer_class

    def action_capability(self, request, action_name):
        if action_name == "create":
            return Capability.INTERPRET
        if action_name == "transition":
            target = request.data.get("target")
            return {
                Examination.Status.CHECKED_IN: Capability.CHECK_IN,
                Examination.Status.IN_PROGRESS: Capability.ACQUIRE,
                Examination.Status.ACQUIRED: Capability.ACQUIRE,
                Examination.Status.REPORTING: Capability.INTERPRET,
                Examination.Status.CANCELLED: Capability.APPOINTMENT_ADMIN,
            }.get(target, Capability.INTERPRET)
        if action_name == "deliver":
            return Capability.DELIVER_REPORT
        if action_name in {"assignment", "assignees"}:
            return Capability.ASSIGN_EXAMINATION
        if action_name == "imaging_upload":
            return Capability.UPLOAD_IMAGE
        if action_name in {"list", "retrieve"} and self.request.membership.has_role("receptionist"):
            return Capability.EXAMINATION_STATUS_READ
        return self.read_capability

    def create(self, request, *args, **kwargs):
        serializer = ExaminationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        exam = self.domain(
            order_examination, clinic_id=request.clinic.pk, actor=request.user,
            membership=request.membership, **serializer.validated_data,
        )
        return success(ExaminationSerializer(exam).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        serializer = ExaminationActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        exam = self.domain(
            transition_examination, examination_id=self.get_object().pk,
            clinic_id=request.clinic.pk, target=serializer.validated_data["target"],
            actor=request.user, membership=request.membership,
        )
        return success(ExaminationSerializer(exam).data)

    @action(detail=True, methods=["post"])
    def deliver(self, request, pk=None):
        exam = self.domain(
            deliver_report, examination_id=self.get_object().pk,
            clinic_id=request.clinic.pk, actor=request.user, membership=request.membership,
        )
        return success({"id": str(exam.pk), "status": exam.status})

    @action(detail=False, methods=["get"])
    def assignees(self, request):
        from clinics.models import MembershipRole
        rows = MembershipRole.objects.filter(
            clinic_id=request.clinic.pk, is_active=True,
            membership__is_active=True, membership__clinic__is_active=True,
            role__in=[MembershipRole.Role.TECHNICIAN, MembershipRole.Role.RADIOLOGIST],
        ).values("membership__user_id", "role").order_by("role", "membership__user_id")
        data = [{"user_id": row["membership__user_id"], "role": row["role"]} for row in rows]
        return success(AssigneeSerializer(data, many=True).data)

    @action(detail=True, methods=["post"])
    def assignment(self, request, pk=None):
        serializer = ExaminationAssignmentWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assignment = self.domain(
            assign_examination, examination_id=self.get_object().pk,
            clinic_id=request.clinic.pk, actor=request.user, membership=request.membership,
            **serializer.validated_data,
        )
        return success({"id": str(assignment.pk), "examination_id": str(assignment.examination_id), "technician_id": assignment.technician_id, "radiologist_id": assignment.radiologist_id})

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser], url_path="imaging")
    def imaging_upload(self, request, pk=None):
        examination = self.get_object()
        upload_limiter = PrivateImageUploadLimitHandler(request._request)
        request._request.upload_handlers.insert(0, upload_limiter)
        files = request.FILES.getlist("files")
        if upload_limiter.limit_exceeded:
            raise ValidationError("The image upload exceeded a configured per-file or request size limit.")
        results = self.domain(
            ingest_uploads, clinic_id=request.clinic.pk, actor=request.user,
            membership=request.membership, examination_id=examination.pk, files=files,
        )
        data = [{
            "duplicate": item["duplicate"],
            "instance": ImagingInstanceSerializer(item["instance"], context={"request": request}).data,
        } for item in results]
        return success(data, status=status.HTTP_200_OK)


class ImagingStudyViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ClinicScopedViewSet):
    model = ImagingStudy
    read_capability = Capability.VIEW_IMAGE
    serializer_class = ImagingStudySerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        examination_id = self.request.query_params.get("examination")
        if examination_id:
            queryset = queryset.filter(examination_id=examination_id)
        if self.request.membership.has_role("technician") and not self.request.membership.has_role("radiologist"):
            queryset = queryset.filter(examination__assignment__technician_id=self.request.user.pk)
        return queryset


class ImagingSeriesViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ClinicScopedViewSet):
    model = ImagingSeries
    read_capability = Capability.VIEW_IMAGE
    serializer_class = ImagingSeriesSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        study_id = self.request.query_params.get("study")
        if study_id:
            queryset = queryset.filter(study_id=study_id)
        if self.request.membership.has_role("technician") and not self.request.membership.has_role("radiologist"):
            queryset = queryset.filter(study__examination__assignment__technician_id=self.request.user.pk)
        return queryset


class ImagingInstanceViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ClinicScopedViewSet):
    model = ImagingInstance
    read_capability = Capability.VIEW_IMAGE
    serializer_class = ImagingInstanceSerializer

    def get_queryset(self):
        queryset = super().get_queryset().select_related("series__study")
        series_id = self.request.query_params.get("series")
        if series_id:
            queryset = queryset.filter(series_id=series_id)
        if self.request.membership.has_role("technician") and not self.request.membership.has_role("radiologist"):
            queryset = queryset.filter(series__study__examination__assignment__technician_id=self.request.user.pk)
        return queryset

    @action(detail=True, methods=["get"])
    def content(self, request, pk=None):
        instance = imaging_instance_for_user(user=request.user, clinic_id=request.clinic.pk, instance_id=self.get_object().pk)
        storage = PrivateImageStorage()
        if not storage.verify(instance.storage_key, checksum_sha256=instance.checksum_sha256, byte_size=instance.byte_size):
            raise NotFound()
        file_handle = storage.open(instance.storage_key)
        is_raster = instance.media_type in {"image/jpeg", "image/png"}
        response = FileResponse(
            file_handle, as_attachment=not is_raster,
            filename=instance.original_name,
            content_type=instance.media_type,
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class ReportViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ClinicScopedViewSet):
    model = Report
    read_capability = Capability.REPORT_READ
    serializer_class = ReportSerializer

    def action_capability(self, request, action_name):
        return {
            "create": Capability.INTERPRET,
            "partial_update": Capability.INTERPRET,
            "update": Capability.INTERPRET,
            "sign": Capability.SIGN_REPORT,
            "release": Capability.RELEASE_REPORT,
            "ai_assist": Capability.INTERPRET,
            "review_ai_assist": Capability.INTERPRET,
            "ai_assists": Capability.INTERPRET,
            "pdf": Capability.REPORT_READ,
            "templates": Capability.REPORT_READ,
        }.get(action_name, self.read_capability)

    def create(self, request, *args, **kwargs):
        serializer = ReportCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = self.domain(
            create_report_draft, clinic_id=request.clinic.pk,
            actor=request.user, membership=request.membership,
            template=serializer.validated_data.get("template"),
            **{key: value for key, value in serializer.validated_data.items() if key != "template"},
        )
        return success(ReportSerializer(report).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        return self.partial_update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        report = self.get_object()
        serializer = ReportDraftSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if "expected_updated_at" not in serializer.validated_data:
            raise ValidationError({"expected_updated_at": "This field is required to prevent overwriting concurrent edits."})
        report = self.domain(
            edit_report_draft, report_id=report.pk, clinic_id=request.clinic.pk,
            actor=request.user, membership=request.membership,
            expected_updated_at=serializer.validated_data["expected_updated_at"],
            technique=serializer.validated_data.get("technique", report.technique),
            sequences=serializer.validated_data.get("sequences", report.sequences),
            comparison=serializer.validated_data.get("comparison", report.comparison),
            findings=serializer.validated_data.get("findings", report.findings),
            impression=serializer.validated_data.get("impression", report.impression),
            additional_comments=serializer.validated_data.get("additional_comments", report.additional_comments),
        )
        return success(ReportSerializer(report).data)

    @action(detail=True, methods=["post"])
    def sign(self, request, pk=None):
        serializer = ReportVersionActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = self.domain(sign_report, report_id=self.get_object().pk, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, expected_updated_at=serializer.validated_data["expected_updated_at"])
        return success(ReportSerializer(report).data)

    @action(detail=True, methods=["post"])
    def release(self, request, pk=None):
        report = self.domain(release_report, report_id=self.get_object().pk, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership)
        return success(ReportSerializer(report).data)

    @action(detail=True, methods=["post"], url_path="ai-assist")
    def ai_assist(self, request, pk=None):
        serializer = AIReportAssistInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assist = self.domain(
            create_ai_report_assist, report_id=self.get_object().pk,
            clinic_id=request.clinic.pk, actor=request.user,
            membership=request.membership, evidence_input=serializer.validated_data,
        )
        return success(AIReportAssistSerializer(assist).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="ai-assists")
    def ai_assists(self, request, pk=None):
        report = self.get_object()
        assists = self.domain(
            list_ai_report_assists, report_id=report.pk,
            clinic_id=request.clinic.pk, actor=request.user,
            membership=request.membership,
        )
        return success(AIReportAssistSerializer(assists, many=True).data)

    @action(detail=True, methods=["post"], url_path="review-ai-assist")
    def review_ai_assist(self, request, pk=None):
        report = self.get_object()
        serializer = AIReportAssistReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assist = self.domain(
            review_ai_report_assist,
            report_id=report.pk, assist_id=serializer.validated_data["assist_id"],
            clinic_id=request.clinic.pk, actor=request.user,
            membership=request.membership,
            decision=serializer.validated_data["decision"],
        )
        return success(AIReportAssistSerializer(assist).data)

    @action(detail=False, methods=["get"])
    def templates(self, request):
        return success(list(REPORT_TEMPLATES.values()))

    @action(detail=True, methods=["get"])
    def pdf(self, request, pk=None):
        report = self.domain(
            authorize_report_pdf, report_id=self.get_object().pk,
            clinic_id=request.clinic.pk, actor=request.user,
            membership=request.membership,
        )
        response = HttpResponse(build_signed_report_pdf(report), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="report-{report.pk}-v{report.version}.pdf"'
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class AIAnalysisViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ClinicScopedViewSet):
    model = AIAnalysis
    read_capability = Capability.REPORT_READ
    serializer_class = AIAnalysisSerializer


class InvoiceViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ClinicScopedViewSet):
    model = Invoice
    read_capability = Capability.BILLING_READ
    serializer_class = InvoiceSerializer

    def action_capability(self, request, action_name):
        if action_name == "create":
            return Capability.BILLING
        if action_name == "cancel":
            return Capability.BILLING_CANCEL
        if action_name == "payments":
            return Capability.BILLING if request.method == "POST" else Capability.BILLING_READ
        return self.read_capability

    def get_queryset(self):
        queryset = super().get_queryset().select_related("patient", "examination").prefetch_related("line_items")
        if self.request.query_params.get("patient_id"):
            queryset = queryset.filter(patient_id=self.request.query_params["patient_id"])
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = InvoiceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = self.domain(create_invoice, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, **serializer.validated_data)
        return success(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get", "post"])
    def payments(self, request, pk=None):
        invoice = self.get_object()
        if request.method == "GET":
            payments = invoice.payments.filter(clinic_id=request.clinic.pk).order_by("created_at")
            return success(PaymentSerializer(payments, many=True).data)
        serializer = PaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = self.domain(record_payment, invoice_id=invoice.pk, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, **serializer.validated_data)
        return success(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        serializer = InvoiceCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = self.domain(void_invoice, invoice_id=self.get_object().pk, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, **serializer.validated_data)
        return success(InvoiceSerializer(invoice).data)

    @action(detail=True, methods=["get"])
    def pdf(self, request, pk=None):
        invoice = self.get_object()
        if invoice.status == Invoice.Status.DRAFT:
            raise ValidationError("Only issued invoices can be downloaded.")
        self.domain(record_billing_document_access, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, invoice=invoice)
        response = HttpResponse(build_invoice_pdf(invoice), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="invoice-{invoice.invoice_number}.pdf"'
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class PaymentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ClinicScopedViewSet):
    model = Payment
    read_capability = Capability.BILLING_READ
    serializer_class = PaymentSerializer

    def action_capability(self, request, action_name):
        if action_name in {"refunds", "reverse"} and request.method == "POST":
            return Capability.BILLING_REFUND
        return self.read_capability

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        serializer = PaymentReverseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = self.domain(reverse_payment, payment_id=self.get_object().pk, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, **serializer.validated_data)
        return success(PaymentSerializer(payment).data)

    @action(detail=True, methods=["get", "post"])
    def refunds(self, request, pk=None):
        payment = self.get_object()
        if request.method == "GET":
            return success(RefundSerializer(payment.refunds.filter(clinic_id=request.clinic.pk), many=True).data)
        serializer = RefundCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        refund = self.domain(refund_payment, payment_id=payment.pk, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, **serializer.validated_data)
        return success(RefundSerializer(refund).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def receipt(self, request, pk=None):
        payment = self.get_object()
        self.domain(record_billing_document_access, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, payment=payment)
        response = HttpResponse(build_payment_receipt_pdf(payment), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="receipt-{payment.pk}.pdf"'
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class RefundViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ClinicScopedViewSet):
    model = Refund
    read_capability = Capability.BILLING_READ
    serializer_class = RefundSerializer

    def get_queryset(self):
        return super().get_queryset().select_related("payment__invoice", "recorded_by")

    @action(detail=True, methods=["get"])
    def receipt(self, request, pk=None):
        refund = self.get_object()
        self.domain(record_billing_document_access, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, payment=refund.payment)
        response = HttpResponse(build_refund_receipt_pdf(refund), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="refund-{refund.pk}.pdf"'
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class ChargeItemViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ClinicScopedViewSet):
    model = ChargeItem
    read_capability = Capability.BILLING_READ
    serializer_class = ChargeItemSerializer

    def action_capability(self, request, action_name):
        return Capability.BILLING_CONFIGURE if action_name in {"create", "partial_update", "update"} else self.read_capability

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.query_params.get("include_inactive") != "true":
            queryset = queryset.filter(is_active=True)
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = ChargeItemCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = self.domain(create_charge_item, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, **serializer.validated_data)
        return success(ChargeItemSerializer(item).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        item = self.get_object()
        serializer = ChargeItemUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        item = self.domain(update_charge_item, charge_item_id=item.pk, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, changes=serializer.validated_data)
        return success(ChargeItemSerializer(item).data)

    def update(self, request, *args, **kwargs):
        return self.partial_update(request, *args, **kwargs)


class ClinicalServiceViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ClinicScopedViewSet):
    model = ClinicalService
    read_capability = Capability.SERVICE_CATALOGUE_READ
    serializer_class = ClinicalServiceSerializer

    def get_queryset(self):
        queryset = super().get_queryset().select_related("linked_charge")
        if self.request.query_params.get("include_inactive") != "true":
            queryset = queryset.filter(is_active=True)
        return queryset

    def action_capability(self, request, action_name):
        return Capability.BILLING_CONFIGURE if action_name in {"create", "partial_update", "update"} else self.read_capability

    def create(self, request, *args, **kwargs):
        serializer = ClinicalServiceWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        service = self.domain(save_clinical_service, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, changes=serializer.validated_data)
        return success(ClinicalServiceSerializer(service).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        service = self.get_object()
        serializer = ClinicalServiceWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        service = self.domain(save_clinical_service, clinic_id=request.clinic.pk, actor=request.user, membership=request.membership, service_id=service.pk, changes=serializer.validated_data)
        return success(ClinicalServiceSerializer(service).data)

    def update(self, request, *args, **kwargs):
        return self.partial_update(request, *args, **kwargs)


class BillingSummaryView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated, ActiveClinicContext, CapabilityPermission]
    required_capability = Capability.BILLING_READ

    def get(self, request):
        from datetime import date
        try:
            target_date = date.fromisoformat(request.query_params.get("date", timezone.localdate().isoformat()))
        except ValueError as exc:
            raise ValidationError({"date": "Use YYYY-MM-DD."}) from exc
        invoices = clinic_queryset(Invoice.objects.all(), request.membership)
        payments = clinic_queryset(Payment.objects.filter(reversed_at__isnull=True), request.membership).filter(created_at__date=target_date)
        refunds = clinic_queryset(Refund.objects.all(), request.membership).filter(created_at__date=target_date)
        received_by_currency = {row["currency"]: row["amount_minor"] for row in payments.values("currency").annotate(amount_minor=Sum("amount_minor"))}
        refunded_by_currency = {row["payment__currency"]: row["amount_minor"] for row in refunds.values("payment__currency").annotate(amount_minor=Sum("amount_minor"))}
        collections_by_currency = {currency: received_by_currency.get(currency, 0) - refunded_by_currency.get(currency, 0) for currency in received_by_currency.keys() | refunded_by_currency.keys()}
        method_totals = {(row["currency"], row["method"]): row["amount_minor"] for row in payments.values("currency", "method").annotate(amount_minor=Sum("amount_minor"))}
        for row in refunds.values("payment__currency", "payment__method").annotate(amount_minor=Sum("amount_minor")):
            key = (row["payment__currency"], row["payment__method"])
            method_totals[key] = method_totals.get(key, 0) - row["amount_minor"]
        open_invoices = invoices.exclude(status__in=[Invoice.Status.CANCELLED, Invoice.Status.REFUNDED])
        outstanding_by_currency = {}
        for invoice in open_invoices.select_related("clinic", "patient"):
            outstanding_by_currency[invoice.currency] = outstanding_by_currency.get(invoice.currency, 0) + invoice.balance_minor
        def single_currency_value(values):
            return next(iter(values.values())) if len(values) == 1 else (0 if not values else None)
        result = {
            "date": target_date.isoformat(),
            "collections_by_currency": [{"currency": currency, "amount_minor": amount} for currency, amount in sorted(collections_by_currency.items())],
            "collections_minor": single_currency_value(collections_by_currency),
            "payment_methods": [{"currency": currency, "method": method, "amount_minor": amount} for (currency, method), amount in sorted(method_totals.items())],
            "outstanding_by_currency": [{"currency": currency, "amount_minor": amount} for currency, amount in sorted(outstanding_by_currency.items())],
            "outstanding_minor": single_currency_value(outstanding_by_currency),
            "outstanding_invoice_count": open_invoices.filter(status__in=[Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID]).count(),
            "partially_paid_count": open_invoices.filter(status=Invoice.Status.PARTIALLY_PAID).count(),
            "recent_payments": PaymentSerializer(clinic_queryset(Payment.objects.all(), request.membership).select_related("invoice").order_by("-created_at")[:20], many=True).data,
        }
        patient_id = request.query_params.get("patient_id")
        if patient_id:
            if not clinic_queryset(Patient.objects.all(), request.membership).filter(pk=patient_id).exists():
                raise NotFound("Patient not found in the active clinic.")
            patient_balances = {}
            for invoice in invoices.filter(patient_id=patient_id).exclude(status__in=[Invoice.Status.CANCELLED, Invoice.Status.REFUNDED]):
                patient_balances[invoice.currency] = patient_balances.get(invoice.currency, 0) + invoice.balance_minor
            result["patient_outstanding_by_currency"] = [{"currency": currency, "amount_minor": amount} for currency, amount in sorted(patient_balances.items())]
            result["patient_outstanding_minor"] = single_currency_value(patient_balances)
        return success(result)


class MembershipViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ClinicScopedViewSet):
    model = Membership
    read_capability = Capability.MEMBERSHIP_READ
    serializer_class = MembershipSerializer

    def get_queryset(self):
        return super().get_queryset().select_related("clinic", "user").prefetch_related("role_grants")

    def action_capability(self, request, action_name):
        if action_name == "transfer_ownership":
            return Capability.OWNER_MANAGE
        return Capability.CLINIC_ADMIN if action_name in {"create", "provision", "roles", "active", "profile"} else self.read_capability

    def create(self, request, *args, **kwargs):
        serializer = MembershipCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership = self.domain(
            create_membership, clinic_id=request.clinic.pk,
            actor=request.user, admin_membership=request.membership,
            **serializer.validated_data,
        )
        return success(MembershipSerializer(membership).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="provision")
    def provision(self, request):
        serializer = StaffAccountCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership, temporary_password = self.domain(
            provision_staff_account, clinic_id=request.clinic.pk,
            actor=request.user, admin_membership=request.membership,
            **serializer.validated_data,
        )
        return success({
            "membership": MembershipSerializer(membership).data,
            "temporary_password": temporary_password,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post", "delete"])
    def roles(self, request, pk=None):
        target = self.get_object()
        serializer = RoleAssignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if request.method == "POST":
            grant = self.domain(assign_role, membership_id=target.pk, clinic_id=request.clinic.pk, role=serializer.validated_data["role"], actor=request.user, admin_membership=request.membership)
            return success({"membership_id": str(target.pk), "role": grant.role, "is_active": grant.is_active}, status=status.HTTP_201_CREATED)
        grant = self.domain(revoke_role, membership_id=target.pk, clinic_id=request.clinic.pk, role=serializer.validated_data["role"], actor=request.user, admin_membership=request.membership)
        return success({"membership_id": str(target.pk), "role": grant.role, "is_active": grant.is_active})

    @action(detail=True, methods=["patch"])
    def active(self, request, pk=None):
        target = self.get_object()
        serializer = MembershipActiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership = self.domain(set_membership_active, membership_id=target.pk, clinic_id=request.clinic.pk, active=serializer.validated_data["is_active"], actor=request.user, admin_membership=request.membership)
        return success(MembershipSerializer(membership).data)

    @action(detail=True, methods=["patch"])
    def profile(self, request, pk=None):
        target = self.get_object()
        serializer = MembershipRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership = self.domain(
            update_membership_registration, membership_id=target.pk,
            clinic_id=request.clinic.pk, actor=request.user,
            admin_membership=request.membership,
            **serializer.validated_data,
        )
        return success(MembershipSerializer(membership).data)

    @action(detail=True, methods=["post"], url_path="transfer-ownership")
    def transfer_ownership(self, request, pk=None):
        target = self.get_object()
        membership = self.domain(transfer_clinic_ownership, clinic_id=request.clinic.pk, target_membership_id=target.pk, actor=request.user, admin_membership=request.membership)
        return success(MembershipSerializer(membership).data)


class AuditEventViewSet(mixins.ListModelMixin, ClinicScopedViewSet):
    model = AuditEvent
    read_capability = Capability.AUDIT_READ
    serializer_class = AuditEventSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        action_filter = self.request.query_params.get("action")
        actor_id = self.request.query_params.get("actor_id")
        date_from = self.request.query_params.get("date_from")
        date_to = self.request.query_params.get("date_to")
        if action_filter:
            queryset = queryset.filter(action__icontains=action_filter[:120])
        if actor_id:
            if not actor_id.isdecimal():
                raise ValidationError({"actor_id": "Use a numeric actor ID."})
            queryset = queryset.filter(actor_id=actor_id)
        if date_from:
            try:
                date.fromisoformat(date_from)
            except ValueError as exc:
                raise ValidationError({"date_from": "Use YYYY-MM-DD."}) from exc
            queryset = queryset.filter(occurred_at__date__gte=date_from)
        if date_to:
            try:
                date.fromisoformat(date_to)
            except ValueError as exc:
                raise ValidationError({"date_to": "Use YYYY-MM-DD."}) from exc
            queryset = queryset.filter(occurred_at__date__lte=date_to)
        return queryset.order_by("-occurred_at")


def api_not_found(request, unmatched_path=""):
    return JsonResponse(
        {"error": {"code": "not_found", "message": "API route not found."}},
        status=404,
    )

