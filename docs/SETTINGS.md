# Clinic settings and administration

Settings are clinic-scoped through the active server session. The clinic ID is never accepted as proof of access; every endpoint resolves the current user’s active membership and capability. Changes use audited domain operations.

## Implemented

- **Clinic profile:** clinic name, contact details, default three-letter currency, and invoice prefix. The prefix affects new invoices only; historical invoice numbers are not rewritten. The profile endpoint returns the configured timezone read-only because the application does not consistently honor per-clinic timezone yet.
- **Staff and access:** owners and administrators can create Django accounts in the active clinic, assign one or more supported staff roles, grant/revoke roles, activate/deactivate memberships, maintain radiologist registration number, and transfer clinic ownership. New accounts receive a cryptographically generated temporary password once in the successful provisioning response. A server-side account flag forces password change before any clinic API is available; existing accounts are not affected. Temporary passwords are never persisted or included in audit events, membership lists or details. Email invitation and recovery are not provided. The Django default user model enforces unique usernames but does not enforce unique email addresses, so duplicate email values are accepted. Only an owner can transfer ownership or grant/revoke the owner role; ordinary staff creation cannot create owners. The clinic must retain an active owner.
- **Clinical service catalogue:** distinct clinic-scoped procedures with code, name, modality, description, active state and optional link to a billing charge. New examination orders may refer to an active service; service code and examination details are snapshotted on the examination. Existing free-text examination ordering remains supported.
- **Billing charge catalogue:** code, name, description, category, unit price, currency, tax basis points and active state can be configured by an owner/administrator. Invoices preserve their issued snapshots.
- **Audit history:** clinic administrators can filter by action, actor ID and date range. Responses allowlist operational details and omit arbitrary JSON payloads, reducing accidental disclosure of patient or report content.
- **Security and reporting information:** the UI describes current session and reporting-template limits. It does not present unavailable controls as operational settings.

## API

- `GET/PATCH /api/v1/clinics/profile/`
- `GET/POST /api/v1/memberships/` (existing-account membership attachment), `POST /api/v1/memberships/provision/` (new account plus active-clinic membership; returns a one-time `temporary_password`), `POST/DELETE /api/v1/memberships/{id}/roles/`, `PATCH /api/v1/memberships/{id}/active/`, `PATCH /api/v1/memberships/{id}/profile/`, and `POST /api/v1/memberships/{id}/transfer-ownership/`
- `POST /api/v1/auth/password/change/` (authenticated, CSRF-protected current/new password change). Until first change is complete, only `/auth/me/`, logout and this route are available to that account.
- `GET/POST /api/v1/clinical-services/`, `GET/PATCH /api/v1/clinical-services/{id}/`
- `GET/POST /api/v1/charge-items/`, `GET/PATCH /api/v1/charge-items/{id}/`
- `GET /api/v1/audit-events/?action=...&actor_id=...&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD`

All catalogue records and audit results are tenant-scoped. Clinical service reads are available to owners, administrators and radiologists; edits require clinic administration/billing configuration capability. Charge editing requires billing configuration capability.

## Not implemented

Logo upload, clinic registration/profile identifiers, invitation delivery, MFA, session inventory/revocation, account recovery, configurable working hours, notification settings, localization preferences, honored clinic-specific timezone, and editable/versioned report templates. Built-in modality report structures remain static. There is no clinic-level currency enforcement across all historic records: currency defaults apply when a charge is created, while a charge’s own currency determines invoice currency.

Ownership and membership changes are transaction-wrapped and owner changes serialize on the clinic row. SQLite does not provide PostgreSQL-equivalent row locking; validate ownership transfer contention on PostgreSQL before relying on it in deployment.
