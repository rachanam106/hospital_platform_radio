# Versioned API

The foundation API is served below `/api/v1/`. Responses use JSON envelopes. Resource collection responses include pagination information under `data`:

```json
{"data":{"count":1,"next":null,"previous":null,"results":[{}]}}
```

Errors use a stable shape; validation errors may include field details. Cross-clinic object lookups return the same `not_found` response as missing objects.

```json
{"error":{"code":"permission_denied","message":"Active clinic context is unavailable."}}
```

## Authentication and clinic context

Authentication uses Django server-side sessions with DRF `SessionAuthentication`. Login and logout are POST-only and CSRF protected. Session and CSRF cookies are HttpOnly and SameSite=Lax; outside local debug mode they are Secure, HTTPS is redirected, and HSTS is enabled. `DJANGO_SECRET_KEY` is required outside debug mode. Failed login responses do not distinguish an unknown username from an incorrect password; authentication audit rows contain no submitted username, password, IP, or patient data.

The frontend first obtains a CSRF token, submits it in `X-CSRFToken`, and retains the session cookie:

1. `GET /api/v1/auth/csrf/` returns `data.csrf_token` and sets the CSRF cookie.
2. `POST /api/v1/auth/login/` with `{ "username": "...", "password": "..." }` returns the user ID, `must_change_password`, and a rotated CSRF token. Store the session cookie and use the returned CSRF token for later writes.
3. `GET /api/v1/clinics/` lists the authenticated user's active clinic memberships and role grants.
4. `POST /api/v1/clinics/active/` with `{ "clinic_id": "..." }` selects a clinic. The server checks current membership and stores the active clinic ID in the server-side session. This ID is only a selector; it does not grant access.
5. `GET /api/v1/auth/me/` returns the current user, active memberships, and validated active clinic context.
6. `POST /api/v1/auth/logout/` ends the session and requires CSRF.

Newly provisioned staff accounts have `must_change_password: true`. Until `POST /api/v1/auth/password/change/` succeeds with `current_password` and `new_password`, API middleware allows only `/auth/me/`, `/auth/logout/`, and the password-change route. The password-change endpoint validates the current password and Django's configured password validators, then clears the gate. Existing accounts are not enrolled in this gate. The generated temporary password is returned once by the owner/admin provisioning response; no password-reset token or email invitation is implemented.

Every clinic resource request resolves that session context to a currently active `Membership` and clinic row. Revoking the membership or deactivating the clinic invalidates the context on the next request. No resource endpoint accepts a clinic ID as authorization proof.

## Endpoints and capability gates

| Resource | Methods/actions | Required capabilities |
|---|---|---|
| `/clinics/`, `/clinics/active/` | list, get/select context | authenticated membership; selection is validated server-side |
| `/clinics/profile/` | GET, PATCH | clinic administrator; identity/contact plus default currency and invoice prefix (timezone read-only) |
| `/memberships/` | GET; POST existing-account attachment; `provision` (POST); detail GET; `roles`, `active`, `profile`, `transfer-ownership` | membership read; provisioning and administration require clinic administration; owner grant/transfer remains owner-only. Provisioning uses server-resolved active clinic and cannot create owner roles. |
| `/patients/` | paginated GET with `search` (name, medical record number, phone), POST; detail GET/PATCH; `potential-duplicates`, `clinical-history` | patient read; patient admin for create/update; radiologist-only clinical history |
| `/appointments/` | GET, POST; detail GET; `transition`, `reschedule` | appointment read/admin, check-in |
| `/examinations/` | GET, POST; detail GET; `transition`, `deliver`, detail `assignment`; collection `assignees` | exam read (technician sees assigned exams only; radiologist sees clinic worklist), status-only read (receptionist), interpretation, acquisition, check-in, delivery, assignment |
| `/examinations/{id}/imaging/` | multipart POST with one or more `files` | image upload + assigned acquisition operator; examination must be `in_progress` |
| `/imaging-studies/`, `/imaging-series/`, `/imaging-instances/` | GET list/detail; optional examination/study/series filters | image view; technician results limited to assigned examinations |
| `/imaging-instances/{id}/content/` | authorized original file GET | image view; access audited |
| `/dicomweb/studies...` | limited QIDO-RS study/series/instance query and WADO-RS metadata/instance/frame GET | image view; clinic and technician assignment scoped; access audited |
| `/reports/templates/` | GET | report read; blank X-ray, CT, MRI, ultrasound and general structures |
| `/reports/` | GET, POST; detail GET/PATCH; `sign`, `release`, `pdf` | report read, interpretation, sign, release |
| `/ai-analyses/` | GET list/detail | report read (radiologist) |
| `/charge-items/` | GET list/detail; POST; PATCH | billing read; charge configuration is owner/admin only |
| `/clinical-services/` | GET list/detail; POST; PATCH | service catalogue read for owner/admin/radiologist; changes require billing configuration |
| `/billing/summary/` | GET with optional `date` and `patient_id` | billing read; clinic-scoped collections and outstanding summaries |
| `/invoices/` | GET with optional `patient_id`, POST; detail GET; `payments` GET/POST; `cancel`, `pdf` | billing read/write; cancellation is owner/admin only |
| `/payments/` | GET list/detail; `refunds` GET/POST; `receipt`; `reverse` | billing read; refunds/reversals are owner/admin only |
| `/refunds/` | GET list/detail; `receipt` | billing read |
| `/memberships/` | GET, POST; detail GET; `roles`, `active`, `profile`, `transfer-ownership` | membership read; clinic admin; owner role required to grant/revoke or transfer owner |
| `/audit-events/` | GET list with optional `action`, `actor_id`, `date_from`, `date_to` | audit read; clinic scope and safe detail allowlist |

