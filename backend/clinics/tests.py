from datetime import datetime, timezone as dt_timezone
from io import BytesIO
import shutil
import tempfile
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from .access import Capability, clinic_membership_or_deny, imaging_instance_for_user, tenant_object_for_worker
from .domain import (
    assign_examination, create_invoice, create_report_draft, deliver_report, edit_report_draft, order_examination,
    record_ai_analysis, record_payment, register_imaging_instance,
    register_patient, release_report, reverse_payment, review_ai_analysis,
    schedule_appointment, sign_report, transition_appointment,
    transition_examination,
)
from .imaging import ingest_uploads
from .ai_assistance import build_assist, verify_claims
from .symptom_extraction import extract_symptoms
from .models import (
    AIAnalysis, Appointment, AuditEvent, Clinic, Examination, ImagingInstance,
    ImagingSeries, ImagingStudy, Invoice, Membership, MembershipRole, Patient,
    Payment, Report,
)


class ClinicDomainTests(TestCase):
    def setUp(self):
        self.private_root = tempfile.mkdtemp(prefix="clinic-private-images-")
        self.storage_settings = override_settings(PRIVATE_IMAGE_STORAGE_ROOT=self.private_root)
        self.storage_settings.enable()
        self.addCleanup(self.storage_settings.disable)
        self.addCleanup(shutil.rmtree, self.private_root, ignore_errors=True)
        user_model = get_user_model()
        self.receptionist = user_model.objects.create(username="frontdesk")
        self.technician = user_model.objects.create(username="tech")
        self.radiologist = user_model.objects.create(username="rad")
        self.owner = user_model.objects.create(username="owner")
        self.other = user_model.objects.create(username="other")
        self.clinic = Clinic.objects.create(name="North Clinic")
        self.other_clinic = Clinic.objects.create(name="South Clinic")
        self.frontdesk_membership = self.make_membership(self.clinic, self.receptionist, [MembershipRole.Role.RECEPTIONIST])
        self.owner_membership = self.make_membership(self.clinic, self.owner, [MembershipRole.Role.OWNER])
        self.technician_membership = self.make_membership(self.clinic, self.technician, [MembershipRole.Role.TECHNICIAN])
        self.radiologist_membership = self.make_membership(self.clinic, self.radiologist, [MembershipRole.Role.RADIOLOGIST])
        self.other_membership = self.make_membership(self.other_clinic, self.other, [MembershipRole.Role.RECEPTIONIST])
        self.other_technician_membership = self.make_membership(self.other_clinic, self.technician, [MembershipRole.Role.TECHNICIAN])
        self.other_radiologist_membership = self.make_membership(self.other_clinic, self.radiologist, [MembershipRole.Role.RADIOLOGIST])

    @staticmethod
    def make_membership(clinic, user, roles):
        membership = Membership(clinic=clinic, user=user)
        membership.save()
        for role in roles:
            grant = MembershipRole(clinic=clinic, membership=membership, role=role)
            grant._domain_service_write = True
            grant.save()
        return membership

    def patient(self, *, clinic=None, actor=None, membership=None, mrn="MRN-001"):
        return register_patient(
            clinic_id=(clinic or self.clinic).pk,
            actor=actor or self.receptionist,
            membership=membership or self.frontdesk_membership,
            medical_record_number=mrn, given_name="Asha", family_name="Rao",
        )

    def examination(self, *, patient=None):
        patient = patient or self.patient()
        return order_examination(
            clinic_id=self.clinic.pk, actor=self.radiologist,
            membership=self.radiologist_membership, patient_id=patient.pk,
            modality="CT", procedure_name="CT chest",
        )

    def to_acquired(self, exam):
        assign_examination(
            examination_id=exam.pk, clinic_id=self.clinic.pk, actor=self.radiologist,
            membership=self.radiologist_membership, technician_id=self.technician.pk,
            radiologist_id=self.radiologist.pk,
        )
        transition_examination(examination_id=exam.pk, clinic_id=self.clinic.pk, target=Examination.Status.CHECKED_IN, actor=self.receptionist, membership=self.frontdesk_membership)
        transition_examination(examination_id=exam.pk, clinic_id=self.clinic.pk, target=Examination.Status.IN_PROGRESS, actor=self.technician, membership=self.technician_membership)
        image = BytesIO()
        Image.new("L", (2, 2), color=120).save(image, format="PNG")
        ingest_uploads(
            clinic_id=self.clinic.pk, actor=self.technician,
            membership=self.technician_membership, examination_id=exam.pk,
            files=[SimpleUploadedFile("synthetic.png", image.getvalue(), content_type="image/png")],
        )
        return transition_examination(examination_id=exam.pk, clinic_id=self.clinic.pk, target=Examination.Status.ACQUIRED, actor=self.technician, membership=self.technician_membership)

    def test_multiple_roles_are_normalized_and_capabilities_are_granular(self):
        combined = self.make_membership(self.clinic, self.other, [MembershipRole.Role.TECHNICIAN, MembershipRole.Role.RADIOLOGIST])
        self.assertTrue(combined.has_role(MembershipRole.Role.TECHNICIAN))
        self.assertTrue(combined.has_role(MembershipRole.Role.RADIOLOGIST))
        with self.assertRaises(PermissionDenied):
            clinic_membership_or_deny(self.receptionist, self.clinic.pk, Capability.SIGN_REPORT)
        clinic_membership_or_deny(self.technician, self.clinic.pk, Capability.UPLOAD_IMAGE)
        with self.assertRaises(PermissionDenied):
            clinic_membership_or_deny(self.technician, self.clinic.pk, Capability.SIGN_REPORT)

    def test_related_patient_and_appointment_must_share_tenant(self):
        foreign_patient = self.patient(clinic=self.other_clinic, actor=self.other, membership=self.other_membership, mrn="FOREIGN")
        appointment = Appointment(clinic=self.clinic, patient=foreign_patient, starts_at=timezone.now())
        appointment._domain_service_write = True
        with self.assertRaises(ValidationError):
            appointment.save()
        exam = Examination(
            clinic=self.clinic, patient=foreign_patient, modality="CT", procedure_name="CT",
            ordering_user=self.radiologist,
        )
        exam._domain_service_write = True
        with self.assertRaises(ValidationError):
            exam.save()

    def test_queryset_and_worker_lookups_are_tenant_scoped(self):
        patient = self.patient()
        foreign_patient = self.patient(clinic=self.other_clinic, actor=self.other, membership=self.other_membership, mrn="OTHER")
        self.assertEqual(tenant_object_for_worker(Patient, clinic_id=self.clinic.pk, object_id=patient.pk), patient)
        with self.assertRaises(Patient.DoesNotExist):
            tenant_object_for_worker(Patient, clinic_id=self.clinic.pk, object_id=foreign_patient.pk)

    def test_radiologist_can_acquire_and_interpret_without_technician(self):
        exam = self.examination()
        transition_examination(examination_id=exam.pk, clinic_id=self.clinic.pk, target=Examination.Status.CHECKED_IN, actor=self.receptionist, membership=self.frontdesk_membership)
        transition_examination(examination_id=exam.pk, clinic_id=self.clinic.pk, target=Examination.Status.IN_PROGRESS, actor=self.radiologist, membership=self.radiologist_membership)
        image = BytesIO()
        Image.new("L", (2, 2), color=80).save(image, format="PNG")
        ingest_uploads(
            clinic_id=self.clinic.pk, actor=self.radiologist,
            membership=self.radiologist_membership, examination_id=exam.pk,
            files=[SimpleUploadedFile("synthetic.png", image.getvalue(), content_type="image/png")],
        )
        exam = transition_examination(examination_id=exam.pk, clinic_id=self.clinic.pk, target=Examination.Status.ACQUIRED, actor=self.radiologist, membership=self.radiologist_membership)
        self.assertEqual(exam.status, Examination.Status.ACQUIRED)
        report = create_report_draft(examination_id=exam.pk, clinic_id=self.clinic.pk, actor=self.radiologist, membership=self.radiologist_membership)
        self.assertEqual(report.status, Report.Status.DRAFT)

    def test_appointment_and_examination_transitions_enforce_roles_and_audit(self):
        patient = self.patient()
        appointment = schedule_appointment(clinic_id=self.clinic.pk, actor=self.receptionist, membership=self.frontdesk_membership, patient_id=patient.pk, starts_at=datetime(2026, 11, 13, 12, tzinfo=dt_timezone.utc))
        exam = order_examination(clinic_id=self.clinic.pk, actor=self.radiologist, membership=self.radiologist_membership, patient_id=patient.pk, appointment_id=appointment.pk, modality="CT", procedure_name="CT chest")
        appointment.status = Appointment.Status.CHECKED_IN
        with self.assertRaises(ValidationError):
            appointment.save()
        with self.assertRaises(ValidationError):
            transition_appointment(appointment_id=appointment.pk, clinic_id=self.clinic.pk, target=Appointment.Status.CHECKED_IN, actor=self.technician, membership=self.technician_membership)
        transition_appointment(appointment_id=appointment.pk, clinic_id=self.clinic.pk, target=Appointment.Status.CHECKED_IN, actor=self.receptionist, membership=self.frontdesk_membership)
        transition_examination(examination_id=exam.pk, clinic_id=self.clinic.pk, target=Examination.Status.CHECKED_IN, actor=self.receptionist, membership=self.frontdesk_membership)
        with self.assertRaises(ValidationError):
            transition_examination(examination_id=exam.pk, clinic_id=self.clinic.pk, target=Examination.Status.ACQUIRED, actor=self.receptionist, membership=self.frontdesk_membership)
        with self.assertRaises(ValidationError):
            transition_examination(examination_id=exam.pk, clinic_id=self.clinic.pk, target=Examination.Status.ACQUIRED, actor=self.radiologist, membership=self.radiologist_membership)
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic, action="appointment.transition").exists())

    def test_report_immutability_release_amendment_and_audit_history(self):
        exam = self.examination()
        self.to_acquired(exam)
        report = create_report_draft(examination_id=exam.pk, clinic_id=self.clinic.pk, actor=self.radiologist, membership=self.radiologist_membership)
        report = edit_report_draft(report_id=report.pk, clinic_id=self.clinic.pk, actor=self.radiologist, membership=self.radiologist_membership, findings="No acute abnormality.", impression="No acute finding.")
        report = sign_report(report_id=report.pk, clinic_id=self.clinic.pk, actor=self.radiologist, membership=self.radiologist_membership)
        report.findings = "Changed after signing"
        with self.assertRaises(ValidationError):
            report.save()
        with self.assertRaises(ValidationError):
            Report.objects.filter(pk=report.pk).update(findings="Bulk bypass")
        with self.assertRaises(ValidationError):
            Report.objects.bulk_update([report], ["impression"])
        with self.assertRaises(ValidationError):
            report.delete()
        with self.assertRaises(ValidationError):
            Report.objects.filter(pk=report.pk).delete()
        report = release_report(report_id=report.pk, clinic_id=self.clinic.pk, actor=self.radiologist, membership=self.radiologist_membership)
        deliver_report(examination_id=exam.pk, clinic_id=self.clinic.pk, actor=self.receptionist, membership=self.frontdesk_membership)
        amendment = create_report_draft(examination_id=exam.pk, clinic_id=self.clinic.pk, actor=self.radiologist, membership=self.radiologist_membership, amends_id=report.pk)
        self.assertEqual(amendment.version, 2)
        self.assertEqual(amendment.amends_id, report.pk)
        duplicate_amendment = Report(clinic=self.clinic, examination=exam, version=2, amends=report, created_by=self.radiologist)
        with self.assertRaises(ValidationError):
            duplicate_amendment.save()
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic, action="report.signed").exists())
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic, action="report.released").exists())
        event = AuditEvent.objects.filter(clinic=self.clinic).first()
        event.action = "rewritten"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()

    def test_dicom_studies_series_and_instances_are_relational_and_scoped(self):
        exam = self.examination()
        self.to_acquired(exam)
        common = dict(
            clinic_id=self.clinic.pk, actor=self.technician, membership=self.technician_membership,
            examination_id=exam.pk, kind=ImagingStudy.Kind.DICOM, modality="CT",
            media_type="application/dicom", byte_size=128, checksum_sha256="a" * 64,
        )
        first = register_imaging_instance(**common, storage_key=f"images/{uuid.uuid4().hex}", original_name="1.dcm", study_instance_uid="1.2.3", series_instance_uid="1.2.3.1", sop_instance_uid="1.2.3.1.1")
        second = register_imaging_instance(**common, storage_key=f"images/{uuid.uuid4().hex}", original_name="2.dcm", study_id=first.series.study_id, series_id=first.series_id, sop_instance_uid="1.2.3.1.2")
        another = register_imaging_instance(**common, storage_key=f"images/{uuid.uuid4().hex}", original_name="3.dcm", study_instance_uid="1.2.4", series_instance_uid="1.2.4.1", sop_instance_uid="1.2.4.1.1")
        self.assertEqual(ImagingStudy.objects.filter(examination=exam, kind=ImagingStudy.Kind.DICOM).count(), 2)
        self.assertEqual(ImagingSeries.objects.filter(study=first.series.study).count(), 1)
        self.assertEqual(ImagingInstance.objects.filter(series=first.series).count(), 2)
        self.assertEqual(imaging_instance_for_user(user=self.technician, clinic_id=self.clinic.pk, instance_id=first.pk), first)
        foreign_patient = self.patient(clinic=self.other_clinic, actor=self.other, membership=self.other_membership, mrn="DICOM-FOREIGN")
        foreign_exam = order_examination(clinic_id=self.other_clinic.pk, actor=self.radiologist, membership=self.other_radiologist_membership, patient_id=foreign_patient.pk, modality="CT", procedure_name="CT")
        foreign_instance = register_imaging_instance(
            clinic_id=self.other_clinic.pk, actor=self.technician, membership=self.other_technician_membership,
            examination_id=foreign_exam.pk, kind=ImagingStudy.Kind.DICOM, modality="CT",
            study_instance_uid="1.2.5", series_instance_uid="1.2.5.1", sop_instance_uid="1.2.5.1.1",
            storage_key=f"images/{uuid.uuid4().hex}", media_type="application/dicom", original_name="f.dcm",
            byte_size=128, checksum_sha256="c" * 64,
        )
        with self.assertRaises(ImagingInstance.DoesNotExist):
            imaging_instance_for_user(user=self.technician, clinic_id=self.clinic.pk, instance_id=foreign_instance.pk)
        with self.assertRaises(PermissionDenied):
            imaging_instance_for_user(user=self.other, clinic_id=self.clinic.pk, instance_id=first.pk)
        with self.assertRaises(ValidationError):
            register_imaging_instance(**{**common, "study_instance_uid": "1.2.03", "series_instance_uid": "1.2.3.1", "sop_instance_uid": "1.2.3.1.2", "storage_key": f"images/{uuid.uuid4().hex}", "original_name": "invalid.dcm"})

    def test_partial_payments_history_and_outstanding_balance(self):
        patient = self.patient()
        invoice = create_invoice(clinic_id=self.clinic.pk, actor=self.receptionist, membership=self.frontdesk_membership, patient_id=patient.pk, amount_minor=10000)
        first = record_payment(invoice_id=invoice.pk, clinic_id=self.clinic.pk, amount_minor=4000, currency="INR", method="cash", reference="r1", actor=self.receptionist, membership=self.frontdesk_membership)
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_minor, 4000)
        self.assertEqual(invoice.balance_minor, 6000)
        with self.assertRaises(ValidationError):
            record_payment(invoice_id=invoice.pk, clinic_id=self.clinic.pk, amount_minor=7000, currency="INR", method="cash", reference="r2", actor=self.receptionist, membership=self.frontdesk_membership)
        reverse_payment(payment_id=first.pk, clinic_id=self.clinic.pk, reason="Duplicate entry", actor=self.owner, membership=self.owner_membership)
        invoice.refresh_from_db()
        self.assertEqual(invoice.balance_minor, 10000)
        with self.assertRaises(ValidationError):
            first.delete()
        self.assertTrue(AuditEvent.objects.filter(action="payment.reversed").exists())

    def test_ai_output_is_provenanced_separate_and_requires_human_review(self):
        exam = self.examination()
        self.to_acquired(exam)
        instance = register_imaging_instance(
            clinic_id=self.clinic.pk, actor=self.technician, membership=self.technician_membership,
            examination_id=exam.pk, kind=ImagingStudy.Kind.RASTER, modality="XR",
            storage_key=f"images/{uuid.uuid4().hex}", media_type="image/png", original_name="xray.png",
            byte_size=99, checksum_sha256="b" * 64,
        )
        analysis = record_ai_analysis(
            clinic_id=self.clinic.pk, actor=self.radiologist, membership=self.radiologist_membership,
            examination_id=exam.pk, imaging_study_id=instance.series.study_id,
            provider="example-provider", model_name="demo-model", model_version="1.0",
            task="triage", output={"suggestion": "review"}, uncertainty={"score": 0.2},
            provenance={"input_instance": str(instance.pk)},
        )
        self.assertEqual(analysis.status, AIAnalysis.ReviewStatus.PENDING)
        with self.assertRaises(ValidationError):
            AIAnalysis(clinic=self.clinic, examination=exam, imaging_study=instance.series.study, provider="x", model_name="x", model_version="x", task="x", output={}).save()
        with self.assertRaises(ValidationError):
            review_ai_analysis(analysis_id=analysis.pk, clinic_id=self.clinic.pk, accepted=True, actor=self.receptionist, membership=self.frontdesk_membership)
        reviewed = review_ai_analysis(analysis_id=analysis.pk, clinic_id=self.clinic.pk, accepted=True, actor=self.radiologist, membership=self.radiologist_membership)
        self.assertEqual(reviewed.status, AIAnalysis.ReviewStatus.ACCEPTED)
        self.assertFalse(Report.objects.filter(examination=exam).exists())
        self.assertTrue(AuditEvent.objects.filter(action="ai_analysis.reviewed").exists())


