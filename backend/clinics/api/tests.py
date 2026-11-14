from datetime import timedelta
from io import BytesIO
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient
from PIL import Image

from clinics.domain import (
    create_invoice, create_report_draft, order_examination,
    record_ai_analysis, record_payment, register_imaging_instance,
    register_patient, schedule_appointment, transition_appointment,
    transition_examination,
)
from clinics.imaging import ingest_uploads
from clinics.report_pdf import build_signed_report_pdf
from clinics.models import (
    AIAnalysis, AIReportAssist, AccountSecurityState, ClinicalHistoryEntry, Appointment, AuditEvent, AuthenticationEvent, Clinic, ChargeItem, Invoice,
    Examination, ImagingInstance, ImagingStudy, Membership, MembershipRole, Payment, Refund, Report,
)


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    REST_FRAMEWORK={
        "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
        "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
        "DEFAULT_RENDERER_CLASSES": ["clinics.api.responses.EnvelopeJSONRenderer"],
        "DEFAULT_PAGINATION_CLASS": "clinics.api.pagination.ClinicPageNumberPagination",
        "EXCEPTION_HANDLER": "clinics.api.errors.api_exception_handler",
        "DEFAULT_THROTTLE_RATES": {"anon": "1000/hour"},
    },
)
class FoundationAPITests(TestCase):
    def setUp(self):
        throttle_patch = patch("clinics.api.views.LoginView.throttle_classes", [])
        throttle_patch.start()
        self.addCleanup(throttle_patch.stop)
        self.private_root = tempfile.mkdtemp(prefix="clinic-private-images-api-")
        self.storage_settings = override_settings(PRIVATE_IMAGE_STORAGE_ROOT=self.private_root)
        self.storage_settings.enable()
        self.addCleanup(self.storage_settings.disable)
        self.addCleanup(shutil.rmtree, self.private_root, ignore_errors=True)
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(username="owner", password="Safe-test-password-1")
        self.receptionist = user_model.objects.create_user(username="desk", password="Safe-test-password-1")
        self.radiologist = user_model.objects.create_user(username="rad", password="Safe-test-password-1")
        self.technician = user_model.objects.create_user(username="tech", password="Safe-test-password-1")
        self.foreign_staff = user_model.objects.create_user(username="foreign", password="Safe-test-password-1")
        self.new_staff = user_model.objects.create_user(username="new-staff", password="Safe-test-password-1")
        self.clinic_a = Clinic.objects.create(name="North Imaging")
        self.clinic_b = Clinic.objects.create(name="South Imaging")
        self.owner_a = self.add_membership(self.clinic_a, self.owner, [MembershipRole.Role.OWNER])
        self.owner_b = self.add_membership(self.clinic_b, self.owner, [MembershipRole.Role.OWNER])
        self.desk_a = self.add_membership(self.clinic_a, self.receptionist, [MembershipRole.Role.RECEPTIONIST])
        self.rad_a = self.add_membership(self.clinic_a, self.radiologist, [MembershipRole.Role.RADIOLOGIST, MembershipRole.Role.TECHNICIAN])
        self.tech_a = self.add_membership(self.clinic_a, self.technician, [MembershipRole.Role.TECHNICIAN])
        self.foreign_membership = self.add_membership(self.clinic_b, self.foreign_staff, [MembershipRole.Role.RECEPTIONIST])
        self.patient_a = register_patient(
            clinic_id=self.clinic_a.pk, actor=self.receptionist, membership=self.desk_a,
            medical_record_number="A-100", given_name="Leena", family_name="Das",
        )
        self.patient_b = register_patient(
            clinic_id=self.clinic_b.pk, actor=self.foreign_staff, membership=self.foreign_membership,
            medical_record_number="B-200", given_name="Ravi", family_name="Kapoor",
        )
        self.exam_a = order_examination(
            clinic_id=self.clinic_a.pk, actor=self.radiologist, membership=self.rad_a,
            patient_id=self.patient_a.pk, modality="CT", procedure_name="CT chest",
        )

    @staticmethod
    def png_file(name="synthetic.png", color=90):
        image = BytesIO()
        Image.new("L", (3, 2), color=color).save(image, format="PNG")
        return SimpleUploadedFile(name, image.getvalue(), content_type="image/png")

    @staticmethod
    def jpeg_file(name="synthetic.jpg", color=90):
        image = BytesIO()
        Image.new("RGB", (3, 2), color=(color, color, color)).save(image, format="JPEG")
        return SimpleUploadedFile(name, image.getvalue(), content_type="image/jpeg")

    def advance_to_acquisition(self, client, token, exam_id, *, upload=True):
        desk, desk_token = self.login_client(self.receptionist)
        self.select_clinic(desk, desk_token, self.clinic_a.pk)
        response = desk.post(f"/api/v1/examinations/{exam_id}/transition/", {"target": "checked_in"}, format="json", HTTP_X_CSRFTOKEN=desk_token)
        self.assertEqual(response.status_code, 200, response.content)
        response = client.post(f"/api/v1/examinations/{exam_id}/transition/", {"target": "in_progress"}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200, response.content)
        if upload:
            uploaded = client.post(f"/api/v1/examinations/{exam_id}/imaging/", {"files": [self.png_file()]}, format="multipart", HTTP_X_CSRFTOKEN=token)
            self.assertEqual(uploaded.status_code, 200, uploaded.content)
        response = client.post(f"/api/v1/examinations/{exam_id}/transition/", {"target": "acquired"}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200, response.content)

    @staticmethod
    def add_membership(clinic, user, roles):
        membership = Membership(clinic=clinic, user=user)
        membership.save()
        for role in roles:
            grant = MembershipRole(clinic=clinic, membership=membership, role=role)
            grant._domain_service_write = True
            grant.save()
        return membership

    def csrf(self, client):
        response = client.get("/api/v1/auth/csrf/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"data"})
        return body["data"]["csrf_token"]

    def login_client(self, user, client=None):
        client = client or APIClient(enforce_csrf_checks=True)
        token = self.csrf(client)
        response = client.post(
            "/api/v1/auth/login/",
            {"username": user.username, "password": "Safe-test-password-1"},
            format="json", HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200, response.content)
        return client, response.json()["data"]["csrf_token"]

    def select_clinic(self, client, token, clinic_id):
        response = client.post(
            "/api/v1/clinics/active/", {"clinic_id": str(clinic_id)},
            format="json", HTTP_X_CSRFTOKEN=token,
        )
        return response

    def test_login_is_csrf_protected_generic_and_audited_without_identifier(self):
        client = APIClient(enforce_csrf_checks=True)
        token = self.csrf(client)
        no_csrf = client.post("/api/v1/auth/login/", {"username": "desk", "password": "bad"}, format="json")
        self.assertEqual(no_csrf.status_code, 403)
        self.assertEqual(no_csrf.json()["error"]["code"], "csrf_failed")
        bad = client.post(
            "/api/v1/auth/login/", {"username": "private-user", "password": "bad"},
            format="json", HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(bad.json()["error"]["message"], "Invalid username or password.")
        self.assertNotIn("private-user", bad.content.decode())
        self.assertNotIn("bad", bad.content.decode())
        event = AuthenticationEvent.objects.get(action=AuthenticationEvent.Action.LOGIN_FAILED)
        self.assertIsNone(event.user_id)

    def test_multiple_clinics_and_server_validated_active_context(self):
        client, token = self.login_client(self.owner)
        me = client.get("/api/v1/auth/me/")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(len(me.json()["data"]["memberships"]), 2)
        selected = self.select_clinic(client, token, self.clinic_b.pk)
        self.assertEqual(selected.status_code, 200, selected.content)
        self.assertEqual(selected.json()["data"]["clinic"]["id"], str(self.clinic_b.pk))
        active = client.get("/api/v1/clinics/active/")
        self.assertEqual(active.json()["data"]["clinic"]["id"], str(self.clinic_b.pk))
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_b, action="clinic.context_selected").exists())

    def test_owner_can_add_existing_user_and_grant_roles_with_audit(self):
        client, token = self.login_client(self.owner)
        self.select_clinic(client, token, self.clinic_a.pk)
        created = client.post(
            "/api/v1/memberships/",
            {"user_id": self.new_staff.pk, "roles": [MembershipRole.Role.RECEPTIONIST]},
            format="json", HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(created.status_code, 201, created.content)
        membership_id = created.json()["data"]["id"]
        assigned = client.post(
            f"/api/v1/memberships/{membership_id}/roles/",
            {"role": MembershipRole.Role.TECHNICIAN},
            format="json", HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(assigned.status_code, 201, assigned.content)
        self.assertTrue(Membership.objects.get(pk=membership_id).has_role(MembershipRole.Role.TECHNICIAN))
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="membership.created").exists())
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="membership.role_assigned").exists())

    def test_membership_revocation_invalidates_existing_active_clinic_context(self):
        desk_client, desk_token = self.login_client(self.receptionist)
        self.select_clinic(desk_client, desk_token, self.clinic_a.pk)
        owner_client, owner_token = self.login_client(self.owner)
        self.select_clinic(owner_client, owner_token, self.clinic_a.pk)
        changed = owner_client.patch(
            f"/api/v1/memberships/{self.desk_a.pk}/active/", {"is_active": False},
            format="json", HTTP_X_CSRFTOKEN=owner_token,
        )
        self.assertEqual(changed.status_code, 200, changed.content)
        blocked = desk_client.get("/api/v1/patients/")
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json()["error"]["message"], "Active clinic context is unavailable.")

    def test_cannot_select_or_read_an_unaffiliated_tenant(self):
        client, token = self.login_client(self.receptionist)
        denied = self.select_clinic(client, token, self.clinic_b.pk)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["message"], "No active membership for this clinic.")
        self.select_clinic(client, token, self.clinic_a.pk)
        response = client.get(f"/api/v1/patients/{self.patient_b.pk}/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")
        listing = client.get("/api/v1/patients/")
        self.assertEqual(listing.status_code, 200)
        returned_ids = [row["id"] for row in listing.json()["data"]["results"]]
        self.assertEqual(returned_ids, [str(self.patient_a.pk)])

    def test_receptionist_gets_operational_exam_status_without_clinical_details(self):
        client, token = self.login_client(self.receptionist)
        self.select_clinic(client, token, self.clinic_a.pk)
        response = client.get("/api/v1/examinations/")
        self.assertEqual(response.status_code, 200)
        exam = response.json()["data"]["results"][0]
        self.assertIn("status", exam)
        self.assertNotIn("clinical_indication", exam)
        self.assertNotIn("procedure_name", exam)

    def test_patient_search_is_paginated_clinic_scoped_and_history_is_radiologist_only(self):
        for number in range(30):
            register_patient(
                clinic_id=self.clinic_a.pk, actor=self.receptionist, membership=self.desk_a,
                medical_record_number=f"PAGE-{number:03}", given_name="Nisha", family_name="Searchable",
                phone=f"900000{number:04}",
            )
        desk, token = self.login_client(self.receptionist)
        self.select_clinic(desk, token, self.clinic_a.pk)
        page1 = desk.get("/api/v1/patients/?search=Searchable")
        self.assertEqual(page1.status_code, 200)
        self.assertEqual(page1.json()["data"]["count"], 30)
        self.assertEqual(len(page1.json()["data"]["results"]), 25)
        page2 = desk.get("/api/v1/patients/?search=Searchable&page=2")
        self.assertEqual(len(page2.json()["data"]["results"]), 5)
        phone_match = desk.get("/api/v1/patients/?search=9000000007")
        self.assertEqual(phone_match.json()["data"]["count"], 1)
        foreign_search = desk.get("/api/v1/patients/?search=Ravi")
        self.assertEqual(foreign_search.json()["data"]["count"], 0)
        duplicate_local = desk.get("/api/v1/patients/potential-duplicates/?medical_record_number=A-100")
        self.assertEqual([x["id"] for x in duplicate_local.json()["data"]], [str(self.patient_a.pk)])
        duplicate_foreign = desk.get("/api/v1/patients/potential-duplicates/?medical_record_number=B-200")
        self.assertEqual(duplicate_foreign.json()["data"], [])
        blocked_history = desk.get(f"/api/v1/patients/{self.patient_a.pk}/clinical-history/")
        self.assertEqual(blocked_history.status_code, 403)

        rad, _ = self.login_client(self.radiologist)
        self.select_clinic(rad, _, self.clinic_a.pk)
        own_history = rad.get(f"/api/v1/patients/{self.patient_a.pk}/clinical-history/")
        self.assertEqual(own_history.status_code, 200)
        self.assertEqual(own_history.json()["data"]["patient_id"], str(self.patient_a.pk))
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="patient.clinical_history_accessed", object_id=str(self.patient_a.pk)).exists())
        self.assertEqual(rad.get(f"/api/v1/patients/{self.patient_b.pk}/clinical-history/").status_code, 404)

    def test_both_staffing_scenarios_complete_with_assignment_audit_and_report_delivery(self):
        for label, use_technician in (("TECH", True), ("SOLO", False)):
            desk, desk_token = self.login_client(self.receptionist)
            self.select_clinic(desk, desk_token, self.clinic_a.pk)
            patient_response = desk.post("/api/v1/patients/", {
                "medical_record_number": f"E2E-{label}", "given_name": "Mira", "family_name": label,
            }, format="json", HTTP_X_CSRFTOKEN=desk_token)
            self.assertEqual(patient_response.status_code, 201, patient_response.content)
            patient_id = patient_response.json()["data"]["id"]
            appointment_response = desk.post("/api/v1/appointments/", {
                "patient_id": patient_id, "starts_at": (timezone.now() + timedelta(days=1)).isoformat(),
                "reason": "Imaging visit",
            }, format="json", HTTP_X_CSRFTOKEN=desk_token)
            self.assertEqual(appointment_response.status_code, 201, appointment_response.content)
            appointment_id = appointment_response.json()["data"]["id"]
            checked_appointment = desk.post(f"/api/v1/appointments/{appointment_id}/transition/", {"target": "checked_in"}, format="json", HTTP_X_CSRFTOKEN=desk_token)
            self.assertEqual(checked_appointment.status_code, 200, checked_appointment.content)

            rad, rad_token = self.login_client(self.radiologist)
            self.select_clinic(rad, rad_token, self.clinic_a.pk)
            exam_response = rad.post("/api/v1/examinations/", {
                "patient_id": patient_id, "appointment_id": appointment_id,
                "modality": "CT", "procedure_name": "CT chest", "clinical_indication": "Synthetic test indication",
            }, format="json", HTTP_X_CSRFTOKEN=rad_token)
            self.assertEqual(exam_response.status_code, 201, exam_response.content)
            exam_id = exam_response.json()["data"]["id"]
            if use_technician:
                assigned = rad.post(f"/api/v1/examinations/{exam_id}/assignment/", {
                    "technician_id": self.technician.pk, "radiologist_id": self.radiologist.pk,
                }, format="json", HTTP_X_CSRFTOKEN=rad_token)
                self.assertEqual(assigned.status_code, 200, assigned.content)
                self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="examination.assignment_changed", object_id=assigned.json()["data"]["id"]).exists())

            checked_exam = desk.post(f"/api/v1/examinations/{exam_id}/transition/", {"target": "checked_in"}, format="json", HTTP_X_CSRFTOKEN=desk_token)
            self.assertEqual(checked_exam.status_code, 200, checked_exam.content)
            operator, operator_token = (self.login_client(self.technician) if use_technician else (rad, rad_token))
            if use_technician:
                self.select_clinic(operator, operator_token, self.clinic_a.pk)
                self.assertEqual(operator.get("/api/v1/examinations/").json()["data"]["count"], 1)
            response = operator.post(f"/api/v1/examinations/{exam_id}/transition/", {"target": "in_progress"}, format="json", HTTP_X_CSRFTOKEN=operator_token)
            self.assertEqual(response.status_code, 200, response.content)
            uploaded = operator.post(f"/api/v1/examinations/{exam_id}/imaging/", {"files": [self.png_file(f"{label}.png")]}, format="multipart", HTTP_X_CSRFTOKEN=operator_token)
            self.assertEqual(uploaded.status_code, 200, uploaded.content)
            response = operator.post(f"/api/v1/examinations/{exam_id}/transition/", {"target": "acquired"}, format="json", HTTP_X_CSRFTOKEN=operator_token)
            self.assertEqual(response.status_code, 200, response.content)

            draft = rad.post("/api/v1/reports/", {"examination_id": exam_id}, format="json", HTTP_X_CSRFTOKEN=rad_token)
            self.assertEqual(draft.status_code, 201, draft.content)
            report_id = draft.json()["data"]["id"]
            edited = rad.patch(f"/api/v1/reports/{report_id}/", {"findings": "Synthetic verified finding", "impression": "Synthetic impression", "expected_updated_at": draft.json()["data"]["updated_at"]}, format="json", HTTP_X_CSRFTOKEN=rad_token)
            self.assertEqual(edited.status_code, 200, edited.content)
            self.assertEqual(rad.post(f"/api/v1/reports/{report_id}/sign/", {"expected_updated_at": edited.json()["data"]["updated_at"]}, format="json", HTTP_X_CSRFTOKEN=rad_token).status_code, 200)
            self.assertEqual(rad.post(f"/api/v1/reports/{report_id}/release/", {}, format="json", HTTP_X_CSRFTOKEN=rad_token).status_code, 200)

            status_view = desk.get("/api/v1/examinations/")
            shown = next(x for x in status_view.json()["data"]["results"] if x["id"] == exam_id)
            self.assertEqual(shown["status"], "released")
            self.assertNotIn("clinical_indication", shown)
            self.assertNotIn("procedure_name", shown)
            delivered = desk.post(f"/api/v1/examinations/{exam_id}/deliver/", {}, format="json", HTTP_X_CSRFTOKEN=desk_token)
            self.assertEqual(delivered.status_code, 200, delivered.content)
            exam = Examination.objects.get(pk=exam_id)
            report = Report.objects.get(pk=report_id)
            self.assertEqual(exam.status, Examination.Status.DELIVERED)
            self.assertEqual(report.status, Report.Status.DELIVERED)
            self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="report.signed", object_id=report_id).exists())
            self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="report.released", object_id=report_id).exists())
            self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="report.delivered", object_id=report_id).exists())

    def test_structured_report_template_signing_pdf_delivery_and_amendment(self):
        owner, owner_token = self.login_client(self.owner)
        self.select_clinic(owner, owner_token, self.clinic_a.pk)
        profile = owner.patch("/api/v1/clinics/profile/", {
            "address": "100 Synthetic Avenue", "phone": "+91 00000 00000",
            "email": "clinic@example.invalid",
        }, format="json", HTTP_X_CSRFTOKEN=owner_token)
        self.assertEqual(profile.status_code, 200, profile.content)
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="clinic.profile_updated").exists())
        self.radiologist.first_name = "Riya"
        self.radiologist.last_name = "Reader"
        self.radiologist.save()
        registration = owner.patch(f"/api/v1/memberships/{self.rad_a.pk}/profile/", {
            "professional_registration_number": "SYNTHETIC-REG-01",
        }, format="json", HTTP_X_CSRFTOKEN=owner_token)
        self.assertEqual(registration.status_code, 200, registration.content)
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="membership.professional_registration_updated").exists())
        rad, rad_token = self.login_client(self.radiologist)
        self.select_clinic(rad, rad_token, self.clinic_a.pk)
        self.advance_to_acquisition(rad, rad_token, self.exam_a.pk)

        template_list = rad.get("/api/v1/reports/templates/")
        self.assertEqual(template_list.status_code, 200)
        self.assertEqual([row["key"] for row in template_list.json()["data"]], ["xray", "ct", "mri", "ultrasound", "general"])
        self.assertIn("comparison", template_list.json()["data"][1]["fields"])
        self.assertIn("sequences", template_list.json()["data"][2]["fields"])
        wrong_template = rad.post("/api/v1/reports/", {"examination_id": str(self.exam_a.pk), "template": "mri"}, format="json", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(wrong_template.status_code, 400)
        created = rad.post("/api/v1/reports/", {"examination_id": str(self.exam_a.pk), "template": "ct"}, format="json", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(created.status_code, 201, created.content)
        report_id = created.json()["data"]["id"]
        self.assertEqual(created.json()["data"]["template"], "ct")
        self.assertEqual(created.json()["data"]["findings"], "")
        bypass_attempt = Report.objects.get(pk=report_id)
        bypass_attempt.technique = "Unaudited direct model write"
        with self.assertRaises(DjangoValidationError):
            bypass_attempt.save()
        draft_pdf = rad.get(f"/api/v1/reports/{created.json()['data']['id']}/pdf/")
        self.assertEqual(draft_pdf.status_code, 400)
        unsigned_release = rad.post(f"/api/v1/reports/{report_id}/release/", {}, format="json", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(unsigned_release.status_code, 400)
        self.assertIn("Report must be signed", unsigned_release.json()["error"]["message"])
        empty_sign = rad.post(f"/api/v1/reports/{report_id}/sign/", {"expected_updated_at": created.json()["data"]["updated_at"]}, format="json", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(empty_sign.status_code, 400)
        edited = rad.patch(f"/api/v1/reports/{report_id}/", {
            "technique": "Synthetic technique text", "findings": "Synthetic observed finding",
            "impression": "Synthetic impression text", "additional_comments": "Synthetic note",
            "expected_updated_at": created.json()["data"]["updated_at"],
        }, format="json", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(edited.status_code, 200, edited.content)
        signed = rad.post(f"/api/v1/reports/{report_id}/sign/", {"expected_updated_at": edited.json()["data"]["updated_at"]}, format="json", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(signed.status_code, 200, signed.content)
        context = signed.json()["data"]["signed_context"]
        self.assertEqual(context["patient"]["medical_record_number"], "A-100")
        self.assertEqual(context["clinic"]["email"], "clinic@example.invalid")
        self.assertEqual(context["radiologist"]["registration_number"], "SYNTHETIC-REG-01")

        anonymous = APIClient(enforce_csrf_checks=True).get(f"/api/v1/reports/{report_id}/pdf/", HTTP_ACCEPT="application/pdf, application/json")
        self.assertIn(anonymous.status_code, {401, 403})
        pdf = rad.get(f"/api/v1/reports/{report_id}/pdf/", HTTP_ACCEPT="application/pdf, application/json")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertIn("private, no-store", pdf["Cache-Control"])
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="report.pdf_exported", object_id=report_id).exists())
        self.add_membership(self.clinic_b, self.radiologist, [MembershipRole.Role.RADIOLOGIST])
        foreign, foreign_token = self.login_client(self.radiologist)
        self.select_clinic(foreign, foreign_token, self.clinic_b.pk)
        self.assertEqual(foreign.get(f"/api/v1/reports/{report_id}/pdf/", HTTP_ACCEPT="application/pdf, application/json").status_code, 404)
        self.assertEqual(foreign.post(f"/api/v1/reports/{report_id}/release/", {}, format="json", HTTP_X_CSRFTOKEN=foreign_token).status_code, 404)
        blocked_edit = rad.patch(f"/api/v1/reports/{report_id}/", {"findings": "silent overwrite", "expected_updated_at": edited.json()["data"]["updated_at"]}, format="json", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(blocked_edit.status_code, 400)
        locked = Report.objects.get(pk=report_id)
        locked.findings = "silent overwrite"
        with self.assertRaises(DjangoValidationError):
            locked.save()
        with self.assertRaises(DjangoValidationError):
            Report.objects.filter(pk=report_id).update(findings="bulk overwrite")

        desk, desk_token = self.login_client(self.receptionist)
        self.select_clinic(desk, desk_token, self.clinic_a.pk)
        denied_release = desk.post(f"/api/v1/reports/{report_id}/release/", {}, format="json", HTTP_X_CSRFTOKEN=desk_token)
        self.assertEqual(denied_release.status_code, 403)
        signed_snapshot = Report.objects.get(pk=report_id)
        signed_fields = (signed_snapshot.technique, signed_snapshot.sequences, signed_snapshot.comparison, signed_snapshot.findings, signed_snapshot.impression, signed_snapshot.additional_comments, signed_snapshot.signed_context.copy())
        released = rad.post(f"/api/v1/reports/{report_id}/release/", {}, format="json", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(released.status_code, 200, released.content)
        self.assertEqual(released.json()["data"]["status"], "released")
        after_release = Report.objects.get(pk=report_id)
        self.assertEqual((after_release.technique, after_release.sequences, after_release.comparison, after_release.findings, after_release.impression, after_release.additional_comments, after_release.signed_context), signed_fields)
        self.assertEqual(desk.get(f"/api/v1/reports/{report_id}/").status_code, 403)
        self.assertEqual(desk.get(f"/api/v1/reports/{report_id}/pdf/").status_code, 403)
        self.assertNotIn("Synthetic observed finding", desk.get("/api/v1/examinations/").content.decode())
        delivered = desk.post(f"/api/v1/examinations/{self.exam_a.pk}/deliver/", {}, format="json", HTTP_X_CSRFTOKEN=desk_token)
        self.assertEqual(delivered.status_code, 200, delivered.content)
        self.assertEqual(Report.objects.get(pk=report_id).status, Report.Status.DELIVERED)
        self.assertIsNotNone(Report.objects.get(pk=report_id).delivered_at)

        amendment = rad.post("/api/v1/reports/", {"examination_id": str(self.exam_a.pk), "amends_id": report_id}, format="json", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(amendment.status_code, 201, amendment.content)
        self.assertEqual(amendment.json()["data"]["version"], 2)
        self.assertEqual(amendment.json()["data"]["findings"], "Synthetic observed finding")
        with patch("clinics.api.views.build_signed_report_pdf", wraps=build_signed_report_pdf) as pdf_builder:
            earlier_version_pdf = rad.get(f"/api/v1/reports/{report_id}/pdf/", HTTP_ACCEPT="application/pdf, application/json")
            self.assertEqual(pdf_builder.call_args.args[0].version, 1)
            self.assertEqual(pdf_builder.call_args.args[0].findings, "Synthetic observed finding")
        self.assertEqual(earlier_version_pdf.status_code, 200)
        self.assertTrue(earlier_version_pdf.content.startswith(b"%PDF"))
        self.assertEqual(Report.objects.get(pk=report_id).findings, "Synthetic observed finding")
        history = rad.get("/api/v1/reports/").json()["data"]["results"]
        self.assertEqual({item["version"] for item in history if item["examination_id"] == str(self.exam_a.pk)}, {1, 2})

    def test_superseded_signed_report_cannot_be_released_and_gets_specific_validation(self):
        rad, token = self.login_client(self.radiologist)
        self.select_clinic(rad, token, self.clinic_a.pk)
        self.advance_to_acquisition(rad, token, self.exam_a.pk)
        created = rad.post("/api/v1/reports/", {"examination_id": str(self.exam_a.pk)}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(created.status_code, 201, created.content)
        report_id = created.json()["data"]["id"]
        edited = rad.patch(f"/api/v1/reports/{report_id}/", {
            "findings": "Persisted signed finding", "impression": "Persisted signed impression",
            "expected_updated_at": created.json()["data"]["updated_at"],
        }, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(edited.status_code, 200, edited.content)
        signed = rad.post(f"/api/v1/reports/{report_id}/sign/", {"expected_updated_at": edited.json()["data"]["updated_at"]}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(signed.status_code, 200, signed.content)
        amendment = rad.post("/api/v1/reports/", {"examination_id": str(self.exam_a.pk), "amends_id": report_id}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(amendment.status_code, 201, amendment.content)
        rejected = rad.post(f"/api/v1/reports/{report_id}/release/", {}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()["error"]["message"], "Only the latest report version can be released.")
        stored = Report.objects.get(pk=report_id)
        self.assertEqual(stored.status, Report.Status.SIGNED)
        self.assertEqual(stored.findings, "Persisted signed finding")
        self.assertEqual(stored.impression, "Persisted signed impression")

    def test_ai_report_assistance_is_separate_audited_radiologist_only_and_tenant_scoped(self):
        rad, token = self.login_client(self.radiologist)
        self.select_clinic(rad, token, self.clinic_a.pk)
        exam = order_examination(clinic_id=self.clinic_a.pk, actor=self.radiologist, membership=self.rad_a, patient_id=self.patient_a.pk, modality="XR", procedure_name="XR chest")
        self.advance_to_acquisition(rad, token, exam.pk)
        created = rad.post("/api/v1/reports/", {"examination_id": str(exam.pk), "template": "xray"}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(created.status_code, 201, created.content)
        report_id = created.json()["data"]["id"]
        endpoint = f"/api/v1/reports/{report_id}/ai-assist/"
        evidence = {"clinical_indication": "Synthetic indication only", "technique": "Synthetic X-ray protocol", "observations": [{"anatomy": "lungs", "text": "Small left pleural effusion.", "polarity": "positive"}]}
        result = rad.post(endpoint, evidence, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(result.status_code, 201, result.content)
        data = result.json()["data"]
        self.assertEqual(data["generated_sections"]["impression"], "")
        self.assertIn("Small left pleural effusion", data["generated_sections"]["findings"])
        self.assertFalse(data["provenance"]["image_processing"])
        self.assertEqual(Report.objects.get(pk=report_id).findings, "")
        self.assertEqual(data["verification"]["claims"][0]["status"], "supported")
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="ai_report_assist.generated", object_id=data["id"]).exists())
        self.assertEqual(len(rad.get(f"/api/v1/reports/{report_id}/ai-assists/").json()["data"]), 1)
        review = rad.post(f"/api/v1/reports/{report_id}/review-ai-assist/", {"assist_id": data["id"], "decision": "accepted"}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(review.status_code, 200, review.content)
        self.assertEqual(AIReportAssist.objects.get(pk=data["id"]).review_status, AIReportAssist.ReviewStatus.ACCEPTED)
        self.assertEqual(Report.objects.get(pk=report_id).findings, "", "Acceptance must not silently write the report draft.")
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="ai_report_assist.reviewed", object_id=data["id"]).exists())

        desk, desk_token = self.login_client(self.receptionist)
        self.select_clinic(desk, desk_token, self.clinic_a.pk)
        self.assertEqual(desk.post(endpoint, evidence, format="json", HTTP_X_CSRFTOKEN=desk_token).status_code, 403)
        self.assertEqual(desk.get(f"/api/v1/reports/{report_id}/ai-assists/").status_code, 403)
        tech, tech_token = self.login_client(self.technician)
        self.select_clinic(tech, tech_token, self.clinic_a.pk)
        self.assertEqual(tech.post(endpoint, evidence, format="json", HTTP_X_CSRFTOKEN=tech_token).status_code, 403)
        foreign, foreign_token = self.login_client(self.foreign_staff)
        self.select_clinic(foreign, foreign_token, self.clinic_b.pk)
        self.assertIn(foreign.post(endpoint, evidence, format="json", HTTP_X_CSRFTOKEN=foreign_token).status_code, {403, 404})
        self.assertEqual(AIReportAssist.objects.filter(report_id=report_id).count(), 1)

    def test_ai_report_assistance_rejects_ct_and_missing_evidence(self):
        rad, token = self.login_client(self.radiologist)
        self.select_clinic(rad, token, self.clinic_a.pk)
        self.advance_to_acquisition(rad, token, self.exam_a.pk)
        created = rad.post("/api/v1/reports/", {"examination_id": str(self.exam_a.pk), "template": "ct"}, format="json", HTTP_X_CSRFTOKEN=token)
        report_id = created.json()["data"]["id"]
        response = rad.post(f"/api/v1/reports/{report_id}/ai-assist/", {"observations": [{"anatomy": "lungs", "text": "Synthetic CT observation.", "polarity": "positive"}]}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 400)
        response = rad.post(f"/api/v1/reports/{report_id}/ai-assist/", {"observations": []}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AIReportAssist.objects.count(), 0)

    def test_symptom_extraction_is_clinician_reviewed_immutable_and_restricted(self):
        rad, token = self.login_client(self.radiologist)
        self.select_clinic(rad, token, self.clinic_a.pk)
        endpoint = f"/api/v1/patients/{self.patient_a.pk}/clinical-history/entries/"
        source = "  Patient reports cough for 3 days. Denies fever but possibly has wheezing.\n"
        response = rad.post(endpoint, {"original_text": source}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 201, response.content)
        item = response.json()["data"]
        self.assertEqual(item["original_text"], source)
        self.assertEqual([mention["normalized"] for mention in item["extracted_mentions"]], ["cough", "fever", "wheezing"])
        self.assertEqual([mention["assertion"] for mention in item["extracted_mentions"]], ["present", "absent", "uncertain"])
        self.assertIn("3 days", item["extracted_mentions"][0]["duration_text"])
        corrected = [dict(mention) for mention in item["extracted_mentions"]]
        corrected[0]["text"] = "dry cough"
        reviewed = rad.post(f"{endpoint}{item['id']}/review/", {"decision": "confirmed", "confirmed_mentions": corrected}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(reviewed.status_code, 200, reviewed.content)
        saved = ClinicalHistoryEntry.objects.get(pk=item["id"])
        self.assertEqual(saved.original_text, source)
        self.assertEqual(saved.extracted_mentions[0]["text"], "cough")
        self.assertEqual(saved.confirmed_mentions[0]["text"], "dry cough")
        self.assertEqual(saved.status, ClinicalHistoryEntry.ReviewStatus.CONFIRMED)
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="clinical_history.extraction_created", object_id=item["id"]).exists())
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="clinical_history.extraction_reviewed", object_id=item["id"]).exists())

        desk, desk_token = self.login_client(self.receptionist)
        self.select_clinic(desk, desk_token, self.clinic_a.pk)
        self.assertEqual(desk.get(endpoint).status_code, 403)
        self.assertEqual(desk.post(endpoint, {"original_text": source}, format="json", HTTP_X_CSRFTOKEN=desk_token).status_code, 403)
        foreign, foreign_token = self.login_client(self.foreign_staff)
        self.select_clinic(foreign, foreign_token, self.clinic_b.pk)
        self.assertIn(foreign.get(endpoint).status_code, {403, 404})

    def test_ai_report_assistance_supports_chest_xray_text_evidence(self):
        rad, token = self.login_client(self.radiologist)
        self.select_clinic(rad, token, self.clinic_a.pk)
        exam = order_examination(clinic_id=self.clinic_a.pk, actor=self.radiologist, membership=self.rad_a, patient_id=self.patient_a.pk, modality="XR", procedure_name="XR chest")
        self.advance_to_acquisition(rad, token, exam.pk)
        report = rad.post("/api/v1/reports/", {"examination_id": str(exam.pk)}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(report.status_code, 201, report.content)
        response = rad.post(f"/api/v1/reports/{report.json()['data']['id']}/ai-assist/", {
            "observations": [{"anatomy": "lungs", "text": "No focal air-space opacity.", "polarity": "negative"}],
        }, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["data"]["verification"]["modality"], "CHEST_XRAY")

    def test_concurrent_draft_edits_and_signing_reject_stale_report_version(self):
        rad, token = self.login_client(self.radiologist)
        self.select_clinic(rad, token, self.clinic_a.pk)
        self.advance_to_acquisition(rad, token, self.exam_a.pk)
        created = rad.post("/api/v1/reports/", {"examination_id": str(self.exam_a.pk)}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(created.status_code, 201, created.content)
        report_id = created.json()["data"]["id"]
        opened_version = created.json()["data"]["updated_at"]

        first_editor = rad.patch(f"/api/v1/reports/{report_id}/", {
            "findings": "Synthetic first editor finding", "impression": "Synthetic first editor impression",
            "expected_updated_at": opened_version,
        }, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(first_editor.status_code, 200, first_editor.content)

        stale_editor = rad.patch(f"/api/v1/reports/{report_id}/", {
            "findings": "Synthetic stale overwrite", "impression": "Synthetic stale impression",
            "expected_updated_at": opened_version,
        }, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(stale_editor.status_code, 400)
        self.assertIn("changed since it was opened", str(stale_editor.content))
        current = Report.objects.get(pk=report_id)
        self.assertEqual(current.findings, "Synthetic first editor finding")

        stale_sign = rad.post(f"/api/v1/reports/{report_id}/sign/", {"expected_updated_at": opened_version}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(stale_sign.status_code, 400)
        self.assertEqual(Report.objects.get(pk=report_id).status, Report.Status.DRAFT)
        signed = rad.post(f"/api/v1/reports/{report_id}/sign/", {
            "expected_updated_at": first_editor.json()["data"]["updated_at"],
        }, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(signed.status_code, 200, signed.content)
        audit = AuditEvent.objects.get(clinic=self.clinic_a, action="report.signed", object_id=report_id)
        self.assertEqual(audit.actor_id, self.radiologist.pk)

    def test_report_version_unique_constraint_race_returns_retryable_validation(self):
        from clinics.domain import create_report_draft

        rad, token = self.login_client(self.radiologist)
        self.select_clinic(rad, token, self.clinic_a.pk)
        self.advance_to_acquisition(rad, token, self.exam_a.pk)
        with patch("clinics.domain.Report.save", side_effect=IntegrityError("simulated concurrent unique conflict")):
            with self.assertRaisesMessage(DjangoValidationError, "created concurrently"):
                create_report_draft(
                    examination_id=self.exam_a.pk, clinic_id=self.clinic_a.pk,
                    actor=self.radiologist, membership=self.rad_a,
                )

    def test_every_clinic_resource_uses_the_selected_tenant_scope(self):
        # This user belongs to both clinics; the active context must still scope each resource.
        for clinic, membership in ((self.clinic_a, self.owner_a), (self.clinic_b, self.owner_b)):
            for role in (MembershipRole.Role.RADIOLOGIST, MembershipRole.Role.TECHNICIAN):
                grant = MembershipRole(clinic=clinic, membership=membership, role=role)
                grant._domain_service_write = True
                grant.save()
        appointment = schedule_appointment(
            clinic_id=self.clinic_b.pk, actor=self.owner, membership=self.owner_b,
            patient_id=self.patient_b.pk, starts_at=timezone.now() + timedelta(days=1),
        )
        exam = order_examination(
            clinic_id=self.clinic_b.pk, actor=self.owner, membership=self.owner_b,
            patient_id=self.patient_b.pk, appointment_id=appointment.pk,
            modality="CT", procedure_name="CT chest",
        )
        transition_appointment(appointment_id=appointment.pk, clinic_id=self.clinic_b.pk, target=Appointment.Status.CHECKED_IN, actor=self.owner, membership=self.owner_b)
        transition_examination(examination_id=exam.pk, clinic_id=self.clinic_b.pk, target=Examination.Status.CHECKED_IN, actor=self.owner, membership=self.owner_b)
        transition_examination(examination_id=exam.pk, clinic_id=self.clinic_b.pk, target=Examination.Status.IN_PROGRESS, actor=self.owner, membership=self.owner_b)
        instance = ingest_uploads(
            clinic_id=self.clinic_b.pk, actor=self.owner, membership=self.owner_b,
            examination_id=exam.pk, files=[self.png_file("foreign.png")],
        )[0]["instance"]
        transition_examination(examination_id=exam.pk, clinic_id=self.clinic_b.pk, target=Examination.Status.ACQUIRED, actor=self.owner, membership=self.owner_b)
        report = create_report_draft(examination_id=exam.pk, clinic_id=self.clinic_b.pk, actor=self.owner, membership=self.owner_b)
        invoice = create_invoice(clinic_id=self.clinic_b.pk, actor=self.owner, membership=self.owner_b, patient_id=self.patient_b.pk, amount_minor=5000)
        payment = record_payment(invoice_id=invoice.pk, clinic_id=self.clinic_b.pk, amount_minor=500, currency="INR", method="cash", reference="b-ref", actor=self.owner, membership=self.owner_b)
        analysis = record_ai_analysis(
            clinic_id=self.clinic_b.pk, actor=self.owner, membership=self.owner_b,
            examination_id=exam.pk, imaging_study_id=instance.series.study_id,
            provider="test", model_name="test-model", model_version="1", task="triage",
            output={"label": "review"}, uncertainty={"score": 0.3},
            provenance={"study_id": str(instance.series.study_id)},
        )

        client, token = self.login_client(self.owner)
        self.select_clinic(client, token, self.clinic_a.pk)
        foreign_details = [
            f"/api/v1/patients/{self.patient_b.pk}/",
            f"/api/v1/appointments/{appointment.pk}/",
            f"/api/v1/examinations/{exam.pk}/",
            f"/api/v1/imaging-studies/{instance.series.study_id}/",
            f"/api/v1/imaging-series/{instance.series_id}/",
            f"/api/v1/imaging-instances/{instance.pk}/",
            f"/api/v1/imaging-instances/{instance.pk}/content/",
            f"/api/v1/reports/{report.pk}/",
            f"/api/v1/ai-analyses/{analysis.pk}/",
            f"/api/v1/invoices/{invoice.pk}/",
            f"/api/v1/payments/{payment.pk}/",
            f"/api/v1/memberships/{self.owner_b.pk}/",
        ]
        for url in foreign_details:
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 404, response.content)

        bad_appointment = client.post(
            "/api/v1/appointments/",
            {"patient_id": str(self.patient_b.pk), "starts_at": timezone.now().isoformat()},
            format="json", HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(bad_appointment.status_code, 404, bad_appointment.content)

    def test_raster_upload_validation_privacy_duplicates_and_secure_retrieval(self):
        desk, desk_token = self.login_client(self.receptionist)
        self.select_clinic(desk, desk_token, self.clinic_a.pk)
        rad, rad_token = self.login_client(self.radiologist)
        self.select_clinic(rad, rad_token, self.clinic_a.pk)
        desk.post(f"/api/v1/examinations/{self.exam_a.pk}/transition/", {"target": "checked_in"}, format="json", HTTP_X_CSRFTOKEN=desk_token)
        rad.post(f"/api/v1/examinations/{self.exam_a.pk}/transition/", {"target": "in_progress"}, format="json", HTTP_X_CSRFTOKEN=rad_token)
        bad = rad.post(f"/api/v1/examinations/{self.exam_a.pk}/imaging/", {"files": [SimpleUploadedFile("fake.jpg", b"not a real image", content_type="image/jpeg")]}, format="multipart", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.exam_a.imaging_studies.count(), 0)
        with override_settings(IMAGE_UPLOAD_MAX_BYTES=10):
            oversize = rad.post(f"/api/v1/examinations/{self.exam_a.pk}/imaging/", {"files": [self.png_file()]}, format="multipart", HTTP_X_CSRFTOKEN=rad_token)
            self.assertEqual(oversize.status_code, 400)

        image = self.png_file()
        image_bytes = image.read()
        image.seek(0)
        uploaded = rad.post(f"/api/v1/examinations/{self.exam_a.pk}/imaging/", {"files": [image, self.png_file("second.png", color=91), self.jpeg_file(color=70), self.jpeg_file("fourth.jpeg", color=180)]}, format="multipart", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(uploaded.status_code, 200, uploaded.content)
        record = uploaded.json()["data"][0]["instance"]
        instance = ImagingInstance.objects.get(pk=record["id"])
        self.assertEqual(instance.uploaded_by_id, self.radiologist.pk)
        self.assertEqual(instance.checksum_sha256, __import__("hashlib").sha256(image_bytes).hexdigest())
        self.assertTrue((__import__("pathlib").Path(self.private_root) / instance.storage_key).is_file())
        duplicate = rad.post(f"/api/v1/examinations/{self.exam_a.pk}/imaging/", {"files": [self.png_file()]}, format="multipart", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["data"][0]["duplicate"])
        self.assertEqual(ImagingInstance.objects.filter(series__study__examination=self.exam_a).count(), 4)
        self.assertEqual(ImagingInstance.objects.filter(series__study__examination=self.exam_a, media_type="image/jpeg").count(), 2)
        self.assertTrue(AuditEvent.objects.filter(action="imaging.duplicate_upload", object_id=str(instance.pk)).exists())

        content = rad.get(f"/api/v1/imaging-instances/{instance.pk}/content/")
        self.assertEqual(content.status_code, 200)
        self.assertEqual(b"".join(content.streaming_content), image_bytes)
        self.assertIn("private, no-store", content["Cache-Control"])
        receptionist, receptionist_token = self.login_client(self.receptionist)
        self.select_clinic(receptionist, receptionist_token, self.clinic_a.pk)
        self.assertEqual(receptionist.get(f"/api/v1/imaging-instances/{instance.pk}/content/").status_code, 403)
        self.assertEqual(receptionist.get("/api/v1/imaging-studies/").status_code, 403)
        technician, technician_token = self.login_client(self.technician)
        self.select_clinic(technician, technician_token, self.clinic_a.pk)
        self.assertEqual(technician.get("/api/v1/imaging-instances/").json()["data"]["count"], 0)
        self.assertEqual(technician.get(f"/api/v1/imaging-instances/{instance.pk}/content/").status_code, 404)

    def test_acquisition_cannot_complete_without_a_validated_file(self):
        desk, desk_token = self.login_client(self.receptionist)
        self.select_clinic(desk, desk_token, self.clinic_a.pk)
        rad, token = self.login_client(self.radiologist)
        self.select_clinic(rad, token, self.clinic_a.pk)
        desk.post(f"/api/v1/examinations/{self.exam_a.pk}/transition/", {"target": "checked_in"}, format="json", HTTP_X_CSRFTOKEN=desk_token)
        rad.post(f"/api/v1/examinations/{self.exam_a.pk}/transition/", {"target": "in_progress"}, format="json", HTTP_X_CSRFTOKEN=token)
        acquired = rad.post(f"/api/v1/examinations/{self.exam_a.pk}/transition/", {"target": "acquired"}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(acquired.status_code, 400)

    def test_dicom_files_group_into_study_series_and_instances(self):
        import pydicom
        from pydicom.dataset import FileDataset, FileMetaDataset
        from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

        desk, desk_token = self.login_client(self.receptionist)
        self.select_clinic(desk, desk_token, self.clinic_a.pk)
        rad, token = self.login_client(self.radiologist)
        self.select_clinic(rad, token, self.clinic_a.pk)
        desk.post(f"/api/v1/examinations/{self.exam_a.pk}/transition/", {"target": "checked_in"}, format="json", HTTP_X_CSRFTOKEN=desk_token)
        rad.post(f"/api/v1/examinations/{self.exam_a.pk}/transition/", {"target": "in_progress"}, format="json", HTTP_X_CSRFTOKEN=token)
        study_uid = generate_uid()

        def dicom(name, series_uid, number, patient_id="A-100"):
            sop_uid = generate_uid()
            meta = FileMetaDataset()
            meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
            meta.MediaStorageSOPInstanceUID = sop_uid
            meta.TransferSyntaxUID = ExplicitVRLittleEndian
            stream = BytesIO()
            dataset = FileDataset(name, {}, file_meta=meta, preamble=b"\0" * 128)
            dataset.SOPClassUID = SecondaryCaptureImageStorage
            dataset.SOPInstanceUID = sop_uid
            dataset.StudyInstanceUID = study_uid
            dataset.SeriesInstanceUID = series_uid
            dataset.PatientID = patient_id
            dataset.Modality = "CT"
            dataset.Rows = 2
            dataset.Columns = 2
            dataset.SamplesPerPixel = 1
            dataset.PhotometricInterpretation = "MONOCHROME2"
            dataset.BitsAllocated = 8
            dataset.BitsStored = 8
            dataset.HighBit = 7
            dataset.PixelRepresentation = 0
            dataset.InstanceNumber = number
            dataset.PixelData = bytes([number, number, number, number])
            pydicom.dcmwrite(stream, dataset, enforce_file_format=True)
            return SimpleUploadedFile(name, stream.getvalue(), content_type="application/dicom")

        series_one, series_two = generate_uid(), generate_uid()
        mismatch = rad.post(f"/api/v1/examinations/{self.exam_a.pk}/imaging/", {"files": [dicom("mismatch.dcm", series_one, 99, "OTHER-MRN")]}, format="multipart", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual(ImagingStudy.objects.filter(examination=self.exam_a).count(), 0)
        files = [dicom("one.dcm", series_one, 1), dicom("two.dcm", series_one, 2), dicom("three.dcm", series_two, 3)]
        file_bytes = [file.read() for file in files]
        files = [SimpleUploadedFile(f"image-{index}.dcm", body, content_type="application/dicom") for index, body in enumerate(file_bytes)]
        response = rad.post(f"/api/v1/examinations/{self.exam_a.pk}/imaging/", {"files": files}, format="multipart", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(ImagingStudy.objects.filter(examination=self.exam_a).count(), 1)
        study = ImagingStudy.objects.get(examination=self.exam_a)
        self.assertEqual(study.series.count(), 2)
        self.assertEqual(ImagingInstance.objects.filter(series__study=study).count(), 3)
        duplicate = rad.post(f"/api/v1/examinations/{self.exam_a.pk}/imaging/", {"files": [SimpleUploadedFile("retry.dcm", file_bytes[0], content_type="application/dicom")]}, format="multipart", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["data"][0]["duplicate"])
        self.assertEqual(study.study_instance_uid, study_uid)
        self.assertEqual(rad.get(f"/api/v1/dicomweb/studies/{study_uid}/series/").status_code, 200)
        self.assertEqual(rad.get(f"/api/v1/dicomweb/studies/{study_uid}/series/{series_one}/instances/").status_code, 200)
        instance = ImagingInstance.objects.filter(series__study=study).order_by("created_at").first()
        metadata = rad.get(f"/api/v1/dicomweb/studies/{study_uid}/series/{series_one}/instances/{instance.sop_instance_uid}/metadata/")
        self.assertEqual(metadata.status_code, 200, metadata.content)
        self.assertNotIn("PatientID", metadata.content.decode())
        retrieved = rad.get(f"/api/v1/dicomweb/studies/{study_uid}/series/{series_one}/instances/{instance.sop_instance_uid}/")
        self.assertEqual(retrieved.status_code, 200)
        self.assertIn(b"DICM", b"".join(retrieved.streaming_content))
        frames = rad.get(f"/api/v1/dicomweb/studies/{study_uid}/series/{series_one}/instances/{instance.sop_instance_uid}/frames/1/")
        self.assertEqual(frames.status_code, 200)
        self.assertIn(bytes([1, 1, 1, 1]), b"".join(frames.streaming_content))
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="imaging.upload_batch_completed", object_id=str(self.exam_a.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="imaging.instance_accessed", object_id=str(instance.pk)).exists())
        receptionist, receptionist_token = self.login_client(self.receptionist)
        self.select_clinic(receptionist, receptionist_token, self.clinic_a.pk)
        self.assertEqual(receptionist.get(f"/api/v1/dicomweb/studies/{study_uid}/series/").status_code, 403)
        self.assertEqual(receptionist.get(f"/api/v1/dicomweb/studies/{study_uid}/series/{series_one}/instances/{instance.sop_instance_uid}/").status_code, 403)
        self.add_membership(self.clinic_b, self.radiologist, [MembershipRole.Role.RADIOLOGIST])
        self.select_clinic(rad, token, self.clinic_b.pk)
        self.assertEqual(rad.get(f"/api/v1/dicomweb/studies/{study_uid}/series/").status_code, 404)

    def test_dicomweb_decodes_installed_compressed_syntax_and_rejects_unsupported(self):
        import pydicom
        from pydicom.dataset import FileDataset, FileMetaDataset
        from pydicom.encaps import encapsulate
        from pydicom.uid import (
            JPEGBaseline8Bit, JPEGLSLossless, SecondaryCaptureImageStorage,
            generate_uid,
        )

        desk, desk_token = self.login_client(self.receptionist)
        self.select_clinic(desk, desk_token, self.clinic_a.pk)
        rad, token = self.login_client(self.radiologist)
        self.select_clinic(rad, token, self.clinic_a.pk)
        desk.post(f"/api/v1/examinations/{self.exam_a.pk}/transition/", {"target": "checked_in"}, format="json", HTTP_X_CSRFTOKEN=desk_token)
        rad.post(f"/api/v1/examinations/{self.exam_a.pk}/transition/", {"target": "in_progress"}, format="json", HTTP_X_CSRFTOKEN=token)

        jpeg = BytesIO()
        Image.new("L", (2, 2), color=84).save(jpeg, format="JPEG")
        study_uid, series_uid = generate_uid(), generate_uid()

        def compressed_file(transfer_syntax, filename):
            sop_uid = generate_uid()
            meta = FileMetaDataset()
            meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
            meta.MediaStorageSOPInstanceUID = sop_uid
            meta.TransferSyntaxUID = transfer_syntax
            dataset = FileDataset(filename, {}, file_meta=meta, preamble=b"\0" * 128)
            dataset.SOPClassUID = SecondaryCaptureImageStorage
            dataset.SOPInstanceUID = sop_uid
            dataset.StudyInstanceUID = study_uid
            dataset.SeriesInstanceUID = series_uid
            dataset.PatientID = "A-100"
            dataset.Modality = "CT"
            dataset.Rows = dataset.Columns = 2
            dataset.SamplesPerPixel = 1
            dataset.PhotometricInterpretation = "MONOCHROME2"
            dataset.BitsAllocated = dataset.BitsStored = 8
            dataset.HighBit = 7
            dataset.PixelRepresentation = 0
            dataset.PixelData = encapsulate([jpeg.getvalue()])
            dataset[0x7FE00010].is_undefined_length = True
            stream = BytesIO()
            pydicom.dcmwrite(stream, dataset, enforce_file_format=True)
            return SimpleUploadedFile(filename, stream.getvalue(), content_type="application/dicom")

        upload = rad.post(
            f"/api/v1/examinations/{self.exam_a.pk}/imaging/",
            {"files": [compressed_file(JPEGBaseline8Bit, "jpeg-baseline.dcm")]},
            format="multipart", HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(upload.status_code, 200, upload.content)
        instance = ImagingInstance.objects.get(series__study__study_instance_uid=study_uid)
        frame = rad.get(f"/api/v1/dicomweb/studies/{study_uid}/series/{series_uid}/instances/{instance.sop_instance_uid}/frames/1/")
        self.assertEqual(frame.status_code, 200)
        self.assertIn(bytes([84]), b"".join(frame.streaming_content))

        unsupported = rad.post(
            f"/api/v1/examinations/{self.exam_a.pk}/imaging/",
            {"files": [compressed_file(JPEGLSLossless, "jpeg-ls.dcm")]},
            format="multipart", HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(unsupported.status_code, 200, unsupported.content)
        unsupported_instance = ImagingInstance.objects.get(series__study__study_instance_uid=study_uid, metadata__transfer_syntax_uid=str(JPEGLSLossless))
        unavailable = rad.get(f"/api/v1/dicomweb/studies/{study_uid}/series/{series_uid}/instances/{unsupported_instance.sop_instance_uid}/frames/1/")
        self.assertEqual(unavailable.status_code, 406)

    def test_failed_database_registration_removes_promoted_files_and_staged_uploads(self):
        rad, token = self.login_client(self.radiologist)
        self.select_clinic(rad, token, self.clinic_a.pk)
        desk, desk_token = self.login_client(self.receptionist)
        self.select_clinic(desk, desk_token, self.clinic_a.pk)
        desk.post(f"/api/v1/examinations/{self.exam_a.pk}/transition/", {"target": "checked_in"}, format="json", HTTP_X_CSRFTOKEN=desk_token)
        rad.post(f"/api/v1/examinations/{self.exam_a.pk}/transition/", {"target": "in_progress"}, format="json", HTTP_X_CSRFTOKEN=token)
        with patch("clinics.imaging.register_imaging_instance", side_effect=RuntimeError("database write failed")):
            rad.raise_request_exception = False
            response = rad.post(f"/api/v1/examinations/{self.exam_a.pk}/imaging/", {"files": [self.png_file()]}, format="multipart", HTTP_X_CSRFTOKEN=token)
            rad.raise_request_exception = True
        self.assertEqual(response.status_code, 500)
        self.assertEqual(ImagingInstance.objects.filter(series__study__examination=self.exam_a).count(), 0)
        self.assertEqual(list((__import__("pathlib").Path(self.private_root) / "images").glob("*")) if (__import__("pathlib").Path(self.private_root) / "images").exists() else [], [])
        self.assertEqual(list((__import__("pathlib").Path(self.private_root) / ".staging").glob("upload-*.part")), [])

    def test_receptionist_cannot_access_or_edit_clinical_resources(self):
        client, token = self.login_client(self.receptionist)
        self.select_clinic(client, token, self.clinic_a.pk)
        exams = client.get("/api/v1/examinations/")
        self.assertEqual(exams.status_code, 200)
        receptionist_exam = exams.json()["data"]["results"][0]
        self.assertNotIn("clinical_indication", receptionist_exam)
        self.assertNotIn("procedure_name", receptionist_exam)
        create_exam = client.post(
            "/api/v1/examinations/",
            {"patient_id": str(self.patient_a.pk), "modality": "CT", "procedure_name": "CT chest"},
            format="json", HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(create_exam.status_code, 403)
        invalid_transition = client.post(
            f"/api/v1/examinations/{self.exam_a.pk}/transition/",
            {"target": Examination.Status.RELEASED}, format="json", HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(invalid_transition.status_code, 403)

    def test_technician_cannot_sign_or_release_and_radiologist_can_acquire_and_interpret(self):
        desk_client, desk_token = self.login_client(self.receptionist)
        self.select_clinic(desk_client, desk_token, self.clinic_a.pk)
        checked_in = desk_client.post(
            f"/api/v1/examinations/{self.exam_a.pk}/transition/",
            {"target": Examination.Status.CHECKED_IN}, format="json", HTTP_X_CSRFTOKEN=desk_token,
        )
        self.assertEqual(checked_in.status_code, 200, checked_in.content)

        rad_client, rad_token = self.login_client(self.radiologist)
        self.select_clinic(rad_client, rad_token, self.clinic_a.pk)
        self.assertEqual(len(rad_client.get("/api/v1/auth/me/").json()["data"]["memberships"][0]["roles"]), 2)
        advanced = rad_client.post(f"/api/v1/examinations/{self.exam_a.pk}/transition/", {"target": Examination.Status.IN_PROGRESS}, format="json", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(advanced.status_code, 200, advanced.content)
        uploaded = rad_client.post(f"/api/v1/examinations/{self.exam_a.pk}/imaging/", {"files": [self.png_file()]}, format="multipart", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(uploaded.status_code, 200, uploaded.content)
        advanced = rad_client.post(f"/api/v1/examinations/{self.exam_a.pk}/transition/", {"target": Examination.Status.ACQUIRED}, format="json", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(advanced.status_code, 200, advanced.content)

        draft = rad_client.post(
            "/api/v1/reports/", {"examination_id": str(self.exam_a.pk)},
            format="json", HTTP_X_CSRFTOKEN=rad_token,
        )
        self.assertEqual(draft.status_code, 201, draft.content)
        report_id = draft.json()["data"]["id"]

        tech_client, tech_token = self.login_client(self.technician)
        self.select_clinic(tech_client, tech_token, self.clinic_a.pk)
        self.assertEqual(tech_client.post(f"/api/v1/reports/{report_id}/sign/", {}, format="json", HTTP_X_CSRFTOKEN=tech_token).status_code, 403)
        self.assertEqual(tech_client.post(f"/api/v1/reports/{report_id}/release/", {}, format="json", HTTP_X_CSRFTOKEN=tech_token).status_code, 403)

        edited = rad_client.patch(
            f"/api/v1/reports/{report_id}/",
            {"findings": "No acute abnormality.", "impression": "No acute finding.", "expected_updated_at": draft.json()["data"]["updated_at"]},
            format="json", HTTP_X_CSRFTOKEN=rad_token,
        )
        self.assertEqual(edited.status_code, 200, edited.content)
        signed = rad_client.post(f"/api/v1/reports/{report_id}/sign/", {"expected_updated_at": edited.json()["data"]["updated_at"]}, format="json", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(signed.status_code, 200, signed.content)
        released = rad_client.post(f"/api/v1/reports/{report_id}/release/", {}, format="json", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(released.status_code, 200, released.content)

    def test_billing_catalog_server_totals_split_payments_refunds_documents_and_permissions(self):
        owner, owner_token = self.login_client(self.owner)
        self.select_clinic(owner, owner_token, self.clinic_a.pk)
        receptionist, receptionist_token = self.login_client(self.receptionist)
        self.select_clinic(receptionist, receptionist_token, self.clinic_a.pk)
        forbidden_catalog = receptionist.post("/api/v1/charge-items/", {
            "code": "CT-1", "name": "CT scan", "category": "ct", "unit_price_minor": 10000,
        }, format="json", HTTP_X_CSRFTOKEN=receptionist_token)
        self.assertEqual(forbidden_catalog.status_code, 403)

        ct = owner.post("/api/v1/charge-items/", {
            "code": "CT-1", "name": "CT scan", "category": "ct", "unit_price_minor": 10000,
            "currency": "INR", "tax_rate_bps": 1000,
        }, format="json", HTTP_X_CSRFTOKEN=owner_token)
        consultation = owner.post("/api/v1/charge-items/", {
            "code": "CONSULT", "name": "Consultation", "category": "consultation", "unit_price_minor": 5000,
            "currency": "INR", "tax_rate_bps": 0,
        }, format="json", HTTP_X_CSRFTOKEN=owner_token)
        self.assertEqual(ct.status_code, 201, ct.content)
        self.assertEqual(consultation.status_code, 201, consultation.content)
        created = owner.post("/api/v1/invoices/", {
            "patient_id": str(self.patient_a.pk), "examination_id": str(self.exam_a.pk),
            "line_items": [
                {"charge_item_id": ct.json()["data"]["id"], "quantity": 2},
                {"charge_item_id": consultation.json()["data"]["id"], "quantity": 1},
            ], "discount_minor": 1000,
        }, format="json", HTTP_X_CSRFTOKEN=owner_token)
        self.assertEqual(created.status_code, 201, created.content)
        invoice_data = created.json()["data"]
        self.assertRegex(invoice_data["invoice_number"], r"^[A-F0-9]{8}-\d{4}-\d{6}$")
        self.assertEqual(invoice_data["gross_amount_minor"], 25000)
        self.assertEqual(invoice_data["discount_minor"], 1000)
        self.assertEqual(invoice_data["tax_minor"], 1920)
        self.assertEqual(invoice_data["amount_minor"], 25920)
        self.assertEqual(invoice_data["status"], "issued")
        self.assertEqual(len(invoice_data["line_items"]), 2)
        protected_invoice = Invoice.objects.get(pk=invoice_data["id"])
        protected_invoice.amount_minor += 1
        with self.assertRaises(DjangoValidationError):
            protected_invoice.save()
        with self.assertRaises(DjangoValidationError):
            Invoice.objects.filter(pk=invoice_data["id"]).update(amount_minor=1)
        protected_line = Invoice.objects.get(pk=invoice_data["id"]).line_items.first()
        protected_line.description = "Changed after issue"
        with self.assertRaises(DjangoValidationError):
            protected_line.save()
        with self.assertRaises(DjangoValidationError):
            Invoice.objects.get(pk=invoice_data["id"]).line_items.all().delete()

        # Later catalogue changes do not rewrite the historical price snapshot.
        price_update = owner.patch(f"/api/v1/charge-items/{ct.json()['data']['id']}/", {"unit_price_minor": 15000}, format="json", HTTP_X_CSRFTOKEN=owner_token)
        self.assertEqual(price_update.status_code, 200, price_update.content)
        invoice_id = invoice_data["id"]
        self.assertEqual(Invoice.objects.get(pk=invoice_id).line_items.get(charge_item_id=ct.json()["data"]["id"]).unit_price_minor, 10000)

        payment1 = receptionist.post(f"/api/v1/invoices/{invoice_id}/payments/", {
            "amount_minor": 10000, "method": "cash", "reference": "cash-1", "idempotency_key": "00000000-0000-4000-8000-000000000001",
        }, format="json", HTTP_X_CSRFTOKEN=receptionist_token)
        self.assertEqual(payment1.status_code, 201, payment1.content)
        self.assertEqual(payment1.json()["data"]["currency"], "INR")
        retry = receptionist.post(f"/api/v1/invoices/{invoice_id}/payments/", {
            "amount_minor": 10000, "method": "cash", "reference": "cash-1", "idempotency_key": "00000000-0000-4000-8000-000000000001",
        }, format="json", HTTP_X_CSRFTOKEN=receptionist_token)
        self.assertEqual(retry.status_code, 201, retry.content)
        self.assertEqual(retry.json()["data"]["id"], payment1.json()["data"]["id"])
        key_conflict = receptionist.post(f"/api/v1/invoices/{invoice_id}/payments/", {
            "amount_minor": 10000, "method": "cash", "reference": "different", "idempotency_key": "00000000-0000-4000-8000-000000000001",
        }, format="json", HTTP_X_CSRFTOKEN=receptionist_token)
        self.assertEqual(key_conflict.status_code, 400)
        self.assertEqual(Invoice.objects.get(pk=invoice_id).status, Invoice.Status.PARTIALLY_PAID)

        upi_unconfirmed = receptionist.post(f"/api/v1/invoices/{invoice_id}/payments/", {
            "amount_minor": 5000, "method": "upi", "reference": "UPI-REFERENCE-ONLY", "idempotency_key": "00000000-0000-4000-8000-000000000002",
        }, format="json", HTTP_X_CSRFTOKEN=receptionist_token)
        self.assertEqual(upi_unconfirmed.status_code, 400)
        upi = receptionist.post(f"/api/v1/invoices/{invoice_id}/payments/", {
            "amount_minor": 5000, "method": "upi", "reference": "UPI-RECEIVED", "confirmed_received": True, "idempotency_key": "00000000-0000-4000-8000-000000000003",
        }, format="json", HTTP_X_CSRFTOKEN=receptionist_token)
        self.assertEqual(upi.status_code, 201, upi.content)
        self.assertTrue(upi.json()["data"]["confirmed_received"])
        overpay = receptionist.post(f"/api/v1/invoices/{invoice_id}/payments/", {
            "amount_minor": 11000, "method": "card", "idempotency_key": "00000000-0000-4000-8000-000000000004",
        }, format="json", HTTP_X_CSRFTOKEN=receptionist_token)
        self.assertEqual(overpay.status_code, 400)
        bank = receptionist.post(f"/api/v1/invoices/{invoice_id}/payments/", {
            "amount_minor": 10920, "method": "bank_transfer", "reference": "BANK-RECEIVED", "idempotency_key": "00000000-0000-4000-8000-000000000005",
        }, format="json", HTTP_X_CSRFTOKEN=receptionist_token)
        self.assertEqual(bank.status_code, 201, bank.content)
        self.assertEqual(Invoice.objects.get(pk=invoice_id).status, Invoice.Status.PAID)

        usd = owner.post("/api/v1/charge-items/", {
            "code": "US-SVC", "name": "Synthetic USD service", "category": "other", "unit_price_minor": 900,
            "currency": "USD", "tax_rate_bps": 0,
        }, format="json", HTTP_X_CSRFTOKEN=owner_token)
        self.assertEqual(usd.status_code, 201, usd.content)
        usd_invoice = owner.post("/api/v1/invoices/", {
            "patient_id": str(self.patient_a.pk), "line_items": [{"charge_item_id": usd.json()["data"]["id"], "quantity": 1}],
        }, format="json", HTTP_X_CSRFTOKEN=owner_token)
        self.assertEqual(usd_invoice.status_code, 201, usd_invoice.content)

        denied_refund = receptionist.post(f"/api/v1/payments/{payment1.json()['data']['id']}/refunds/", {
            "amount_minor": 2000, "reason": "Synthetic adjustment",
        }, format="json", HTTP_X_CSRFTOKEN=receptionist_token)
        self.assertEqual(denied_refund.status_code, 403)
        refund1 = owner.post(f"/api/v1/payments/{payment1.json()['data']['id']}/refunds/", {
            "amount_minor": 2000, "reason": "Synthetic partial refund",
        }, format="json", HTTP_X_CSRFTOKEN=owner_token)
        self.assertEqual(refund1.status_code, 201, refund1.content)
        self.assertEqual(Invoice.objects.get(pk=invoice_id).status, Invoice.Status.PARTIALLY_PAID)
        self.assertEqual(Invoice.objects.get(pk=invoice_id).balance_minor, 2000)
        self.assertTrue(Refund.objects.filter(pk=refund1.json()["data"]["id"], recorded_by=self.owner).exists())
        self.assertTrue(AuditEvent.objects.filter(action="payment.refunded", actor=self.owner).exists())
        excessive_refund = owner.post(f"/api/v1/payments/{payment1.json()['data']['id']}/refunds/", {
            "amount_minor": 8001, "reason": "Too much",
        }, format="json", HTTP_X_CSRFTOKEN=owner_token)
        self.assertEqual(excessive_refund.status_code, 400)

        invoice_pdf = receptionist.get(f"/api/v1/invoices/{invoice_id}/pdf/")
        receipt_pdf = receptionist.get(f"/api/v1/payments/{payment1.json()['data']['id']}/receipt/")
        refund_pdf = receptionist.get(f"/api/v1/refunds/{refund1.json()['data']['id']}/receipt/")
        for response in (invoice_pdf, receipt_pdf, refund_pdf):
            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(response.content.startswith(b"%PDF"))
            self.assertIn("private, no-store", response["Cache-Control"])
        self.assertGreaterEqual(AuditEvent.objects.filter(action="billing.document_exported", actor=self.receptionist).count(), 3)
        self.assertTrue(AuditEvent.objects.filter(action="payment.recorded", actor=self.receptionist).exists())
        self.assertTrue(AuditEvent.objects.filter(action="invoice.issued", actor=self.owner).exists())

        detail = receptionist.get(f"/api/v1/invoices/{invoice_id}/")
        self.assertEqual(detail.json()["data"]["balance_minor"], 2000)
        self.assertEqual(len(receptionist.get(f"/api/v1/invoices/{invoice_id}/payments/").json()["data"]), 3)
        summary = receptionist.get(f"/api/v1/billing/summary/?date={timezone.localdate().isoformat()}&patient_id={self.patient_a.pk}")
        self.assertEqual(summary.status_code, 200, summary.content)
        self.assertIsNone(summary.json()["data"]["outstanding_minor"])
        self.assertEqual({r["currency"]: r["amount_minor"] for r in summary.json()["data"]["outstanding_by_currency"]}, {"INR": 2000, "USD": 900})
        self.assertIsNone(summary.json()["data"]["patient_outstanding_minor"])
        self.assertEqual({r["currency"]: r["amount_minor"] for r in summary.json()["data"]["patient_outstanding_by_currency"]}, {"INR": 2000, "USD": 900})
        self.assertEqual(summary.json()["data"]["collections_minor"], 23920)
        self.assertEqual({row["method"]: row["amount_minor"] for row in summary.json()["data"]["payment_methods"]}["cash"], 8000)
        self.assertEqual(summary.json()["data"]["collections_minor"], 23920)
        self.assertEqual({row["method"]: row["amount_minor"] for row in summary.json()["data"]["payment_methods"]}["cash"], 8000)
        for payment_data, remainder in ((payment1.json()["data"], 8000), (upi.json()["data"], 5000), (bank.json()["data"], 10920)):
            refunded = owner.post(f"/api/v1/payments/{payment_data['id']}/refunds/", {
                "amount_minor": remainder, "reason": "Synthetic full repayment",
            }, format="json", HTTP_X_CSRFTOKEN=owner_token)
            self.assertEqual(refunded.status_code, 201, refunded.content)
        refunded_invoice = Invoice.objects.get(pk=invoice_id)
        self.assertEqual(refunded_invoice.status, Invoice.Status.REFUNDED)
        self.assertEqual(refunded_invoice.balance_minor, 0)
        self.assertEqual(Payment.objects.filter(invoice_id=invoice_id).count(), 3)
        self.assertTrue(AuditEvent.objects.filter(action="payment.reversed", actor=self.owner).exists())
        after_refunds = receptionist.get(f"/api/v1/billing/summary/?date={timezone.localdate().isoformat()}")
        self.assertEqual(after_refunds.json()["data"]["collections_by_currency"], [{"currency": "INR", "amount_minor": 0}])

        cancellable = owner.post("/api/v1/invoices/", {
            "patient_id": str(self.patient_a.pk),
            "line_items": [{"charge_item_id": ct.json()["data"]["id"], "quantity": 1}],
        }, format="json", HTTP_X_CSRFTOKEN=owner_token)
        self.assertEqual(cancellable.status_code, 201, cancellable.content)
        cancel_url = f"/api/v1/invoices/{cancellable.json()['data']['id']}/cancel/"
        self.assertEqual(receptionist.post(cancel_url, {"reason": "Synthetic correction"}, format="json", HTTP_X_CSRFTOKEN=receptionist_token).status_code, 403)
        cancelled = owner.post(cancel_url, {"reason": "Synthetic correction"}, format="json", HTTP_X_CSRFTOKEN=owner_token)
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        self.assertEqual(cancelled.json()["data"]["status"], "cancelled")
        self.assertTrue(Invoice.objects.filter(pk=cancellable.json()["data"]["id"], status=Invoice.Status.CANCELLED).exists())
        self.assertTrue(AuditEvent.objects.filter(action="invoice.cancelled", actor=self.owner, object_id=cancellable.json()["data"]["id"]).exists())

        foreign, foreign_token = self.login_client(self.foreign_staff)
        self.select_clinic(foreign, foreign_token, self.clinic_b.pk)
        self.assertEqual(foreign.get(f"/api/v1/invoices/{invoice_id}/pdf/").status_code, 404)
        self.assertEqual(foreign.get(f"/api/v1/payments/{payment1.json()['data']['id']}/receipt/").status_code, 404)

    def test_billing_role_matrix_and_invoice_related_object_tenant_validation(self):
        owner, owner_token = self.login_client(self.owner)
        self.select_clinic(owner, owner_token, self.clinic_a.pk)
        catalogue = owner.post("/api/v1/charge-items/", {
            "code": "XR-1", "name": "Synthetic X-ray", "category": "xray", "unit_price_minor": 1200,
        }, format="json", HTTP_X_CSRFTOKEN=owner_token)
        self.assertEqual(catalogue.status_code, 201, catalogue.content)

        foreign_patient = owner.post("/api/v1/invoices/", {
            "patient_id": str(self.patient_b.pk),
            "line_items": [{"charge_item_id": catalogue.json()["data"]["id"], "quantity": 1}],
        }, format="json", HTTP_X_CSRFTOKEN=owner_token)
        self.assertEqual(foreign_patient.status_code, 404)
        foreign_rad_grant = MembershipRole(clinic=self.clinic_b, membership=self.foreign_membership, role=MembershipRole.Role.RADIOLOGIST)
        foreign_rad_grant._domain_service_write = True
        foreign_rad_grant.save()
        foreign_exam = order_examination(
            clinic_id=self.clinic_b.pk, actor=self.foreign_staff, membership=self.foreign_membership,
            patient_id=self.patient_b.pk, modality="XR", procedure_name="Synthetic chest X-ray",
        )
        mismatched_exam = owner.post("/api/v1/invoices/", {
            "patient_id": str(self.patient_a.pk), "examination_id": str(foreign_exam.pk),
            "line_items": [{"charge_item_id": catalogue.json()["data"]["id"], "quantity": 1}],
        }, format="json", HTTP_X_CSRFTOKEN=owner_token)
        self.assertEqual(mismatched_exam.status_code, 404)

        admin_user = get_user_model().objects.create_user(username="billing-admin", password="Safe-test-password-1")
        self.add_membership(self.clinic_a, admin_user, [MembershipRole.Role.ADMIN])
        test_invoice = create_invoice(
            clinic_id=self.clinic_a.pk, actor=self.owner, membership=self.owner_a,
            patient_id=self.patient_a.pk, amount_minor=5000,
        )
        role_users = (
            ("receptionist", self.receptionist, True),
            ("administrator", admin_user, True),
            ("radiologist", self.radiologist, False),
            ("technician", self.technician, False),
        )
        # There is no separate DOCTOR role in MembershipRole.Role; radiologists are the supported interpreting-clinician role.
        for label, user, allowed in role_users:
            client, token = self.login_client(user)
            self.select_clinic(client, token, self.clinic_a.pk)
            with self.subTest(role=label):
                expected = 200 if allowed else 403
                self.assertEqual(client.get("/api/v1/invoices/").status_code, expected)
                self.assertEqual(client.get("/api/v1/charge-items/").status_code, expected)
                if not allowed:
                    payment_attempt = client.post(
                        f"/api/v1/invoices/{test_invoice.pk}/payments/",
                        {"amount_minor": 100, "method": "cash", "idempotency_key": "00000000-0000-4000-8000-000000000099"},
                        format="json", HTTP_X_CSRFTOKEN=token,
                    )
                    self.assertEqual(payment_attempt.status_code, 403)

        # A dual-role radiologist/technician still receives no billing capability from either clinical role.
        self.assertEqual(set(self.rad_a.roles()), {MembershipRole.Role.RADIOLOGIST, MembershipRole.Role.TECHNICIAN})

    def test_logout_requires_csrf_and_records_authentication_event(self):
        client, token = self.login_client(self.receptionist)
        self.assertTrue(AuthenticationEvent.objects.filter(action=AuthenticationEvent.Action.LOGIN_SUCCEEDED, user=self.receptionist).exists())
        self.select_clinic(client, token, self.clinic_a.pk)
        blocked = client.post("/api/v1/auth/logout/", {}, format="json")
        self.assertEqual(blocked.status_code, 403)
        logged_out = client.post("/api/v1/auth/logout/", {}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(logged_out.status_code, 200)
        self.assertTrue(logged_out.json()["data"]["logged_out"])
        self.assertTrue(AuthenticationEvent.objects.filter(action=AuthenticationEvent.Action.LOGOUT, user=self.receptionist).exists())
        self.assertEqual(client.get("/api/v1/auth/me/").status_code, 403)

    def test_unknown_api_routes_use_standard_error_envelope(self):
        response = APIClient().get("/api/v1/not-a-resource/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": {"code": "not_found", "message": "API route not found."}})

    def test_settings_profile_catalogues_and_audit_are_tenant_scoped_and_audited(self):
        owner, token = self.login_client(self.owner)
        self.select_clinic(owner, token, self.clinic_a.pk)
        profile = owner.patch("/api/v1/clinics/profile/", {"name": "North Imaging Center", "default_currency": "USD", "invoice_prefix": "NORTH"}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(profile.status_code, 200, profile.content)
        self.assertEqual(profile.json()["data"]["default_currency"], "USD")
        charge = owner.post("/api/v1/charge-items/", {"code": "XR", "name": "X-ray", "category": "xray", "unit_price_minor": 1200}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(charge.status_code, 201, charge.content)
        self.assertEqual(charge.json()["data"]["currency"], "USD")
        issued = owner.post("/api/v1/invoices/", {"patient_id": str(self.patient_a.pk), "line_items": [{"charge_item_id": charge.json()["data"]["id"], "quantity": 1}]}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(issued.status_code, 201, issued.content)
        self.assertTrue(issued.json()["data"]["invoice_number"].startswith("NORTH-"))
        service = owner.post("/api/v1/clinical-services/", {"code": "CXR", "name": "Chest radiograph", "modality": "DX", "linked_charge_id": charge.json()["data"]["id"]}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(service.status_code, 201, service.content)
        self.assertEqual(service.json()["data"]["linked_charge_id"], charge.json()["data"]["id"])
        radiologist, rad_token = self.login_client(self.radiologist)
        self.select_clinic(radiologist, rad_token, self.clinic_a.pk)
        exam = radiologist.post("/api/v1/examinations/", {"patient_id": str(self.patient_a.pk), "clinical_service_id": service.json()["data"]["id"]}, format="json", HTTP_X_CSRFTOKEN=rad_token)
        self.assertEqual(exam.status_code, 201, exam.content)
        self.assertEqual(exam.json()["data"]["procedure_name"], "Chest radiograph")
        self.assertEqual(Examination.objects.get(pk=exam.json()["data"]["id"]).service_code_snapshot, "CXR")
        audit = owner.get("/api/v1/audit-events/?action=clinic.profile_updated")
        self.assertEqual(audit.status_code, 200)
        self.assertGreaterEqual(audit.json()["data"]["count"], 1)
        self.select_clinic(owner, token, self.clinic_b.pk)
        self.assertEqual(owner.get(f"/api/v1/clinical-services/{service.json()['data']['id']}/").status_code, 404)

    def test_administrative_roles_cannot_transfer_ownership_and_owner_transfer_is_audited(self):
        admin_user = get_user_model().objects.create_user(username="settings-admin", password="Safe-test-password-1")
        self.add_membership(self.clinic_a, admin_user, [MembershipRole.Role.ADMIN])
        admin, admin_token = self.login_client(admin_user)
        self.select_clinic(admin, admin_token, self.clinic_a.pk)
        create = admin.post("/api/v1/memberships/", {"user_id": self.new_staff.pk, "roles": ["receptionist"]}, format="json", HTTP_X_CSRFTOKEN=admin_token)
        self.assertEqual(create.status_code, 201)
        denied = admin.post(f"/api/v1/memberships/{create.json()['data']['id']}/transfer-ownership/", {}, format="json", HTTP_X_CSRFTOKEN=admin_token)
        self.assertEqual(denied.status_code, 403)
        owner, token = self.login_client(self.owner)
        self.select_clinic(owner, token, self.clinic_a.pk)
        transferred = owner.post(f"/api/v1/memberships/{create.json()['data']['id']}/transfer-ownership/", {}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(transferred.status_code, 200, transferred.content)
        self.assertFalse(self.owner_a.has_role(MembershipRole.Role.OWNER))
        self.assertTrue(Membership.objects.get(pk=create.json()["data"]["id"]).has_role(MembershipRole.Role.OWNER))
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="membership.ownership_transferred", actor=self.owner).exists())

    def test_staff_provisioning_is_active_clinic_scoped_audited_and_secret_is_one_time(self):
        owner, token = self.login_client(self.owner)
        self.select_clinic(owner, token, self.clinic_a.pk)
        response = owner.post("/api/v1/memberships/provision/", {
            "full_name": "Asha Rao", "username": "asha-rao", "email": "asha@example.invalid",
            "roles": ["receptionist", "technician"], "clinic_id": str(self.clinic_b.pk),
        }, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()["data"]
        membership = Membership.objects.get(pk=body["membership"]["id"])
        user = membership.user
        self.assertEqual(membership.clinic_id, self.clinic_a.pk)
        self.assertEqual(set(membership.roles()), {"receptionist", "technician"})
        self.assertEqual(user.get_full_name(), "Asha Rao")
        self.assertTrue(user.check_password(body["temporary_password"]))
        self.assertTrue(AccountSecurityState.objects.get(user=user).must_change_password)
        self.assertNotIn("temporary_password", owner.get("/api/v1/memberships/").content.decode())
        self.assertNotIn(user.password, owner.get("/api/v1/memberships/").content.decode())
        event = AuditEvent.objects.get(clinic=self.clinic_a, action="staff.account_created", object_id=str(membership.pk))
        serialized_audit = str(event.details)
        self.assertNotIn(body["temporary_password"], serialized_audit)
        self.assertNotIn(user.password, serialized_audit)
        self.assertTrue(AuditEvent.objects.filter(clinic=self.clinic_a, action="membership.created", object_id=str(membership.pk)).exists())

    def test_first_login_requires_password_change_without_forcing_existing_owner(self):
        owner, owner_token = self.login_client(self.owner)
        self.assertFalse(owner.get("/api/v1/auth/me/").json()["data"]["must_change_password"])
        provision, token = self.login_client(self.owner)
        self.select_clinic(provision, token, self.clinic_a.pk)
        created = provision.post("/api/v1/memberships/provision/", {
            "full_name": "Nina Test", "username": "nina-test", "email": "nina@example.invalid", "roles": ["receptionist"],
        }, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(created.status_code, 201, created.content)
        password = created.json()["data"]["temporary_password"]
        staff_user = Membership.objects.get(pk=created.json()["data"]["membership"]["id"]).user
        staff = APIClient(enforce_csrf_checks=True)
        csrf = self.csrf(staff)
        login_response = staff.post("/api/v1/auth/login/", {"username": staff_user.username, "password": password}, format="json", HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(login_response.status_code, 200, login_response.content)
        self.assertTrue(login_response.json()["data"]["must_change_password"])
        csrf = login_response.json()["data"]["csrf_token"]
        me = staff.get("/api/v1/auth/me/")
        self.assertTrue(me.json()["data"]["must_change_password"])
        self.assertEqual(staff.get("/api/v1/clinics/").status_code, 403)
        self.assertEqual(staff.get("/api/v1/memberships/").json()["error"]["code"], "password_change_required")
        changed = staff.post("/api/v1/auth/password/change/", {
            "current_password": password, "new_password": "Newly-chosen-strong-password-42!",
        }, format="json", HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(changed.status_code, 200, changed.content)
        self.assertFalse(staff.get("/api/v1/auth/me/").json()["data"]["must_change_password"])
        self.assertEqual(staff.post("/api/v1/auth/password/change/", {
            "current_password": password, "new_password": "Another-new-password-55!",
        }, format="json", HTTP_X_CSRFTOKEN=changed.json()["data"]["csrf_token"]).status_code, 400)
        self.assertEqual(owner.get("/api/v1/clinics/").status_code, 200)
        self.assertTrue(AuthenticationEvent.objects.filter(user=staff_user, action="password_changed").exists())

    def test_staff_provisioning_rejects_non_admin_and_duplicate_username_and_rolls_back(self):
        desk, desk_token = self.login_client(self.receptionist)
        self.select_clinic(desk, desk_token, self.clinic_a.pk)
        payload = {"full_name": "No Access", "username": "no-access", "email": "no@example.invalid", "roles": ["receptionist"]}
        self.assertEqual(desk.post("/api/v1/memberships/provision/", payload, format="json", HTTP_X_CSRFTOKEN=desk_token).status_code, 403)
        owner, token = self.login_client(self.owner)
        self.select_clinic(owner, token, self.clinic_a.pk)
        payload["username"] = self.receptionist.username
        duplicate = owner.post("/api/v1/memberships/provision/", payload, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(duplicate.status_code, 400)
        payload["username"] = "rollback-staff"
        with patch("clinics.domain.create_membership", side_effect=DjangoValidationError("simulated membership failure")):
            failed = owner.post("/api/v1/memberships/provision/", payload, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(failed.status_code, 400, failed.content)
        self.assertFalse(get_user_model().objects.filter(username="rollback-staff").exists())

    def test_duplicate_email_follows_default_django_user_model_constraint(self):
        owner, token = self.login_client(self.owner)
        self.select_clinic(owner, token, self.clinic_a.pk)
        for username in ("mail-one", "mail-two"):
            response = owner.post("/api/v1/memberships/provision/", {
                "full_name": username, "username": username, "email": "shared@example.invalid", "roles": ["technician"],
            }, format="json", HTTP_X_CSRFTOKEN=token)
            self.assertEqual(response.status_code, 201, response.content)

