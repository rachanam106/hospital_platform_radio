# Radiology workflow and data boundaries

Core clinic and imaging workflows are exposed below `/api/v1/` with session authentication and CSRF protection. Users first select an active clinic; every tenant endpoint re-resolves their current membership and capability. The clinic identifier is held in the server-side session after validation. Only documented service actions are writable; state fields are never accepted as ordinary model updates. See [API docs](API.md) for route and permission details.

## Appointment lifecycle

Appointment states are distinct from examination states:

```text
scheduled → checked_in
scheduled → cancelled
scheduled → no_show
```

Only the appointment service may change state. A receptionist or clinic owner/admin can check in, cancel, or mark no-show through capability checks. The current model disallows invalid status changes outside that service. Rescheduling is available for scheduled appointments through the API action endpoint.

## Examination lifecycle

```text
ordered → checked_in → in_progress → acquired → reporting → reported → released → delivered
    └──────────────→ cancelled       └── amendment reopens reporting from a released/reported state
```

The implemented transition service rejects transitions that are not in its allowlist and checks the matching capability. An exam linked to an appointment can be checked in only after its appointment is checked in. Walk-in exams without appointments can be checked in directly. Technician is optional: a user with a radiologist role can both acquire and interpret. A technician can acquire/upload but cannot interpret, sign, or release a report.

New examinations are initially assigned to the ordering radiologist. A clinic administrator or radiologist can assign/reassign an active clinic technician (optional) and radiologist. Every change records old and new staff IDs in the audit log. Technicians only see assigned examinations and can acquire only when assigned; interpretation, signing and release are restricted to the assigned radiologist. Both staffing patterns use the same states and report actions.

## Clinic settings and administration

Owner and administrator settings are available in the Settings workspace and use clinic-scoped APIs. Staff can hold multiple role grants. Owners/admins can create staff accounts directly in the server-resolved active clinic; each new account gets a one-time generated temporary password and is blocked from normal clinic APIs until changing it. Existing accounts are not forced through setup. Email invitations are not implemented. Administrators can activate/deactivate memberships and edit radiologist registration numbers. Only an owner can grant/revoke owner or transfer ownership, and ordinary staff creation cannot create owners. Every account, membership, role and activation change emits a clinic audit event without recording credentials. The separate clinical-service catalogue may link to a billing charge; examination orders retain the selected service code and order details while existing free-text ordering remains available. Default currency applies to newly created charges and invoice prefix to newly issued invoices. Timezone is read-only because the full application does not consistently honor per-clinic timezone. MFA, recovery, working-hours preferences and custom report templates are not implemented. See [SETTINGS.md](SETTINGS.md).

## Patient search and clinical history

Patient collections accept `search` for partial name, MRN or phone matches and use the existing pagination envelope. Potential duplicates require an exact MRN or name plus DOB/phone and are limited to the active clinic. The radiologist-only clinical-history route returns completed prior examinations, imaging metadata and released report contents; opening history is audited. Receptionist examination responses and permissions exclude clinical-history/report content.

Signing a draft report moves the exam from reporting to reported. A signed report is immutable and includes a signature-time snapshot of patient/exam/clinic/radiologist context. Releasing the latest signed report moves it to released; delivery is a separate audited transition and is retained on both the report version and examination. Amendments create a new report version linked to the prior version, reopen reporting with an audit event, and retain previous versions. Findings and impression are required before signing. PDF export is limited to signed versions and uses the persisted report plus its frozen context. See [REPORTING.md](REPORTING.md) for templates, PDF, API and limits.

## Imaging model

```text
Examination
└── ImagingStudy (DICOM or raster)
    └── ImagingSeries
        └── ImagingInstance (private storage key + size + SHA-256)
```

