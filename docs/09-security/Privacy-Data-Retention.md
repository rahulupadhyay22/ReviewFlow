# Privacy & Data Retention

## Status: V1 Operational Policy Locked

The following retention periods are the V1 operational defaults for ReviewFlow. They are implementation controls and should be reviewed against applicable legal, contractual, tax, and customer-deletion requirements before production launch.

## What Is Collected

| Data | Where stored | Contains PII? | V1 retention |
|---|---|---:|---|
| Customer name, phone | `Customer` | Yes | 24 months after last transaction/activity, then eligible for anonymization/deletion unless a legal/financial hold applies |
| Raw sale payloads | `IntegrationEvent.payload` | Often yes | 90 days, then purge/redact |
| WhatsApp message content/status | `WhatsAppMessage` | Indirectly | 90 days for message content/log detail; aggregate analytics retained separately |
| Feedback comments | `Feedback` | Possibly | 24 months after submission, then purge/anonymize |
| Google reviewer name/text | `GoogleReview` | Yes | 24 months after last sync/update, subject to provider/legal requirements |
| Audit logs | `AuditLog` | Potentially | 365 days minimum for V1 operational/security audit |

## Consent / Opt-Out

- Customers interact with the merchant, not directly with ReviewFlow.
- `Customer.opted_out = True` is permanent for that merchant unless the customer explicitly opts back in through a documented merchant process.
- Opt-out does not delete historical records automatically.

## Retention Rules

1. A daily purge job removes or redacts data whose V1 retention window has elapsed.
2. Raw inbound payloads are the shortest-lived operational data because they may contain duplicated PII from external systems.
3. WhatsApp content/log detail is retained only as long as needed for support, delivery troubleshooting, and audit; aggregate metrics are not dependent on retaining raw message content.
4. Financial records and subscription/payment records follow their applicable accounting/tax/legal retention requirements and are not deleted solely by the generic customer-data purge.
5. A legal/dispute hold prevents deletion of records covered by the hold.

## Customer / Merchant Deletion Request

Deletion requests create an audited request record. The system identifies all tenant-owned customer data, applies legal/financial holds, anonymizes or deletes eligible PII, and preserves only the minimum information required for immutable financial/audit records. The deletion job is idempotent and records completion/failure in `AuditLog`.

## Logging Rules

- Never log raw phone numbers, OAuth tokens, access tokens, message bodies, or raw inbound payloads.
- `QRScanEvent.ip_hash` may be stored only as a salted hash for abuse prevention; raw IP is never persisted.
