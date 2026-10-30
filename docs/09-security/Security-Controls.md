# Security Controls Checklist

*Operational checklist. For the architectural view (auth flows, tenant isolation, threat model), see `../02-architecture/Security-Architecture.md`.*

## Encryption & Secrets

- [ ] OAuth tokens (Google, Meta) encrypted at rest via Fernet, key sourced from a KMS/secret manager — never hardcoded, never committed.
- [ ] Integration credentials (`Integration.credentials_encrypted`) encrypted at rest.
- [ ] All environment secrets (`DJANGO_SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `META_APP_SECRET`, `META_WEBHOOK_VERIFY_TOKEN`, `META_SHARED_POOL_ACCESS_TOKEN`, `GOOGLE_OAUTH_CLIENT_SECRET`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `FERNET_KEY`, `SHOPIFY_CLIENT_SECRET`, `SHOPIFY_CLIENT_SECRET_PREVIOUS`) live in the hosting provider's secret manager, injected as env vars, read once at settings load.
- [ ] Google Business Profile *(spec 09-google-business-profile)*: `GOOGLE_OAUTH_CLIENT_SECRET` (listed above) is the only secret. It is a **platform** credential (never per-merchant), read from the environment only, and never stored in the database, a log, an audit row or an API response. The other Google variables are configuration, not secrets: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_REDIRECT_URI`, `GOOGLE_INTEGRATION_ENABLED` (default `False`; an unset id, secret or redirect URI also fails closed with `503 google_not_configured`), `GOOGLE_OIDC_JWKS_URI` and the timing settings in `config/settings.py`. Access and refresh tokens are Fernet ciphertext at rest; the authorization code is Fernet ciphertext only until the exchange task takes it; the ID token is validated (signature, issuer, audience, expiry, nonce) and never stored.
- [ ] `SHOPIFY_CLIENT_SECRET`/`_PREVIOUS` are the ReviewFlow Shopify app's own **platform** credentials (never per-merchant) — used only to verify Shopify webhook HMACs and the OAuth callback `hmac`, and never stored in `Integration.credentials_encrypted`, `config_json`, a log or an audit row (05-integrations/Shopify.md).

- [ ] `META_APP_SECRET`, `META_WEBHOOK_VERIFY_TOKEN` and `META_SHARED_POOL_ACCESS_TOKEN` are **platform** credentials (never per-merchant) — read from the environment only, and never stored in the database, a log, an audit row or an API response. An unset `META_APP_SECRET` or verify token fails closed: the inbound webhook rejects every request.
- [ ] Merchant Meta credentials *(spec 08-whatsapp-embedded-signup, approved 2026-10-09)*:
  - **Business token** (one per merchant own number): stored only as Fernet ciphertext in `WhatsAppAccount.access_token_enc`, decrypted only inside the WhatsApp provider. Cleared on disconnect (this discards ReviewFlow's copy only; it does not revoke the grant in Meta). Never logged, audited, returned, or passed as a Celery task argument.
  - **Embedded Signup code and two-step PIN**: held only in request memory for the three onboarding calls allowed by the approved `Architecture.md` exception (ES-4), and never persisted anywhere.
  - **Embedded Signup state**: only its SHA-256 hash is stored (`EmbeddedSignupState`), bound to the merchant and user, expiring, and consumed exactly once in a short committed transaction before any Meta call. The plaintext state is never stored, logged or audited, and is never kept in Redis or the Django session.
  - Those code paths use Django's `sensitive_variables` and `sensitive_post_parameters` (`code`, `pin`).
  - No request-body logging, and no error reporter that captures frame locals unscrubbed.
- [ ] `RAZORPAY_KEY_SECRET` and `RAZORPAY_WEBHOOK_SECRET` are **platform** credentials (never per-merchant) — read from the environment only, and never stored in the database, a log, an audit row or an API response. `RAZORPAY_KEY_ID` is the public key id and is the only Razorpay value returned to the dashboard (to an OWNER, for Checkout). No card, UPI or payer contact data is stored by ReviewFlow.

## Permissions & Access

- [ ] Every view enforces role-based permissions via a DRF permission class — never an inline `if` check scattered in view logic.
- [ ] No endpoint trusts a client-supplied `merchant_id`/`location_id` — always derived from the authenticated principal.
- [ ] API keys are scoped (per-merchant, per-permission) and individually revocable.
- [ ] Billing endpoints are session only: OWNER may read and mutate billing, ADMIN may only read it, MANAGER and VIEWER have no billing access, and no API key can reach a billing endpoint.
- [ ] No standing database role with `BYPASSRLS` or superuser privileges in normal application configuration — cross-tenant admin access goes through an explicit, audited privileged service path only.

## Rate Limiting & Abuse Prevention

- [ ] Per-API-key and per-IP rate limiting via Redis sliding window (DRF throttle classes).
- [ ] Per-merchant WhatsApp send-rate cap, independent of Meta's own limits, to contain a runaway integration bug.
- [ ] Input validation at the adapter boundary (`schemas.py`) rejects malformed payloads before normalization.

## Webhook Security

- [ ] Every inbound webhook endpoint verifies signature/secret and fails closed (rejects on missing/invalid signature — never "process anyway").
- [ ] The Razorpay billing webhook verifies `X-Razorpay-Signature` (HMAC-SHA256 over the raw body) before any database read, rejects every request when `RAZORPAY_WEBHOOK_SECRET` is unset, and takes the merchant only from the stored provider subscription reference, never from the payload.
- [ ] A spike in invalid-signature attempts on any endpoint triggers an internal alert.
- [ ] WhatsApp opt-out reliability (amended 2026-10-12): a broker failure on a STOP answers `503` (never a silent `200`); a failed or exhausted opt-out fan-out logs an error-level event (`whatsapp.optout_enqueue_failed`, `whatsapp.optout_fanout_exhausted`) with counts and merchant ids only, never a phone number, and that log is alerted on.
- [ ] Outbound webhooks (future) are HMAC-signed and timestamped to prevent replay.

## Identifiers

- [ ] All externally-exposed IDs (API responses, QR redirect codes) use UUIDs or hashids, not raw sequential integers, to prevent IDOR enumeration.

## File / Object Storage

- [ ] R2 holds only blobs (QR images, CSV imports, exports) — never queryable business records.
- [ ] Temporary upload files (CSV imports) are purged after processing.

## Row-Level Security

- [ ] RLS enabled on every tenant-owned table.
- [ ] `SET LOCAL app.current_merchant_id` executed inside each tenant database transaction (`transaction.atomic()`) before any tenant-scoped query — transaction-local, never session/connection-level, so it cannot leak across pooled connections (see `../02-architecture/Multi-Tenancy.md` and `../FINAL-ARCHITECTURE-REVIEW.md` §8).
- [ ] RLS policies tested explicitly to fail a cross-tenant read/write (see `../10-development/Testing-Strategy.md`).

## Dependency & Code Security

- [ ] Automated dependency scanning (`pip-audit`/Dependabot) in CI.
- [ ] Pre-launch manual review pass: auth bypass attempts, IDOR checks, webhook fail-closed verification.

## Privileged Admin Operations

- [ ] Internal admin (Django Admin) gated behind staff-only auth + IP allowlist/VPN.
- [ ] Every privileged/cross-tenant action is recorded in `AuditLog` — see `Audit-Logging.md`.
