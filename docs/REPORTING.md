# Radiology reporting

## Implemented workflow

An optional revised Phase 7 text-evidence research preview is documented in [AI_RESEARCH.md](AI_RESEARCH.md). It structures observations supplied by the assigned radiologist for chest X-ray only; CT remains manual and no CT generalization is claimed. It does not inspect image data or autonomously write into report drafts. It is a rule-based scaffold, not a diagnostic or clinically validated generator.

The reporting workspace is available to users with radiologist access. It opens examinations assigned to the active radiologist, loads patient demographics, prior radiology history, and study metadata, and opens the existing imaging workspace for previews. DICOM viewing remains optional: if OHIF is absent, the report editor still works and the imaging workspace explains the viewer limitation.

Templates are a server-side registry in `backend/clinics/reporting.py`. X-ray and ultrasound provide technique, findings, impression and comments; CT adds comparison; MRI adds sequences and comparison; general radiology is a fallback with comparison. Templates provide structure only: fields start empty and are entered by the radiologist. To alter template structures, change the registry and its modality mapping, then run the API tests. A clinic-specific template-management interface is not implemented.

Draft creation, editing, signing, release, amendment and delivery are distinct:

```text
draft → signed → released → delivered
          │          │
          └─ amendment creates a new draft version from the prior signed content
```

Draft edits are debounced in the React client, flushed on editor blur or examination switch, and saved to the API. Each edit and sign request includes the draft's `expected_updated_at` value, which the server checks under a row lock; stale clients are told to reload before saving or signing. This prevents silent last-write-wins replacement and signing content that the current client has not reviewed. Signing requires nonblank findings and impression, stores a snapshot of patient/examination/clinic/radiologist context, and locks report content. Version numbers are allocated while locking the examination; the unique `(examination, version)` constraint is the final concurrency guard. Release and delivery are separate audited transitions. Only the latest signed version can be released; when an amendment draft exists, the UI still allows viewing/downloading the prior signed PDF but omits its release action. Validation failures return safe domain messages so state/assignment problems are visible. Delivery currently records a human user's confirmation; the prototype does not send reports externally or obtain recipient acknowledgment. The previous signed version remains immutable and accessible to authorized radiologists.

Reception staff can see safe examination delivery status and mark a released report delivered through the existing exam action. They cannot list report records, read findings, or download PDFs. Report and PDF APIs require clinic context and report-read capability; draft edits, signing and release are further restricted to the assigned radiologist and their capability grants.

## Signed PDF

`GET /api/v1/reports/{report_id}/pdf/` produces a private/no-store PDF only for a signed, released or delivered report version. The frontend sends `Accept: application/pdf, application/json` to accommodate DRF's JSON-only renderer negotiation while the view returns `application/pdf`; it reads the response as a Blob and uses the authenticated same-origin session. Clinic context and report-read capability remain mandatory. The endpoint uses persisted content and the context snapshot saved at signing, not current unsaved browser fields. The PDF contains configured clinic identity/contact data, patient name/MRN/date of birth, examination/modality/indication, template/version/status, structured report fields, signature time, and radiologist name plus registration number when configured. Missing contact or professional details remain absent; the system does not invent them. Export creates `report.pdf_exported` audit history.

PDF generation uses ReportLab. Its built-in fonts do not cover every non-Latin writing system; configure and verify an appropriate font before localization or clinical deployment. The layout is a professional prototype, not a regulator-approved or clinically validated document.

## Administrative configuration

- `GET/PATCH /api/v1/clinics/profile/` lets clinic owners/administrators configure clinic name, address, phone and email.
- `PATCH /api/v1/memberships/{membership_id}/profile/` lets clinic administrators configure an optional professional registration number for a radiologist membership.

These changes are tenant-scoped and audited. The React staff screen does not yet expose these profile fields, so use the API or an authorized administrative integration until that UI is added.

## Verification and limits

Automated tests cover template matching, blank template content, structured draft edits, direct-model and bulk-write protections, stale concurrent draft writes/sign attempts, required signing fields, signed immutability, PDF response content type/authentication/tenant isolation and retrieval of the correct earlier version after amendment, signed release, unsigned and unauthorized release denial, release immutability, delivery, amendments, version-allocation conflict handling and receptionist restrictions. Frontend tests cover PDF Accept/session/blob handling, template selection, draft restoration, autosave requests, examination-switch flush, signing actions, and hiding release for superseded signed versions. Django system checks and the complete backend and frontend suites, TypeScript compilation and production build were run for the reporting PDF/release fix. These checks validate software behavior only; reports, PDF layout, templates and workflows have not undergone clinical validation or regulatory review. No AI report generation is implemented.
