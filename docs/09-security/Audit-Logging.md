# Audit Logging

## Purpose

`AuditLog` provides an accountability trail for privileged and administrative actions — both merchant-side (team management, billing changes) and platform-side (internal admin/support access to a merchant's data).

## Model

See `../03-database/Data-Dictionary.md` for the full field list: `merchant_id` (nullable for platform-level actions), `actor_user_id`, `action`, `target_type`/`target_id` (polymorphic reference), `metadata_json`, `created_at`.

## What Gets Logged

**Merchant-side (always logged):**
- Team member invited / role changed / removed
- Manager location assignments changed
- Two-factor authentication enabled / disabled / a recovery code used
- Campaign activated / deactivated
- Integration connected / disconnected
- WhatsApp sender changed (own number ↔ shared pool)
- WhatsApp own number connected, disconnected or failed activation *(spec 08-whatsapp-embedded-signup ES-8, approved 2026-10-09)*:
  - `whatsapp.own_number_connected` (metadata `{ reconnect }` only);
  - `whatsapp.own_number_disconnected` (no metadata);
  - `whatsapp.own_number_activation_failed` (system event, no actor, no metadata).

  The target is the `WhatsAppAccount`. Metadata never contains an access token, authorization code, PIN, `phone_number_id` or `business_account_id`.
- Google connection added or reauthorized *(spec 09-google-business-profile, approved 2026-10-23)*:
  - `google.connection_added`;
  - `google.connection_reauthorized`.

  The target is the `GoogleConnection`; the actor is the user who started the connect attempt. No metadata is recorded: never a token, authorization code, ID token, `google_account_sub` or email. A connection moving to `NEEDS_REAUTH` is a system event and writes no audit row; it is logged as a structured warning carrying only the connection id and a fixed reason.
- Billing plan changed; checkout started; cancellation requested
- Plan-change replacement started, committed (downgrade) or abandoned (`billing.replacement_started`, `billing.replacement_committed`, `billing.replacement_abandoned`); and, for staff, a paid replacement on an ended subscription (`billing.replacement_paid_after_end`, system event, written once). Metadata holds plan names, the plan id and the kind or effect only: never a provider reference, payment id or PII
- Subscription state changed (activated, past due, recovered, cancelled, expired) and each dunning checkpoint reached — system events with no actor
- API key created / revoked

**Platform-side (always logged, and always via the privileged access path described in `../02-architecture/Multi-Tenancy.md`):**
- Any staff access to a specific merchant's data through the internal admin: `staff.merchant_data_viewed` *(spec 16-internal-admin)*. Written by `auditlog.services.staff_merchant_read` under that merchant's id (`merchant_id` = the selected merchant, actor = the staff user, target = the `Merchant`, metadata `{ resource }` only), and committed before any tenant data is read: if the insert fails, nothing is read.
- Staff (Django Admin) two-factor authentication *(spec 16-internal-admin, LC-1, approved 2026-10-13)*. These are platform rows (`merchant_id` NULL) written through `record_platform()` inside `platform_write_atomic("staff_auth")`, each in the same transaction as the change it records, so a failed audit insert rolls the change back:
  - `user.totp_enabled`: staff enrollment through `enroll_staff_totp`, no metadata;
  - `user.totp_recovery_code_used`: a recovery code accepted at admin login, metadata `{ recovery_codes_remaining }` only.

  A successful TOTP-code admin login and a failed admin login write no audit row (as on the dashboard). No row ever contains a code, secret, `otpauth` URI or recovery hash.
- Any cross-tenant query executed through the privileged service path
- Shared WhatsApp sender configured or changed (`whatsapp.shared_pool_configured`; a system event with `merchant_id` NULL, written only through the platform path; metadata is the status only, never a provider id)
- Merchant suspension / reactivation
- Manual quota adjustment

## What Does Not Get Logged Here

Routine, non-privileged reads (a merchant viewing their own dashboard) are not audit-logged — that would be noise, not accountability. `AuditLog` is for actions with lasting effect or actions crossing a trust boundary (platform staff touching merchant data).

## Retention

Audit logs are retained longer than the general data-retention policy in `Privacy-Data-Retention.md` covers — they are the record that lets a future dispute or security review be resolved. V1 operational retention is 365 days minimum, subject to longer applicable legal/accounting/security holds.

## Access to Audit Logs

Read-only, via the internal admin (Django Admin), staff-auth gated. Audit logs themselves are never editable or deletable through normal application code paths.
