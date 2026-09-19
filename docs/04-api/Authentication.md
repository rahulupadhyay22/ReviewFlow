# Authentication

Three distinct authentication mechanisms are used, for three distinct callers. Never mix them: a dashboard session should never be accepted on the public API, and an API key should never be accepted on dashboard-only endpoints.

## 1. Dashboard Users (Next.js frontend)

- Django session auth, cookie-based, same-origin (via Cloudflare) with the Next.js frontend.
- CSRF protection required on all state-changing requests.
- Optional TOTP 2FA per user.
- Login: `POST /auth/login` (see `API-Specification.md`).
- **Why session over JWT for V1**: simpler to reason about revocation and CSRF is a well-understood pattern; revisit only if a fully-decoupled mobile app is planned (see the blueprint's "Decisions I Made Without Asking").

## 2. Public API (external systems, scripts, custom integrations)

- `Authorization: Bearer rf_live_<random32>` header.
- Keys are shown once at creation; stored server-side only as a `sha256` hash — never recoverable.
- Each key is:
  - **Scoped** to exactly one `Merchant`.
  - **Permission-scoped** (e.g. `sales:write`, `reviews:read`, `transactions:read`).
  - **Individually revocable** without affecting other keys.
  - **Rate-limited** independently (Redis sliding window; see `../02-architecture/Security-Architecture.md`).
- Every request derives `merchant_id` from the key — never from a request body/query parameter.

## 3. Inbound Provider Webhooks

- Not authenticated via API key. Each provider's own signature scheme is verified inside that provider's adapter:
  - Shopify: `X-Shopify-Hmac-SHA256` header, verified against the integration's stored webhook secret.
  - Petpooja / GoFrugal: provider-specific signature header, verified per their documentation at implementation time.
  - Generic webhook: a shared secret configured per `Integration` at connection time.
- **Fail closed**: missing/invalid signature → `401`, never processed.

## 4. OAuth (Google, Meta)

- Standard OAuth 2.0 authorization-code flow for both.
- Google: scopes limited to what's needed for location listing + review read/reply (`business.manage` or the specific GBP API scopes).
- Meta: Embedded Signup flow for merchant-owned WhatsApp numbers.
- Tokens (`access_token`, `refresh_token`) encrypted at rest (Fernet, KMS-backed key).
- Refresh handled proactively by a scheduled Celery Beat task (`refresh_google_tokens`, daily) — not reactively only on failure — to avoid a customer-facing gap when a token silently expires.
- On refresh failure or explicit revocation: `GoogleConnection.status = NEEDS_REAUTH`, surfaced as a dashboard banner; message sending is unaffected (decoupled from Google sync).

## Session vs. API Key — Quick Reference

| | Dashboard Session | API Key |
|---|---|---|
| Used by | Next.js frontend, logged-in humans | External systems, scripts |
| Credential | Session cookie + CSRF token | `Authorization: Bearer` header |
| Scope | Full role-based permissions for the logged-in user | Explicit per-key scopes |
| Revocation | Logout / session expiry | Individually revocable |
| Rate limit | Standard per-user throttle | Per-key throttle, independently configurable |