All list/detail viewsets filter by `request.clinic` before lookup. The serializers omit storage keys. Imaging upload/content and limited DICOMweb routes use private storage, clinic scoping and audit checks; see [IMAGING.md](IMAGING.md). Patient write and clinical workflow actions call the audited domain services rather than saving request data directly.

Receptionists can work with demographics, appointments, check-in, billing, and report delivery state. They can list/retrieve a status-only examination representation (identifier, patient ID, appointment ID, modality and timestamps) to track availability; it omits procedure and clinical indication. They cannot read/edit clinical reports or use clinical examination actions. Technicians can read only assigned acquisition work and image metadata/content for assigned examinations, upload during acquisition, and cannot sign or release reports. Radiologists can acquire/upload and interpret when granted corresponding roles. Receptionists cannot access imaging metadata or content. Owners/admins do not automatically receive clinical capabilities.

Receptionists can issue invoices and record payments. Owners and administrators additionally configure charge items, apply discounts, cancel unpaid invoices and refund payments. Doctors/radiologists and technicians have no billing capability by default; billing access can be granted through an additional clinic role. A submitted UPI reference is never evidence of receipt: UPI requires a separate manual `confirmed_received: true` confirmation by the recording staff member.

Patient search and duplicate candidate queries always run against the current active clinic-scoped queryset before filtering and pagination. `search` supports partial name, MRN, and phone matches. `potential-duplicates` requires an exact MRN or a name plus DOB/phone and returns at most ten same-clinic candidates. Clinical history requires `patient_clinical_history`; it includes completed prior examinations, imaging-study metadata, and released report text, and emits a `patient.clinical_history_accessed` audit event. Receptionists and technicians do not have that capability.

Each new examination starts assigned to the ordering radiologist. A radiologist or clinic administrator may update its assignment through `POST /examinations/{id}/assignment/` with a required clinic radiologist ID and optional technician ID; both selected users must have active role grants in that clinic. Assignment changes include previous/current IDs in `examination.assignment_changed` audit details. A technician sees only examinations currently assigned to them. Acquisition requires the assigned technician when one exists; otherwise it is performed by the assigned radiologist. Reporting, signing and release require the assigned radiologist. Technician assignment is optional.

## Billing and payments

