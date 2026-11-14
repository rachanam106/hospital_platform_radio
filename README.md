# Radiology Clinic Management & EMR

Radiology-first, multi-clinic EMR prototype with a reviewed Django/DRF backend and a React/TypeScript clinic workspace. The frontend uses the existing `/api/v1/` APIs and session-authentication flow; it does not replace or recreate the backend architecture.

## Implemented

- Django REST Framework endpoints under `/api/v1/` with JSON success/error envelopes and pagination.
- CSRF-protected session login/logout, a CSRF token endpoint, generic invalid-credential errors, authentication audit events, secure cookie settings, and an anonymous login throttle. Owner/admin staff provisioning creates accounts with one-time temporary passwords and a server-enforced first-login password change.
- A user can have memberships in multiple clinics and multiple active roles per clinic. Clinic selection is stored in the server-side session only after membership validation, and is revalidated for every clinic resource request.
- Backend capability checks and tenant-scoped list/detail access for patients, appointments, examinations, imaging metadata, reports, AI analysis records, invoices, payments, memberships, and audit events.
- Clinical and administrative mutations call audited domain services. Cross-clinic detail lookups return `not_found`; client-supplied clinic IDs are never accepted as proof of access.
- Receptionists cannot access clinical report/examination content; technicians cannot sign or release; radiologists granted acquisition and radiologist roles can acquire and interpret.
- Protected endpoint tests for CSRF/authentication, multiple clinics/roles, membership revocation, cross-tenant access, and role-restricted workflow actions.
- Responsive React client with CSRF-protected session login/logout, server-validated clinic selection, role-aware combined navigation, patient and appointment workflows, acquisition queue, imaging uploads and raster preview, reporting lifecycle, billing/payment recording, and staff account/role/membership management.
- Clinic billing with clinic-specific configurable charges, invoice line snapshots and numbering, server-calculated totals, partial/split payments, manually confirmed UPI receipts, owner/admin refunds and cancellations, outstanding summaries, audit events, and private printable invoice/payment/refund documents. See [billing workflow and limits](docs/BILLING.md).
- Structured radiology reporting with blank modality templates, debounced draft autosave, immutable signed versions, amendments, separate release/delivery states, and audited signed-version PDF export. See [reporting workflow and limits](docs/REPORTING.md).
- Optional chest X-ray text-evidence assistance for assigned radiologists: local observation structuring, claim attribution/uncertainty, immutable provenance and explicit review. CT remains on the manual reporting flow; no CT generalization is claimed. The assistant does not analyze images and is not a diagnostic model. See [AI research scope and dataset gate](docs/AI_RESEARCH.md).
- Clinician-reviewed symptom extraction in the radiologist workspace: original history text, rule-based symptom spans/negation/uncertainty/duration candidates, editable clinician-confirmed mentions, append-only history and audit events. It is a limited rule-based prototype, not a loaded biomedical NLP model. See [revised Phase 7 research limits](docs/AI_RESEARCH.md).
- Reception-safe examination status endpoint representation so front-desk staff can track report availability without seeing procedure or indication details.
- Frontend tests for session requests, multi-role navigation, server-side patient search/pagination, and CSRF-protected image upload progress.

See [frontend setup and behavior](docs/FRONTEND.md), [API usage and limits](docs/API.md), [billing workflow](docs/BILLING.md), [data model](docs/DATA_MODEL.md), [imaging management](docs/IMAGING.md), [radiology reporting](docs/REPORTING.md), [AI research scope](docs/AI_RESEARCH.md) and [workflow/model boundaries](docs/WORKFLOW.md).

## Architecture and role model

Clinic-owned data uses explicit clinic relations and related-object validation. Domain services use clinic-scoped lookups and transaction boundaries; serializers do not accept a clinic ID on business records. Capabilities are distinct from role labels, so financial configuration/refund rights and clinical signing rights are independent. Reports remain versioned/immutable after signing, issued invoices snapshot their terms, payments and refunds retain append-only history, image records remain hierarchical, and AI results stay separate from verified reports.

| Capability | Owner/admin | Receptionist | Technician | Radiologist |
|---|---:|---:|---:|---:|
| Patient registration/update | Yes | Yes | No | No |
| Appointment scheduling/check-in | Yes | Yes | No | No |
| Examination read/acquisition | No | No | Yes | Yes |
| Image metadata view | No | No | Yes | Yes |
| Interpretation and report drafting | No | No | No | Yes |
| Sign/release report | No | No | No | Yes |
| Billing | Yes | Yes | No | No |
| Membership and audit administration | Yes | No | No | No |

## Setup

From the project root, create a local backend environment and database:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:DJANGO_DEBUG = 'true'
python manage.py check
python manage.py migrate
python manage.py createsuperuser
$env:BOOTSTRAP_USERNAME = '<local-superuser-username>'
python manage.py shell -c "exec(open('bootstrap_local.py', encoding='utf-8').read())"
python manage.py runserver
```

Set `BOOTSTRAP_USERNAME` to the local superuser username before running the bootstrap command; optionally set `BOOTSTRAP_CLINIC_NAME`. The helper stops if any clinic already exists; never run it against a database with existing clinic data.

SQLite is used locally when `DATABASE_URL` is unset. For PostgreSQL, copy `backend/.env.example` to `backend/.env` and replace all placeholders. `DJANGO_SECRET_KEY` is required outside debug mode. In local debug mode, Django generates an ephemeral key if the variable is missing. Never commit `.env` or real credentials, and do not use real patient information.

For client authentication, call `GET /api/v1/auth/csrf/`, use its returned token as `X-CSRFToken` on login and every later POST/PATCH/DELETE, retain the session cookie, then select a clinic with `POST /api/v1/clinics/active/`. Full request examples are in [API docs](docs/API.md).

### Frontend

After installing and starting the backend, use a second terminal from the project root:

```powershell
cd frontend
npm ci --cache "$env:LOCALAPPDATA\npm-cache"
npm run dev
```

Vite proxies `/api` to `http://127.0.0.1:8000`. For frontend checks run `npm ci`, `npm test`, and `npm run build`. `npm ci` uses the committed lockfile. See [frontend documentation](docs/FRONTEND.md) for Windows `EPERM` troubleshooting, session/CSRF details, screen coverage and actual API limitations.

