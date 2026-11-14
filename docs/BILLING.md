# Billing and payments

## Implemented workflow

1. An owner/admin configures clinic-specific charge items in the charge catalogue. Supported categories are consultation, X-ray, CT, MRI, ultrasound and other services. Each item has a clinic-unique code, unit price, currency and optional tax rate.
2. A receptionist selects a clinic patient, optionally links a matching examination, and submits charge IDs and quantities. The backend resolves the current clinic prices and calculates subtotal, discount, tax and final amount. A discount requires the owner/admin discount capability. The API issues the invoice in the same transaction and returns its clinic/year/sequence number.
3. Reception records one or more cash, UPI or other method-code payments. Multiple entries support split tender and partial payment. API submissions carry an idempotency UUID so an identical network retry returns the original payment instead of recording it twice. UPI requires an explicit manual receipt confirmation; no gateway or reference-based confirmation exists.
4. Owners/admins can cancel unpaid invoices with a reason and can issue partial refunds or a full reversal. Each refund is separately retained. Issued invoice lines and payment records remain immutable.
5. Authorized billing users can view daily net collections, method totals, outstanding invoices, patient balances, payment history, invoice PDFs, receipts and refund receipts. Financial summaries group amounts by currency; unlike currencies are never added together.

## Statuses and balances

```text
issued → partially_paid → paid
  │             │           │
  └─ cancelled  └─ refund   └─ partial refund → partially_paid
                           └─ full invoice refund → refunded
```

`draft` is available in the model for workflow extension; the current API's invoice creation issues atomically and does not expose an editable draft. `cancelled` is only available when no payment history exists. Payment amount is reduced by refund rows; partial refunds increase invoice balance. A fully refunded invoice is recorded as closed/refunded with zero active balance; issue a new invoice if a new receivable is due. The system's delivery concept is unrelated to billing.

## Permissions and audit

- Receptionist: invoice issue, payment recording, billing read, printable documents.
- Owner/admin: all receptionist actions plus charge catalogue configuration, discounts, unpaid invoice cancellation, refunds/reversals and financial oversight.
- Radiologist/technician: no billing permissions by default. Billing is not a prerequisite for patient or clinical workflow access.
- A user with additional clinic roles receives the union of those role capabilities. Every endpoint still enforces active clinic context and clinic-scoped querysets.

Audited actions include catalogue creation/updates, invoice creation/issue/status change/cancellation, payment record/refund/reversal and document export. Refunds retain amount, reason, staff member and timestamp.

## Endpoints

See [API.md](API.md#billing-and-payments) for request formats and permissions. Main routes:

- `/charge-items/`, `/billing/summary/`
- `/invoices/`, `/invoices/{id}/payments/`, `/invoices/{id}/cancel/`, `/invoices/{id}/pdf/`
- `/payments/`, `/payments/{id}/refunds/`, `/payments/{id}/receipt/`
- `/refunds/`, `/refunds/{id}/receipt/`

All IDs resolve within active clinic scope. Invoices require patient and examination references from that clinic, and the examination patient must match. Amounts are integer minor units; tax rates are basis points. Invoice tax is computed per line after proportionally allocating the authorized discount, using integer minor-unit rounding.

## Document and compliance limits

ReportLab creates private/no-store PDFs from saved invoice lines and payment/refund records. Clinic and patient details are issue-time snapshots. No tax identification number, statutory tax rules, invoice-retention jurisdiction, or regulatory layout is inferred. PDF font coverage is limited for some non-Latin scripts. Have the relevant finance/legal reviewers configure and validate local requirements before operational use.

## Concurrency and remaining limitations

Invoice number allocation, payment recording and refunds use database transactions and row locks, with a unique clinic invoice-number constraint. The supplied SQLite development database does not implement PostgreSQL-style `SELECT FOR UPDATE`; concurrency needs verification on the intended database before deployment. There is no payment gateway, automatic UPI/bank verification, accounting ledger, or external refund settlement integration. A `refunded` record means staff recorded a refund; it does not establish that a bank or card network completed settlement.