- `POST /charge-items/` creates a clinic-specific catalogue item with `code`, `name`, `category`, `unit_price_minor`, optional currency and `tax_rate_bps`. Owners/admins can `PATCH` name, category, price, tax rate and active state. Issued invoice lines retain their description, price, discount and tax snapshots.
- `GET/POST /clinical-services/` and `GET/PATCH /clinical-services/{id}/` manage the separate clinical procedure catalogue. A service has a code, name, modality, description, active state, and optional same-clinic charge link. `POST /examinations/` may specify an active `clinical_service_id`; modality/procedure name can still be supplied directly. The examination retains a code snapshot and order details.
- `POST /invoices/` accepts `patient_id`, optional matching `examination_id`, `line_items: [{charge_item_id, quantity}]`, and optional `discount_minor`. The server resolves active clinic charges and calculates subtotal, allocated discount, tax, payable total and invoice number. Client totals are not accepted.
- Invoice creation atomically issues the invoice and creates its line snapshots. Invoice number is clinic/year/sequence scoped. Issued terms and lines are immutable. An unpaid invoice can be cancelled with a reason; one with payment history cannot be silently cancelled.
- `POST /invoices/{id}/payments/` accepts amount in minor units, method code, optional reference, `confirmed_received`, and a required UUID `idempotency_key`. Repeating an identical request with that key returns the original payment; reusing it with different details is rejected. Currency defaults to the invoice currency and is checked if supplied. Multiple distinct keys support partial or split-method payments. Built-in methods include cash and UPI; a short lowercase method code also supports other methods such as card, transfer or cheque. UPI requires manual confirmation and is never automatically verified from its reference.
- `POST /payments/{id}/refunds/` accepts `amount_minor` and a reason. Refunds are separate immutable rows and do not delete or rewrite the payment. `POST /payments/{id}/reverse/` records the full unrefunded amount through the same refund ledger. Only owners/admins may refund or reverse.
- `GET /billing/summary/?date=YYYY-MM-DD` returns net collections, method totals and outstanding balances grouped by currency, active invoice count, partial count and recent payments. Single-currency responses also include scalar compatibility fields; mixed-currency scalar totals are `null` to avoid adding unlike currencies. Add `patient_id` for that clinic patient's balance grouped by currency. Billing status never blocks patient, examination, imaging or reporting access.
- `GET /invoices/{id}/pdf/`, `GET /payments/{id}/receipt/`, and `GET /refunds/{id}/receipt/` return private/no-store PDFs and create `billing.document_exported` audit events.

Financial writes emit `billing.charge_created/updated`, `invoice.created/issued/cancelled/status_changed`, `payment.recorded/refunded/reversed`, and document-export events. Invoice-row and payment-row locks serialize number allocation, payments and refunds on databases with row-level locking; unique invoice numbers and transactional validation are final guards. SQLite development tests do not establish PostgreSQL concurrency behavior.

## Radiology reporting

- `GET /reports/templates/` returns blank modality-specific structures; it inserts no findings, diagnoses, measurements, or normal statements.
- `POST /reports/` accepts `examination_id`, optional `amends_id`, and optional `template` (`xray`, `ct`, `mri`, `ultrasound`, `general`). If omitted, the server selects the modality-matched template. Incompatible modality/template pairs are rejected.
- `PATCH /reports/{id}/` edits structured fields only while draft and requires the report's current `expected_updated_at` timestamp. The timestamp is checked under a row lock; stale writes return a conflict-style validation error instead of replacing another editor's changes. Each accepted server edit is audited.
- `POST /reports/{id}/sign/` requires the current `expected_updated_at`, nonblank findings and impression, and the assigned radiologist. Stale clients must reload and review the draft. Signing freezes patient, examination, clinic, radiologist, report date, and structured content; signed content cannot be overwritten.
- `POST /reports/{id}/release/` is a separate audited operation for the latest signed version.
- `GET /reports/{id}/pdf/` returns a private/no-store `application/pdf` for a signed version only. The frontend requests `Accept: application/pdf, application/json` because the endpoint returns a raw PDF response while DRF negotiates against the configured JSON renderers. It consumes the response as a Blob. The request uses the same-origin session and remains clinic/capability scoped; GET is safe and does not require CSRF. PDF content comes from persisted signed fields and the signature snapshot, including earlier versions after amendment. It records PDF export access.
- `POST /examinations/{id}/deliver/` records an authorized user's confirmation that the released version was delivered. This is a manual status record, not outbound email/fax or recipient acknowledgment. Receptionists can see the safe examination status only, not report content.
- `GET/PATCH /clinics/profile/` configures clinic name/contact, default currency for newly created charges, and invoice prefix for newly issued invoices. Existing invoice numbers are not rewritten. Timezone is returned read-only because per-clinic timezone is not consistently honored. `PATCH /memberships/{id}/profile/` configures an optional radiologist registration number. These changes are audited; missing information remains blank and is never fabricated.

