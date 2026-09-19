# Security Controls Checklist

*Operational checklist. For the architectural view (auth flows, tenant isolation, threat model), see `../02-architecture/Security-Architecture.md`.*

## Encryption & Secrets

- [ ] OAuth tokens (Google, Meta) encrypted at rest via Fernet, key sourced from a KMS/secret manager — never hardcoded, never committed.
- [ ] Integration credentials (`Integration.credentials_encrypted`) encrypted at rest.
- [ ] All environment secrets (`DJANGO_SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `META_APP_SECRET`, `GOOGLE_OAUTH_CLIENT_SECRET`, payment gateway secret, `FERNET_KEY`) live in the hosting provider's secret manager, injected as env vars, read once at settings load.

## Permissions & Access

- [ ] Every view enforces role-based permissions via a DRF permission class — never an inline `if` check scattered in view logic.
- [ ] No endpoint trusts a client-supplied `merchant_id`/`location_id` — always derived from the authenticated principal.
- [ ] API keys are scoped (per-merchant, per-permission) and individually revocable.
- [ ] No standing database role with `BYPASSRLS` or superuser privileges in normal application configuration — cross-tenant admin access goes through an explicit, audited privileged service path only.

## Rate Limiting & Abuse Prevention

- [ ] Per-API-key and per-IP rate limiting via Redis sliding window (DRF throttle classes).
- [ ] Per-merchant WhatsApp send-rate cap, independent of Meta's own limits, to contain a runaway integration bug.
- [ ] Input validation at the adapter boundary (`schemas.py`) rejects malformed payloads before normalization.

## Webhook Security

- [ ] Every inbound webhook endpoint verifies signature/secret and fails closed (rejects on missing/invalid signature — never "process anyway").
- [ ] A spike in invalid-signature attempts on any endpoint triggers an internal alert.
- [ ] Outbound webhooks (future) are HMAC-signed and timestamped to prevent replay.

## Identifiers

- [ ] All externally-exposed IDs (API responses, QR redirect codes) use UUIDs or hashids, not raw sequential integers, to prevent IDOR enumeration.

## File / Object Storage

- [ ] R2 holds only blobs (QR images, CSV imports, exports) — never queryable business records.
- [ ] Temporary upload files (CSV imports) are purged after processing.

## Row-Level Security

- [ ] RLS enabled on every tenant-owned table.
- [ ] `SET LOCAL app.current_merchant_id` executed inside each tenant database transaction (`transaction.atomic()`) before any tenant-scoped query — transaction-local, never session/connection-level, so the value cannot leak across pooled connections (see `../02-architecture/Multi-Tenancy.md` and `../FINAL-ARCHITECTURE-REVIEW.md` §8).
- [ ] RLS policies tested explicitly to fail a cross-tenant read/write (see `../10-development/Testing-Strategy.md`).

## Dependency & Code Security

- [ ] Automated dependency scanning (`pip-audit`/Dependabot) in CI.
- [ ] Pre-launch manual review pass: auth bypass attempts, IDOR checks, webhook fail-closed verification.

## Privileged Admin Operations

- [ ] Internal admin (Django Admin) gated behind staff-only auth + IP allowlist/VPN.
- [ ] Every privileged/cross-tenant action is recorded in `AuditLog` — see `Audit-Logging.md`.
