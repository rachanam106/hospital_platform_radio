# Frontend application

The patient profile includes clinician-reviewed symptom extraction for the existing radiologist role, and the ReportingWorkspace includes optional text-only radiologist observation structuring. These are local rule-based research previews, not trained NLP models or image interpretation. See [AI_RESEARCH.md](AI_RESEARCH.md) for dataset gates and limitations.

The React 18 + TypeScript client in `frontend/` integrates with the existing Django `/api/v1/` API. It uses same-origin Django session cookies and CSRF tokens; it does not persist credentials or session tokens in browser storage. Vite proxies `/api` to Django at `http://127.0.0.1:8000`. When OHIF is enabled, Vite also proxies `/ohif` to `OHIF_DEV_SERVER` (default `http://127.0.0.1:3000`) so viewer requests use the same browser origin.

## Setup (Windows)

1. Set up and start Django following the root README.
2. In a second PowerShell terminal, use the locked dependency set. If npm's configured cache is restricted, choose a user-local cache for the command; this does not change the lockfile or require administrator access.
3. From `frontend/`, install exact locked versions and start Vite:

   ```powershell
   npm ci --cache "$env:LOCALAPPDATA\npm-cache"
   npm run dev
   ```

4. Run verification:

   ```powershell
   npm run build
   npm test
   ```

The repository keeps `package.json` versions and `package-lock.json`; do not delete the lockfile. In the managed Codex workspace, Node received `EPERM` creating nested directories under `node_modules` despite the current account having full directory control and no Node process holding the directory. The locked install succeeded through authorized workspace execution, indicating a managed process/filesystem restriction rather than a dependency conflict. If `EPERM` persists on a normal Windows machine, close Node/Vite processes, verify project/cache ACLs, and check Controlled Folder Access. Do not disable antivirus or apply broad exclusions.

## Authentication and clinic context

The client obtains a CSRF token from `GET /api/v1/auth/csrf/`, posts credentials to the CSRF-protected session login, and sends `X-CSRFToken` for writes with `credentials: same-origin`. Logout is a CSRF-protected POST. A 401 or backend `not_authenticated` response clears the in-memory CSRF token and returns the application to sign-in; a permission denial does not masquerade as session expiry. Clinic selection calls the server-side active-context endpoint and reloads `/auth/me/`. UI role checks shape navigation only; API capability and tenant checks remain authoritative.

## Implemented UI and backend-connected workflows

- Shared responsive workspace, sign-in/out, clinic switcher, combined navigation for users holding multiple roles, loading/error/empty states, notifications, tables and dialogs.
- Reception: patient registration, profile view, paginated server-side patient search by name/MRN/phone, duplicate candidate warning, appointment booking, check-in, walk-in flow, examination status/report availability, invoice and partial-payment recording.
- Technician: assigned examination worklist, acquisition progression, private JPG/JPEG/PNG and DICOM upload, raster preview with zoom/pan/navigation, study/series/instance lists and safe metadata. Upload is accepted only during acquisition.
- Radiologist: assigned/pending examination workflow, acquisition when capability permits, patient/prior clinical history, associated imaging workspace, modality-specific structured report drafting, debounced autosave, draft preview, signing, release, version history, and signed-version PDF download.
- Owner/administrator: clinic overview and Settings for clinic contact, currency/invoice prefix, multi-role staff, membership activation, radiologist registration, ownership transfer, clinical services, charge catalogue and filtered audit history.
- Staff assignment supports optional technician and assigned radiologist. A radiologist may acquire and interpret the same examination. Changes are saved through the backend and audited.

Both staffing arrangements use the same backend entities: technician acquires then radiologist interprets, or assigned radiologist acquires and interprets. Report release and delivery update the examination/report state that reception sees for availability. These paths are covered by backend integration tests; the UI does not simulate persisted records.

## Boundaries and limitations

Patient search and potential-duplicate checks are server-side and clinic-scoped. Radiologist clinical history is separately capability-gated and audited; receptionist-safe examination status responses do not contain clinical report content. API authorization is required regardless of what the current navigation displays.

The reporting workspace uses the live report, patient, history, and imaging APIs. Draft changes are debounced and saved to the backend, while sign/release and PDF export use separate backend actions. Clinic contact and radiologist registration details are editable in Settings. Report templates remain built-in and read-only. See [REPORTING.md](REPORTING.md) and [SETTINGS.md](SETTINGS.md).

The billing workspace uses live charge, invoice, payment and summary APIs for invoice creation, server-calculated totals, partial/split payment recording, manual UPI receipt confirmation, refund entry for owners/admins, patient/invoice balances, and private invoice/receipt/refund PDFs. Catalogue administration is centralized in Settings. The browser does not infer or calculate invoice tax totals. Billing workflow and API limitations are in [BILLING.md](BILLING.md).

The app has a limited DICOMweb adapter and optional OHIF iframe integration; OHIF is not bundled. Set `VITE_OHIF_VIEWER_URL` and configure the OHIF data source as described in [IMAGING.md](IMAGING.md). A configured external OHIF viewer has not been browser-verified here; compressed-frame decoding may be unavailable. Owner/admin staff account provisioning and first-login password change are implemented. Email invitations, account recovery, MFA, AI inference, generated report text, push updates and production infrastructure are not implemented. This prototype is not production-ready and must not use real patient data.

## Verification status

Latest verification: `npm run build` (including TypeScript compilation) passed, all 22 frontend tests passed, Django `manage.py check` passed, `makemigrations --check --dry-run` reported no model changes, and all 46 backend tests passed. This prototype is not production-ready.