## Clinician-reviewed symptom extraction

- `GET/POST /patients/{id}/clinical-history/entries/` lists a patient's entries or accepts `{ "original_text": "..." }` for local rule-based extraction. The source text and extracted candidates are stored separately as a pending entry.
- `POST /patients/{id}/clinical-history/entries/{entry_id}/review/` accepts `{ "decision": "confirmed|rejected" }`; confirmation may include a corrected `confirmed_mentions` list. The original text and extraction remain unchanged; subsequent correction is a new entry.

Both routes require the existing `PATIENT_CLINICAL_HISTORY` capability, currently granted to radiologists. They are tenant-scoped and audited; audit details do not contain the text. The platform has no separate doctor role. Extraction is a limited local lexicon/rule prototype with uncalibrated confidence, not a trained NLP model or diagnosis.

## Evidence-assisted reporting (research preview)

- `POST /reports/{id}/ai-assist/` accepts optional text `clinical_indication` and `technique`, plus 1–40 `{ "anatomy": "...", "text": "...", "polarity": "positive|negative|uncertain" }` observations. Only an assigned radiologist with interpretation capability may use an editable chest/thorax X-ray draft. CT remains manual; no CT generalization is claimed. Output stores separately from the report.
- `GET /reports/{id}/ai-assists/` returns persisted suggestions and review state for the assigned radiologist.
- `POST /reports/{id}/review-ai-assist/` accepts `{ "assist_id": "...", "decision": "accepted|rejected" }`. This is a review/audit transition only. The frontend copies accepted text into editable fields, then the ordinary report draft endpoint saves it with concurrency protection.

The implementation is local and deterministic: it structures explicitly supplied radiologist observations and runs lexical cosine/polarity checks. It does not call a model provider or inspect images/DICOM. Similarity is not entailment; it is not diagnostic or clinically validated. Manual reporting remains available. See [AI research documentation](AI_RESEARCH.md).

## Clinic settings and administration

- Owners/admins can provision a new account in the server-resolved active clinic, add existing platform accounts, grant or revoke multiple roles, activate/deactivate memberships and edit radiologist registration details. Provisioning returns a temporary password only once and requires password change before normal API access. It does not send email. Owners alone can grant/revoke owner and transfer ownership using `POST /memberships/{id}/transfer-ownership/`; the transaction ensures the clinic retains an owner and records the actor/target.
- Owners/admins manage clinic-scoped clinical services and billing charges. Clinical services are not financial charges; a service can optionally link to one charge. Service and charge edits are audited.
- `GET /audit-events/` supports action substring, numeric actor ID, and ISO date bounds. Audit response details are limited to an allowlist; arbitrary event JSON is never returned.
- See [SETTINGS.md](SETTINGS.md) for the implemented settings UI and known gaps. MFA, account recovery, session inventory, logos, configurable schedules, and editable report templates are not implemented.

PDFs are generated with ReportLab from persisted signed content and the signature-time snapshot. ReportLab's built-in fonts have limited non-Latin glyph coverage; localization requires explicit font configuration and validation.

## Operational notes and limitations

- Pagination defaults to 25 rows and caps `page_size` at 100.
- Login has an anonymous rate limit (20/hour). The default Django cache is process-local; production needs a shared cache for consistent throttling across workers.
- Email invitations, password reset, MFA, and public/self-service account creation are not implemented. Owner/admin staff provisioning exists at `POST /memberships/provision/`; membership administration can also link an existing Django user ID to a clinic.
- Tenant isolation is enforced in application viewsets and domain services plus related-object validation. There is no PostgreSQL row-level security policy; trusted internal ORM code can still make an unscoped query if it bypasses these entry points.
- S3-compatible storage, resumable upload, PACS integration, and bundled OHIF are not included. The DICOMweb adapter is limited, compressed transfer syntax decoding depends on optional pydicom handlers, and no clinical viewer validation has been performed.
- Production deployment, TLS termination, secret rotation, session/cache operations, backups, monitoring, and clinical/legal controls require deployment-specific review. This is not production-ready.