### Architecture

- `backend/`: Django/DRF API, tenant-aware permissions, domain services, migrations, private local imaging storage and tests.
- `frontend/`: React/TypeScript application built with Vite; API calls use same-origin `/api/v1/` paths.
- `docs/`: API, workflow, imaging, reporting, billing, settings, data-model and research limitations.
- `research/`: reproducibility scripts and a reviewed source/license manifest. Research datasets and generated results are excluded from Git.

### Separate frontend/backend deployment prerequisites

This repository contains no provider-specific deployment configuration. For a prototype deployment, configure Vercel to build from `frontend/` with `npm ci` and `npm run build`, and serve `dist/`. Route browser requests under `/api/` through a same-origin HTTPS rewrite/proxy to Django. The client uses Django session cookies and CSRF; this project does not configure cross-origin CORS. A frontend and API on unrelated default provider domains will not work reliably with the current `SameSite=Lax` session cookies.

Configure Django with PostgreSQL and environment variables from `backend/.env.example`: a private random `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=false`, correct `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` for the browser origin, `DATABASE_URL` and database TLS mode. Run migrations as a release step. HTTPS is required because secure cookies, redirect and HSTS are enabled outside debug mode. Verify proxy host/origin forwarding, cookies and CSRF end to end. The environment variable `DJANGO_CSRF_TRUSTED_ORIGINS` is read by Django settings.

Imaging currently writes to a private local filesystem path configured by `PRIVATE_IMAGE_STORAGE_ROOT`; a production S3-compatible adapter is not wired. Ephemeral host storage can lose uploaded studies on redeploy and is not suitable for persistent imaging. Configure durable private storage and backups, or keep imaging disabled for a disposable demo. Do not upload real patient data to a free prototype deployment. Check provider free-tier database, storage, sleep and bandwidth limits before choosing services; those terms change.

## Verification

Latest verified checks: Django system check passed; `makemigrations --check --dry-run` reported no model changes; all 46 backend tests passed. Frontend TypeScript compilation and production build passed; all 22 frontend tests passed. Backend tests use an isolated test database. These checks do not verify provider deployment configuration or PostgreSQL concurrency behavior.

## Not implemented / limitations

- A local synthetic-clinic bootstrap helper and in-app staff-account provisioning are implemented. Email invitations, password reset/recovery and MFA are not implemented.
- Local private image upload and authenticated retrieval are implemented. The limited DICOMweb adapter now decodes native uncompressed, RLE, JPEG Baseline/Extended, and JPEG 2000 transfer syntaxes when the installed pydicom handlers support them; other compressed syntaxes can return HTTP 406. OHIF has a read-only configuration example and a frontend iframe/proxy integration point, but it is not bundled and could not be launched/browser-verified in this environment. See [imaging compatibility, OHIF setup, and limitations](docs/IMAGING.md).
- Revised Phase 7 provides limited rule-based symptom extraction and text structuring, with clinician review and a heuristic claim verifier. No pretrained model was loaded, no dataset was accessed, and no valid symptom-extraction or report-generation experiment was run. Text-only structuring does not interpret imaging. See [dataset access review and model limitations](docs/AI_RESEARCH.md). Templates and PDFs are not clinically validated; OHIF remains optional and unverified as documented in [IMAGING.md](docs/IMAGING.md).
- Billing uses staff-recorded receipts and refunds; it does not connect to a payment gateway, verify bank settlement, or claim tax/legal compliance. Tax rates are generic configurable percentages. Custom payment method codes are recorded as entered.
- Billing summaries now separate amounts by currency. The role model has no distinct `doctor` value; interpreting clinicians use the `radiologist` role, so doctor-specific permission behavior cannot be tested until the product defines that role.
- PDF font coverage is limited for non-Latin scripts.
- Clinic profile, membership/role/status administration, radiologist registration, ownership transfer, clinical service catalogue, billing charge catalogue, and filtered/redacted audit history are exposed in the owner/administrator Settings workspace. Invoice prefix and default charge currency settings affect newly created records only. See [clinic settings](docs/SETTINGS.md).
- Clinic logo/registration identifiers, invitations, MFA, session management, account recovery, working hours, notification preferences, consistently honored clinic timezone, and editable report templates remain unimplemented. The settings workspace reports these limitations explicitly.
- No PostgreSQL row-level security; current tenant isolation is at the application/model/service layer. Internal ORM code that bypasses the API and domain services can still query across tenants.
- Production operations are not configured: provider routing, durable private object storage, shared cache/rate-limit persistence, monitoring, backup/restore, key rotation, and security review remain deployment work.
- Imaging upload is synchronous, limited to configured size caps, and not resumable; operational storage security, backups, and retention must be configured before deployment.

This project is an academic prototype foundation and is **not production-ready** or suitable for real patient data.