class EvidenceVerificationTests(TestCase):
    def test_supported_contradicted_unsupported_and_uncertain_are_distinct(self):
        evidence = ["No pleural effusion is identified."]
        results = verify_claims([
            "No pleural effusion is identified.",
            "Pleural effusion is present.",
            "Acute aortic dissection is present.",
            "Pleural fluid may be present.",
        ], evidence)
        self.assertEqual([item["status"] for item in results], ["supported", "contradicted", "unsupported", "uncertain"])
        self.assertTrue(all("similarity_signal" in item and "reason" in item for item in results))

    def test_xray_structurer_keeps_impression_blank_and_requires_human_review(self):
        evidence = {"observations": [{"anatomy": "lungs", "text": "No focal opacity.", "polarity": "negative"}]}
        sections, verification = build_assist(evidence, "CHEST_XRAY")
        self.assertEqual(sections["impression"], "")
        self.assertEqual(verification["modality"], "CHEST_XRAY")
        self.assertTrue(verification["human_review_required"])
        self.assertEqual(verification["assessment"], "attributed_to_supplied_observations")

    def test_rule_extractor_tracks_spans_negation_uncertainty_and_duration(self):
        source = "No fever but cough for 3 days; possibly wheezing."
        mentions = extract_symptoms(source)
        self.assertEqual([item["normalized"] for item in mentions], ["fever", "cough", "wheezing"])
        self.assertEqual([item["assertion"] for item in mentions], ["absent", "present", "uncertain"])
        self.assertEqual(source[mentions[1]["start"]:mentions[1]["end"]].lower(), "cough")
        self.assertIn("3 days", mentions[1]["duration_text"])
        self.assertIsNone(mentions[0]["confidence"])

