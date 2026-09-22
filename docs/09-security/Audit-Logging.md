# Audit Logging

## Purpose

`AuditLog` provides an accountability trail for privileged and administrative actions — both merchant-side (team management, billing changes) and platform-side (internal admin/support access to a merchant's data).

## Model

See `../03-database/Data-Dictionary.md` for the full field list: `merchant_id` (nullable for platform-level actions), `actor_user_id`, `action`, `target_type`/`target_id` (polymorphic reference), `metadata_json`, `created_at`.

## What Gets Logged

**Merchant-side (always logged):**
- Team member invited / role changed / removed
- Two-factor authentication enabled / disabled / a recovery code used
- Campaign activated / deactivated
- Integration connected / disconnected
- WhatsApp sender changed (own number ↔ shared pool)
- Google connection added / reauthorized
- Billing plan changed
- API key created / revoked

**Platform-side (always logged, and always via the privileged access path described in `../02-architecture/Multi-Tenancy.md`):**
- Any staff access to a specific merchant's data through the internal admin
- Any cross-tenant query executed through the privileged service path
- Merchant suspension / reactivation
- Manual quota adjustment

## What Does Not Get Logged Here

Routine, non-privileged reads (a merchant viewing their own dashboard) are not audit-logged — that would be noise, not accountability. `AuditLog` is for actions with lasting effect or actions crossing a trust boundary (platform staff touching merchant data).

## Retention

Audit logs are retained longer than the general data-retention policy in `Privacy-Data-Retention.md` covers — they are the record that lets a future dispute or security review be resolved. V1 operational retention is 365 days minimum, subject to longer applicable legal/accounting/security holds.

## Access to Audit Logs

Read-only, via the internal admin (Django Admin), staff-auth gated. Audit logs themselves are never editable or deletable through normal application code paths.