This preserves multiple studies, series, and instances per examination. DICOM records retain Study, Series, and SOP Instance UIDs validated as numeric dotted UIDs with a 64-character maximum under [DICOM PS3.5 section 9](https://dicom.nema.org/medical/dicom/current/output/chtml/part05/chapter_9.html). Pillow verifies JPEG/PNG bytes; pydicom parses DICOM Part 10 metadata and checks required image pixel data elements. DICOM files group by Study and Series UIDs rather than creating one examination per file. Embedded Patient ID must match the clinic MRN selected via the server-side examination.

Uploads are private, content-validated, bounded by configured per-file and batch limits, checksummed, retained as original bytes, and associated to the examination's server-resolved clinic patient. Opaque storage keys are never returned. Upload is allowed only for the assigned technician or radiologist while acquisition is `in_progress`; transition to `acquired` requires at least one persisted file present in private storage. Content downloads and DICOMweb reads are permission/tenant scoped, checksum-verified, private/no-store, and audited. Technician-only memberships only see assigned examinations. Receptionists have no image-view capability.

The local storage adapter defaults to `backend/private_media/`, outside public static/media serving. It cleans request failures and includes a scheduled management command to remove staging/orphan blobs older than 24 hours after hard process interruption. Configure filesystem permissions, encryption, backups and retention before deployment. Supported raster previews use normal browser image rendering with basic zoom/pan/navigation; this is not DICOM viewing. A limited authenticated QIDO/WADO adapter is provided for optional external OHIF integration. OHIF is not bundled; compressed frame support depends on installed pydicom decoder handlers. See [IMAGING.md](IMAGING.md) for limits, routes and viewer boundaries.

## Report and AI boundaries

Reports have a unique `(examination, version)` constraint and are allocated while locking the examination row. Draft edits, signing, release, and delivery are separate auditable operations. Report versions cannot be deleted or bulk-updated. A signed version's findings, impression, author, signature, and version linkage are immutable; corrections use an amendment.

Imaging `AIAnalysis` remains separate from `Report`. Revised Phase 7 adds `ClinicalHistoryEntry` for original clinician-entered symptom text, rule-extracted candidates and clinician-confirmed corrections, plus immutable `AIReportAssist` records separate from report drafts. The rule-based extractor and X-ray text structurer are not pretrained models and do not inspect images; CT remains manual, with no CT generalization claimed. They require clinician/radiologist review and are documented in [AI_RESEARCH.md](AI_RESEARCH.md). No external AI provider is configured, and no valid model evaluation has been run.

## Billing and audit

Clinic owners/admins maintain a clinic-specific charge catalogue for consultation, modalities and other services. Receptionists can select a patient and optional examination, add one or more catalogued services, and issue an invoice. The API snapshots each description, quantity, unit price, discount allocation and configured tax; it calculates invoice totals and a clinic/year/sequence invoice number. Changing the catalogue affects future invoices only. Issued terms are immutable; an unpaid invoice can be cancelled with a reason and audit event.

Payment records are append-only. Receptionists may record partial or split payments using cash, UPI or a short method code for other methods. Each payment is associated with its acting staff member and optional reference. API writes require a clinic-unique idempotency UUID; identical retries return the original payment and a conflicting reuse is rejected. UPI requires an explicit manual “receipt confirmed” flag; a transaction reference alone never marks it successful. Invoice-row locking serializes distinct payment writes and rejects overpayment on databases with row-level locking. Summaries group collections and balances by currency rather than summing unlike currency minor units.

Refunds create immutable rows for amount, reason, actor and timestamp while preserving the original payment. Owners/admins may record partial refunds or reverse the remaining payment amount. Balances are derived from payments less refunds. A fully paid invoice may move to `paid`; partial collections move to `partially_paid`; a full invoice refund moves to `refunded`; cancellation is separate. Daily summaries include net collections and payment-method totals, invoice and patient balances, and recent payments. Billing state is informational and never blocks clinical access.

Invoice, payment receipt and refund receipt PDFs use issue-time clinic/patient identity snapshots and stored line/payment values. Exports are private/no-store and audited. They are printable prototype documents, not verified tax or regulatory forms. No gateway, automatic UPI validation, accounting ledger or external report delivery is included. See [BILLING.md](BILLING.md), [API.md](API.md), and [DATA_MODEL.md](DATA_MODEL.md).

Important implemented domain writes append tenant-scoped `AuditEvent` records in the same transaction. Audit rows cannot be updated or deleted through model or queryset APIs. A full access/audit policy, log export, and production-grade archival/retention controls remain future work.
# Frontend workflow coverage

The React application under `frontend/` uses the same versioned endpoints described in [API.md](API.md); it does not mock persisted data or infer permissions from hidden controls. Multi-role navigation combines capabilities represented by the active membership's roles. Receptionists receive safe examination status fields for availability and delivery; clinical detail remains behind radiologist/technician API permissions. Acquisition actions are available to technicians and radiologists with acquisition capability, so a separate technician is not required.

The UI supports patient registration and server-side search, appointments/check-in, walk-in registration, role-gated clinician symptom extraction/review, assigned acquisition progression, imaging upload and raster preview, report drafting/signing/release, text-only observation structuring/review, invoice creation and payment history/recording, and staff account/membership/role administration with a mandatory first-login password change. The optional external OHIF iframe requires a configured OHIF 3 deployment and same-origin DICOMweb proxy; that viewer has not been verified here. No image inference, pretrained model, or clinical validation is implemented.
