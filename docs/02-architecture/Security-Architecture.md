# Security Architecture

*Architectural view of authentication, authorization, and the tenant/threat model. For the operational controls checklist, retention policy, and audit-log spec, see `../09-security/`.*

## Authentication

### Dashboard Users
Django auth (email/password), session-based for the Next.js frontend, with CSRF protection. Optional TOTP 2FA. Chosen over JWT for V1 simplicity, assuming a same-origin-ish frontend/API relationship behind Cloudflare.

```
User -> Session -> CSRF -> authenticated request
```

### Public API
API key in `Authorization: Bearer <key>` header.

```
API Key -> sha256 hash lookup -> scoped to one Merchant -> merchant context set -> request processed
```

Keys are shown once at creation, stored only as a hash, scoped (`sales:write`, `reviews:read`, etc.), individually revocable and rate-limited.

### Inbound Provider Webhooks
Verified per-provider (Shopify HMAC header, Petpooja's own scheme, etc.) inside each adapter's `verify()` — never by API key.

```
Webhook -> signature verification -> integration identified from URL/payload -> IntegrationEvent
```

Fails closed: missing or invalid signature is always rejected, never "processed anyway."

### Outbound Webhooks (future)
HMAC-SHA256 signed with a per-`WebhookEndpoint` secret, timestamped to prevent replay, retried with backoff on non-2xx.

### OAuth (Google, Meta)
Standard OAuth 2.0 authorization-code flow. Tokens encrypted at rest (Fernet, key from a KMS/secret manager — never hardcoded). Refreshed proactively before expiry via scheduled Celery Beat task, not reactively on failure.

## Authorization

Role-based, enforced via DRF permission classes composed per-view (never scattered ad hoc in view code):

```
OWNER   — billing, delete merchant, manage all locations, manage team
ADMIN   — manage locations, integrations, campaigns; no billing/delete
MANAGER — campaigns for assigned locations only
VIEWER  — read-only dashboard access
```

`MANAGER` location assignment is stored via the explicit `TeamMemberLocation` through-model (not a default M2M), which carries its own `merchant_id` and is rejected at creation if it would cross tenant boundaries — see `Multi-Tenancy.md`.

## Multi-Tenancy Isolation

Summarized here; full detail in `Multi-Tenancy.md`. Two independent layers: `TenantScopedManager` (application) + Postgres RLS (database), with no standing RLS-bypass role anywhere in normal application configuration.

## Threat Model Highlights

- **Cross-tenant data leak** → mitigated by the two isolation layers above; tested explicitly (see Testing Strategy).
- **Cross-tenant ID collision on ingestion** → `IntegrationEvent.merchant_id` is resolved from `Integration` before the row is created (never null, never resolved later), and idempotency is scoped to `(integration_id, external_event_id)` rather than the bare provider name — see `Multi-Tenancy.md`.
- **Cross-tenant team/location assignment** → `TeamMemberLocation` is an explicit through-model with a service-layer three-way merchant match invariant, not a default M2M relying on read-time permission checks alone.
- **Duplicate/replayed webhooks causing duplicate sends** → `(integration_id, external_event_id)` on `IntegrationEvent` (revised from `(source, external_event_id)` — see `Multi-Tenancy.md`) and `(campaign_id, transaction_id)` on `CampaignExecution`, both enforced at the database, not just in application logic, with the latter additionally backed by a transactional row lock (see `../06-automation/Campaign-Engine.md`).
- **IDOR on externally-exposed IDs** → recommend UUIDs/hashids for any ID exposed in the public API or QR redirect URLs, not raw sequential integers.
- **Google/Meta token revocation** → surfaced via a dashboard reconnect banner, decoupled from message sending so a broken sync never blocks the core send loop.
- **Runaway integration bug** → a per-merchant WhatsApp send-rate cap, independent of Meta's own limits.

## Encryption & Secrets

- OAuth tokens and provider credentials encrypted at rest (Fernet/KMS-backed).
- Environment-specific secrets (`DJANGO_SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `META_APP_SECRET`, `GOOGLE_OAUTH_CLIENT_SECRET`, payment gateway secret, `FERNET_KEY`) live in the hosting provider's secret manager, never committed, read once at settings load.
