# Authentication

Three distinct authentication mechanisms are used, for three distinct callers. Never mix them: a dashboard session should never be accepted on the public API, and an API key should never be accepted on dashboard-only endpoints.

## 1. Dashboard Users (Next.js frontend)

- Django session auth, cookie-based, same-origin (via Cloudflare) with the Next.js frontend.
- CSRF protection required on all state-changing requests.
- Optional TOTP 2FA per user (RFC 6238, per-user opt-in only — a merchant cannot require it). Enrollment is `POST /auth/2fa/setup` + `POST /auth/2fa/confirm`, which returns 10 one-time recovery codes shown once. Login is two-step for an enrolled user: `POST /auth/login` checks the password and returns `{ totp_required: true }` without creating a session, then `POST /auth/login/totp` completes it with a TOTP or recovery code.
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
- **Explicit opt-in per endpoint and method.** An endpoint accepts API keys only if its view declares it (see `API-Specification.md`); API-key auth is never global and never inferred from the URL. A Bearer request on an opted-in endpoint is judged by the key alone — a session cookie on the same request is ignored, and an invalid key never falls back to the session. On any other endpoint the Bearer header is ignored and the request is judged by its session alone.
- **Point-in-time checks.** Key state (active) and merchant state (`ACTIVE`) are checked on every request. Revoking a key or suspending the merchant does not cancel a request that already authenticated; every subsequent request fails with the generic `401 invalid_api_key`.
- **`last_used_at` is best-effort** operational metadata, written at most once per minute per key in its own short transaction; a failure to write it never fails an otherwise valid request.
- **Per-IP limit** keys on the client IP as determined by DRF's `get_ident()` under the configured `NUM_PROXIES`, and is applied before the key lookup so invalid-key attempts are counted. Default rates: 600/min per key, 1200/min per IP, both environment-configurable.

## 3. Inbound Provider Webhooks

- Not authenticated via API key. Each provider's own signature scheme is verified inside that provider's adapter:
  - Shopify: `X-Shopify-Hmac-Sha256` header — base64 HMAC-SHA256 over the raw request body, keyed with the **platform** `SHOPIFY_CLIENT_SECRET` (the ReviewFlow Shopify app's own client secret), never a per-merchant secret. During a client-secret rotation, verification also accepts `SHOPIFY_CLIENT_SECRET_PREVIOUS`, so a delivery signed with either the current or the previous secret passes. After the HMAC, the receiver additionally requires `X-Shopify-Shop-Domain` to equal the integration's stored `shop_domain` — defense in depth only, since Shopify's HMAC covers the raw body, not headers. See `05-integrations/Shopify.md`.
  - Petpooja / GoFrugal: provider-specific signature header, verified per their documentation at implementation time.
  - Generic webhook: `X-ReviewFlow-Signature: sha256=<hex HMAC-SHA256>` over the raw request body, keyed with a **server-generated** secret (`whsec_...`) issued once at connect time and never accepted from the merchant.
  - Razorpay (billing): `X-Razorpay-Signature` header — hex HMAC-SHA256 over the **raw request body**, keyed with the platform `RAZORPAY_WEBHOOK_SECRET`, compared in constant time. With the secret unset, every request is rejected. The `x-razorpay-event-id` header is required and is unique per event; it is the dedup key (`UNIQUE(provider, provider_event_id)` on `BillingEvent`), so a re-delivered event is a no-op. Razorpay may deliver events more than once and out of order.
- The Razorpay billing webhook (`POST /api/v1/billing/webhooks/razorpay`) has no `Integration`. Its signature is verified **before any database read**; the merchant is then resolved by the pre-tenant `billing_ref_lookup` lookup (Multi-Tenancy.md §"Billing reference lookup"), keyed on the provider subscription id that ReviewFlow stored at checkout, never on anything else in the payload. A missing or invalid signature and a missing event id return the identical `401`.
- Every sale-capture receiver first identifies its `Integration` via the pre-tenant `integration_lookup` lookup (Multi-Tenancy.md §"Integration lookup"), keyed on the `{integration_id}` in the URL — not a secret, since the row is only used to fetch the signing secret and nothing is returned or stored before the signature verifies.
- **Fail closed**: an unknown/malformed integration id, a wrong-provider id, a `DISCONNECTED` integration and a missing/invalid signature all return the identical `401`, and nothing is stored for any of them, never processed.

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
