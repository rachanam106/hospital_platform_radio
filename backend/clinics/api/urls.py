from django.urls import include, path
from django.urls import re_path
from rest_framework.routers import DefaultRouter

from .views import (
    AIAnalysisViewSet, ActiveClinicView, AppointmentViewSet, AuditEventViewSet, BillingSummaryView, ChargeItemViewSet, ClinicalServiceViewSet,
    ClinicListView, ClinicProfileView, CsrfTokenView, CurrentUserView, ExaminationViewSet,
    ImagingInstanceViewSet, ImagingSeriesViewSet, ImagingStudyViewSet,
    InvoiceViewSet, LoginView, LogoutView, MembershipViewSet,
    PatientViewSet, PaymentViewSet, RefundViewSet, ReportViewSet, PasswordChangeView, api_not_found,
)
from .dicomweb import (
    DICOMFramesView, DICOMInstanceListView, DICOMInstanceRetrieveView,
    DICOMMetadataView, DICOMSeriesListView, DICOMStudyListView,
)


router = DefaultRouter()
router.register("patients", PatientViewSet, basename="patient")
router.register("appointments", AppointmentViewSet, basename="appointment")
router.register("examinations", ExaminationViewSet, basename="examination")
router.register("imaging-studies", ImagingStudyViewSet, basename="imaging-study")
router.register("imaging-series", ImagingSeriesViewSet, basename="imaging-series")
router.register("imaging-instances", ImagingInstanceViewSet, basename="imaging-instance")
router.register("reports", ReportViewSet, basename="report")
router.register("ai-analyses", AIAnalysisViewSet, basename="ai-analysis")
router.register("invoices", InvoiceViewSet, basename="invoice")
router.register("payments", PaymentViewSet, basename="payment")
router.register("refunds", RefundViewSet, basename="refund")
router.register("charge-items", ChargeItemViewSet, basename="charge-item")
router.register("clinical-services", ClinicalServiceViewSet, basename="clinical-service")
router.register("memberships", MembershipViewSet, basename="membership")
router.register("audit-events", AuditEventViewSet, basename="audit-event")

urlpatterns = [
    path("auth/csrf/", CsrfTokenView.as_view(), name="csrf-token"),
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/logout/", LogoutView.as_view(), name="logout"),
    path("auth/me/", CurrentUserView.as_view(), name="current-user"),
    path("auth/password/change/", PasswordChangeView.as_view(), name="password-change"),
    path("clinics/", ClinicListView.as_view(), name="clinic-list"),
    path("clinics/active/", ActiveClinicView.as_view(), name="active-clinic"),
    path("clinics/profile/", ClinicProfileView.as_view(), name="clinic-profile"),
    path("billing/summary/", BillingSummaryView.as_view(), name="billing-summary"),
    re_path(r"^dicomweb/studies/?$", DICOMStudyListView.as_view(), name="dicomweb-studies"),
    re_path(r"^dicomweb/studies/(?P<study_uid>[0-9.]+)/series/?$", DICOMSeriesListView.as_view(), name="dicomweb-series"),
    re_path(r"^dicomweb/studies/(?P<study_uid>[0-9.]+)/series/(?P<series_uid>[0-9.]+)/instances/?$", DICOMInstanceListView.as_view(), name="dicomweb-instances"),
    re_path(r"^dicomweb/studies/(?P<study_uid>[0-9.]+)/series/(?P<series_uid>[0-9.]+)/instances/(?P<sop_uid>[0-9.]+)/metadata/?$", DICOMMetadataView.as_view(), name="dicomweb-instance-metadata"),
    re_path(r"^dicomweb/studies/(?P<study_uid>[0-9.]+)/series/(?P<series_uid>[0-9.]+)/instances/(?P<sop_uid>[0-9.]+)/frames/(?P<frame_list>[0-9,]+)/?$", DICOMFramesView.as_view(), name="dicomweb-frames"),
    re_path(r"^dicomweb/studies/(?P<study_uid>[0-9.]+)/series/(?P<series_uid>[0-9.]+)/instances/(?P<sop_uid>[0-9.]+)/?$", DICOMInstanceRetrieveView.as_view(), name="dicomweb-instance"),
    path("", include(router.urls)),
    path("<path:unmatched_path>", api_not_found, name="api-not-found"),
]
