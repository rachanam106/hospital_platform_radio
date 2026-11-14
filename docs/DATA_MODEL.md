# Data model notes

The Django models live in `backend/clinics/models.py`. Tenant-owned records have a clinic foreign key; APIs resolve clinic from validated server-side session context and filter before lookup.

`ClinicalHistoryEntry` belongs to one clinic and patient. It append-only stores original clinician text, extracted symptom candidates and provenance separately from optional clinician-confirmed/corrected mentions. Pending/confirmed/rejected state changes go through the audited clinical-history review service; confirmed entries are immutable. It is not stored on `Patient` demographics and does not replace prior entries.

## Clinic configuration and clinical services

`Clinic` stores contact details, default currency, invoice prefix, timezone, active state and invoice sequence. The timezone currently remains read-only in the UI because date/time operations do not consistently honor each clinic's value. `Membership` is unique per clinic/user and has separate `MembershipRole` grants so one user can have multiple roles in a clinic. Ownership transfer is an audited transaction serialized on the clinic row.

`ClinicalService` is a clinic-scoped procedure catalogue, separate from `ChargeItem`. It includes code, name, modality, description, active state and an optional same-clinic link to a charge. `Examination.clinical_service` is optional for legacy/free-text orders; `service_code_snapshot` and existing modality/procedure fields preserve the order context if the catalogue is edited later.

`ChargeItem` now includes a description. Its profile can be edited through the audited billing configuration service; issued invoice line snapshots remain immutable. Audit events are append-only and the settings API exposes only allowlisted event details.

## Billing records

```text
Clinic
├── ChargeItem (clinic code, category, current unit price and tax rate)
└── Invoice (clinic sequence number, patient/examination, issued snapshots)
    ├── InvoiceLineItem (catalogue link plus immutable description/price/discount/tax snapshot)
    └── Payment (amount, method, actor, optional manual receipt confirmation)
        └── Refund (immutable amount, reason, actor and timestamp)
```

- `Clinic.next_invoice_number` is incremented under a clinic row lock. `Invoice` also has a unique `(clinic, invoice_number)` constraint.
- `ChargeItem` is the clinic's editable future-price catalogue. Invoice line rows snapshot the applied description, quantity, unit price, discount allocation and tax rate/amount so later catalogue changes do not affect issued documents.
- `Invoice` stores gross subtotal, discount, tax and final amount in minor currency units; amounts are server-calculated. Clinic and patient identity snapshots are captured at issue for printable invoices.
- `Payment` rows are append-only financial entries. They carry the staff actor, method, reference, an optional internal-service idempotency key (required by the API, unique per clinic), and, for manually confirmed receipts, confirmation actor/time.
- `Refund` rows preserve each partial or full amount returned; the original `Payment` is never deleted. Existing legacy `reversed_at` records remain readable.
- Invoice paid amount is computed from payments less refunds. Outstanding is payable less net payments for active invoices. A fully refunded invoice is closed with `refunded` status; partial refunds increase the remaining balance and restore `issued`/`partially_paid` status as applicable.

Migration `0008_chargeitem_invoicelineitem_refund_and_more` backfills previous flat invoices as a single “Previously issued service” line and preserves their status, amounts and clinic/patient identity. Migration `0009_payment_idempotency_key_and_more` adds the clinic-unique payment request key while preserving historical payments with a null key. The backfill is for continuity; legacy descriptions cannot be reconstructed from the former amount-only model.

## Non-goals and integrity

This schema does not implement an accounting ledger, gateway settlement reconciliation, statutory tax configuration, or an external delivery system. Row locks provide payment/refund/number serialization on databases that implement `SELECT FOR UPDATE`; SQLite development behavior is not evidence of production concurrency safety. Clinic scoping and service/model validations supplement, but do not replace, database-level row security.
