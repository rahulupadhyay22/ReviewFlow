# API Specification

Base path: `/api/v1/`. All endpoints require authentication (session or API key — see `Authentication.md`). `merchant_id` is always derived from the authenticated principal, never from a request parameter.

**Conventions used below for every endpoint:** Method, URL, Auth, Permissions, Request, Response, Validation, Errors, Pagination, Rate limits.

---

## Auth

### `GET /auth/login`
- **Auth**: none
- **Response**: `204`; sets the `csrftoken` cookie so the frontend can send `X-CSRFToken` on its first POST

### `POST /auth/login`
- **Auth**: none; CSRF required
- **Request**: `{ email, password }`
- **Response, no TOTP enrolled**: `200 { user: { id, email, totp_enabled }, merchant: { id, name }, role }` and sets the session cookie
- **Response, TOTP enrolled**: `200 { totp_required: true }`. The password and membership were already checked, exactly as above, but the session is **not** logged in — it holds only a short-lived (5 minute), unauthenticated pending marker. Complete the login with `POST /auth/login/totp`.
- **Errors**: `401` invalid credentials (one generic response for every failure reason, including for an enrolled user), `403` missing CSRF token, `429` too many attempts (per-IP)

### `POST /auth/login/totp`
- **Auth**: none (the pending marker from `POST /auth/login` only); CSRF required
- **Request**: `{ code }` — a 6-digit TOTP code, or a recovery code
- **Response**: `200` — same body shape as a successful `POST /auth/login`, and sets the session cookie
- **Errors**: `401 invalid_credentials` — one generic response for every failure reason (no pending marker, expired marker, attempt cap reached, wrong or replayed code, membership revoked or merchant suspended since step 1, 2FA disabled since step 1); `403` missing CSRF token; `429` too many attempts (per-IP). After 5 wrong codes the pending marker is discarded and `POST /auth/login` must be repeated.

### `POST /auth/2fa/setup`
- **Auth**: session required; all roles (2FA is per user, not per merchant)
- **Request**: `{ password }`
- **Response**: `200 { secret, otpauth_uri }` — scan `otpauth_uri` as a QR code, or enter `secret` manually. Calling this again before confirming replaces the pending secret.
- **Errors**: `400 reauthentication_failed` (wrong password); `409 totp_already_enabled`; `429` too many attempts (per user)

### `POST /auth/2fa/confirm`
- **Auth**: session required; all roles
- **Request**: `{ code }` — a 6-digit TOTP code
- **Response**: `200 { recovery_codes: [ "xxxxx-xxxxx-xxxxx-xxxxx", … 10 ] }` — shown once; there is no way to retrieve them again
- **Errors**: `400 invalid_totp_code`; `409 totp_already_enabled`; `409 totp_setup_required` (no pending setup); `429` too many attempts

### `POST /auth/2fa/disable`
- **Auth**: session required; all roles
- **Request**: `{ password, code }` — `code` is a TOTP or recovery code
- **Response**: `204`
- **Errors**: `400 reauthentication_failed` — one generic response for a wrong password **or** a wrong/replayed code; `409 totp_not_enabled`; `429` too many attempts

### `POST /auth/logout`
- **Auth**: session required
- **Response**: `204`

### `POST /auth/refresh`
- **Auth**: session required (if using short-lived session tokens)
- **Response**: `200` refreshed session state (same body as login); the session expiry is extended

### `POST /auth/accept-invite`
- **Auth**: none; CSRF required
- **CSRF bootstrap**: the invitee has no session, so the frontend calls `GET /auth/login` first to obtain the `csrftoken` cookie, then sends it as `X-CSRFToken` here. This does **not** authenticate the invitee or create a login session — it only issues the CSRF cookie, exactly as it does for the login page.
- **Invite link transport**: `POST /team-members` returns an `invite_token`. The frontend builds the invite link as a URL **fragment**, never a query parameter — `https://<frontend>/accept-invite#token=<invite_token>` — so the token is never sent to the server in a navigation request and never appears in server, CDN or WAF access logs. The accept-invite page reads `location.hash`, removes it from the address bar (`history.replaceState`) **before** any analytics, telemetry, error-reporting or other third-party script can observe the URL, and sends the token only in this endpoint's JSON body — never as a query parameter or header, and never to analytics/telemetry/error-reporting. Invite links are `https://` only; production serves the frontend and API over HTTPS only.
- **Request**: `{ token, password }`
- **Response**: `204`. The invitee then logs in through `POST /auth/login`.
- **Password rule**: if the invited email has no usable password yet (a brand-new person, or an existing `User` row created by another still-pending invite that was never accepted), `password` sets it, validated by Django's password validators. If the email already has a usable password (an existing account), `password` must be that account's **current** password — accepting an invite never changes an existing password. This prevents an OWNER/ADMIN of any merchant from taking over a known email's existing account via an invite link.
- **Errors**: `400 invalid_invite` — one generic response for every failure reason (bad/tampered signature, expired token, already accepted, superseded by a re-invite, revoked, merchant not `ACTIVE`, or wrong current password for an existing account); `422` only when a new/uninitialized account's password fails validation; `403` missing CSRF token; `429` too many attempts (per-IP)

---

## Merchant

### `GET /merchant`
- **Auth**: session or API key
- **Permissions**: any role (session); any active API key, no scope required
- **Response**: `200 { id, name, business_type, timezone, plan, status }`
- **`plan`**: derived from the merchant's `Subscription` — `{ id, name }` of `Subscription.plan` while the subscription is `ACTIVE` or `PAST_DUE`, otherwise `null`. `Merchant` has no plan column.
- **Note**: no-scope API-key access here is the Phase-05 compatibility consumer only. It does not mean every future key-accepting endpoint is reachable without a scope: each one names its required scope (e.g. `POST /sales` → `sales:write`).

### `PATCH /merchant`
- **Permissions**: OWNER, ADMIN
- **Request**: partial `{ name?, business_type?, timezone? }`
- **Validation**: `timezone` must be a valid IANA name
- **Errors**: `403` insufficient role, `422` validation

---

## Locations

Location body: `{ id, name, address, phone, timezone, is_active, created_at, updated_at }`.

### `GET /locations`
- **Permissions**: any role (VIEWER read-only; MANAGER sees only assigned locations)
- **Pagination**: cursor-based, default page size 25, `?cursor=`/`?limit=` (max 100)
- **Response**: `200 { results: [Location], next_cursor }`. `next_cursor` is `null` on the last page.
- Lists both active and inactive locations, each with its `is_active` value. There is no `?is_active` filter.

### `POST /locations`
- **Permissions**: OWNER, ADMIN
- **Request**: `{ name, address?, phone?, timezone? }`
- **Response**: `201` with the Location body. `is_active` is always `true` on create.
- **Errors**: `422` missing/blank `name`, an invalid IANA `timezone`, or an over-length field
- Any `merchant_id`, `id` or `is_active` in the request body is ignored.

### `GET /locations/{id}`
- **Errors**: `404` if not found, belongs to another merchant, or (for a MANAGER) is not assigned to the caller — never `403` in any of these cases, so a response never reveals cross-tenant existence or another team member's assignment scope

### `PATCH /locations/{id}`
- **Permissions**: OWNER, ADMIN, or MANAGER assigned to this location
- **Request**: partial `{ name?, address?, phone?, timezone? }`. `address`, `phone` and `timezone` accept `null` to clear the value. `is_active` is not writable here.
- **Response**: `200` with the Location body.
- **Errors**: `404` as for `GET /locations/{id}` — a MANAGER not assigned to this location gets `404`, not `403`; `422` for validation errors

### `DELETE /locations/{id}`
- **Permissions**: OWNER, ADMIN
- **Behavior**: soft-delete (`is_active = False`); does not cascade-delete historical transactions/executions, and does not delete or change any `TeamMemberLocation` assignment
- **Response**: `204`. Idempotent — deleting an already-inactive location returns `204` again and changes nothing.
- There is no reactivation endpoint.

---

## Transactions

### `GET /transactions`
- **Filters**: `location_id`, `date_from`, `date_to`, `status`
- **Pagination**: cursor-based
- **Rate limits**: standard per-key limit (see Authentication.md)

### `GET /transactions/{id}`
- **Response**: includes linked `CampaignExecution` summary if one exists

---

## Campaigns

### `GET /campaigns`
- **Filters**: `location_id`, `is_active`

### `POST /campaigns`
- **Permissions**: OWNER, ADMIN, MANAGER (own locations)
- **Request**: `{ location_id, mode, delay_minutes, frequency_cap_days, message_template_id }`
- **Validation**: activation blocked (see below) until prerequisites are met

### `PATCH /campaigns/{id}`
- **Validation on activation (`is_active: true`)**: rejects with `422` unless — a Google location is connected or explicitly skipped, a WhatsApp sender is configured, and at least one `APPROVED` template exists. This is a service-layer rule, not just a UI check.

### `GET /campaigns/{id}/executions`
- **Filters**: `status`, `date_from`, `date_to`
- **Response**: execution history rows (customer, transaction, scheduled/sent/delivered/read timestamps, status)

---

## Reviews & Feedback

### `GET /reviews`
- **Filters**: `location_id`, `date_from`, `date_to`, `rating_min`
- **Response**: synced `GoogleReview` rows — never includes an attribution field (see `../01-product/Business-Rules.md` §8)

### `GET /feedback`
- **Filters**: `location_id`, `date_from`, `date_to`

---

## Sales (generic ingestion)

### `POST /sales`
- **Auth**: API key with `sales:write` scope. Session requests get `403` (no fallback).
- **Request**: normalized `SaleCreated` shape (see `Webhook-Specification.md`). `merchant_id`, `location_id`, `source` and `event` in the body are ignored.
- **Response** (Phase 06 spec Decision 8):
  - `201 { transaction_id, event_id }` — newly created, and the event ended `PROCESSED`.
  - `200 { transaction_id, event_id }` — a replay of an already-`PROCESSED` event.
  - `202 { event_id, event_status, transaction_id: null }` — the event is persisted (`RECEIVED` or `FAILED`); the transaction is not yet available. `retry_failed_events` recovers it; replay later for the final result.
  - `409 { error: { code: "sale_not_processed", message }, event_id, event_status }` — the event is terminal (`DEAD_LETTER` or `CANCELLED`) and will never produce a transaction.
- **Validation**: `external_transaction_id` required. Customer phone is optional: a missing or blank phone is a valid sale and creates a `Transaction` with `customer = null` (no `Customer` row). A phone that is supplied must be valid E.164; a supplied invalid phone is rejected with `422` (the safe error code, lowercased, e.g. `invalid_phone`). `422 location_unresolved` if the sale can't be mapped to a location. `422 integration_not_connected` if the merchant has no `CONNECTED` Generic REST API integration.
- **Idempotency**: repeating the same `(external_location_id, external_transaction_id)` pair is a no-op at the inbox (`UNIQUE(integration_id, external_event_id)`), and `(location_id, external_transaction_id)` backstops it at the transaction level.

### `GET /sales`
- **Auth**: API key with `transactions:read` scope
- Same filters/pagination/body as `/transactions`, but merchant-wide (not MANAGER-location-scoped: a key has no `TeamMember`)

---

## QR Codes

### `POST /qrcodes`
- **Request**: `{ location_id, name, destination_type, destination_url }` — `destination_type` is `GOOGLE_REVIEW` or `FEEDBACK_PAGE`
- **Response**: `201 { id, name, destination_type, redirect_url }`

### `GET /qrcodes`
- **Filters**: `location_id`, `active`
- **Response**: does not include a scan count inline (see `/qrcodes/{id}/scans`) — scan totals are derived from `QRScanEvent`, not stored on `QRCode`

### `PATCH /qrcodes/{id}`
- **Request**: partial `{ name?, active?, destination_url? }`

### `GET /qrcodes/{id}/scans`
- **Filters**: `date_from`, `date_to`
- **Response**: aggregate scan count for the period, plus (if requested) the underlying `QRScanEvent` rows — never a raw IP and never exposes `ip_hash` to merchant-facing API consumers; the hash is internal-only for abuse prevention/deduplication
- **Reporting note**: a scan count is never presented as equivalent to, or reliably attributable to, a Google review (see `../01-product/Business-Rules.md` §8)

---

## Team

Member body: `{ id, user: { id, email }, role, invited_at, accepted_at, location_ids }`.
`location_ids` is a list of assigned `Location` ids (read from
`TeamMemberLocation`) and is `[]` for every non-`MANAGER` role. A `MANAGER`
starts with no location assignment until an OWNER/ADMIN sets one with
`PUT /team-members/{id}/locations` below.

### `GET /team-members`
- **Permissions**: OWNER, ADMIN only
- **Response**: `200 { results: [member] }`, pending and accepted members, ordered by creation time. No pagination.

### `POST /team-members` (invite)
- **Permissions**: OWNER, ADMIN only
- **Request**: `{ email, role }`
- **Response**: `201 { ...member, invite_token }`. `invite_token` is a credential — see `Authentication.md` §1 and the transport rules under `POST /auth/accept-invite` above.
- **Behavior**: creates the `User` (with an unusable password) if the email is new. Re-inviting a still-pending member refreshes `role` and `invited_at` and returns a new `invite_token`; the previous token then fails with `400 invalid_invite`.
- **Errors**: `409 already_member` if the email is already an accepted member of this merchant; `403 team_permission_denied` if a non-OWNER actor invites with `role: OWNER`; `422` for an invalid email or role

### `PATCH /team-members/{id}` (role change)
- **Permissions**: OWNER, ADMIN only
- **Request**: `{ role }`
- **Response**: `200` with the member body
- **Rules**: nobody can change their own role. Only an OWNER can change an OWNER's role or grant the OWNER role. The last accepted OWNER is protected by these rules: self role change and self revoke are `403`, and only an accepted OWNER can target another OWNER, so an OWNER always remains. There is no reachable `409` for this. A role change **away from** `MANAGER` deletes the member's `TeamMemberLocation` assignments in the same transaction; a later change back to `MANAGER` starts with `location_ids: []`.
- **Errors**: `404` if `{id}` is not a member of the current merchant (never `403`, to avoid revealing cross-tenant existence); `403 team_permission_denied` for the self/OWNER rules above; `422` for an invalid role

### `DELETE /team-members/{id}` (revoke)
- **Permissions**: OWNER, ADMIN only
- **Response**: `204`. Deletes the `TeamMember` row (pending or accepted); the underlying `User` is kept.
- **Rules**: same self/OWNER rules as the role change above, which also protect the last OWNER.
- **Errors**: `404`, `403 team_permission_denied` — same meanings as `PATCH` above

### `PUT /team-members/{id}/locations` (set assigned locations, target must be MANAGER)
- **Permissions**: OWNER, ADMIN only
- **Request**: `{ location_ids: [uuid, ...] }`
- **Behavior**: replaces the member's assignment set atomically (not a merge). An empty list clears it; duplicate ids are collapsed. The target may be pending (not yet accepted). Inactive locations may be assigned.
- **Response**: `200` with the member body, including the updated `location_ids`.
- **Validation**: every `location_id` must belong to the same merchant as the team member — rejected with `422` otherwise (this is the API-level surface of the `TeamMemberLocation` invariant in `../02-architecture/Multi-Tenancy.md`). An unknown `location_id` and a cross-merchant `location_id` return the identical `422` body, so a response never reveals whether a location exists in another merchant.
- **Errors**: `403 team_permission_denied` for MANAGER/VIEWER actors; `404` if `{id}` is not a member of the current merchant (never `403`); `422` if the target's role is not `MANAGER`, if any `location_id` is unknown/cross-merchant, or for a malformed UUID

---

## API Keys

Key body: `{ id, scopes, is_active, created_at, last_used_at }`. Never includes the key hash. All three endpoints are **session only** (+ CSRF on writes) — an API key can never list, create, or revoke keys.

### `GET /api-keys`
- **Permissions**: OWNER, ADMIN
- **Pagination**: cursor-based, `?cursor=`/`?limit=` (max 100)
- **Response**: `200 { results: [key], next_cursor }`, active and revoked keys, newest first

### `POST /api-keys`
- **Permissions**: OWNER, ADMIN
- **Request**: `{ scopes: [ ... ] }` — non-empty, distinct, each one of `sales:write`, `reviews:read`, `transactions:read`
- **Response**: `201 { ...key, key: "rf_live_..." }` — the plaintext `key` is returned only in this response; it is stored only as a sha256 hash and can never be retrieved again
- **Errors**: `403` insufficient role or missing CSRF; `422` missing/empty scopes, an unknown scope, or duplicates
- Any `merchant_id`, `id`, `is_active` or key hash in the body is ignored.

### `DELETE /api-keys/{id}` (revoke)
- **Permissions**: OWNER, ADMIN
- **Response**: `204`. Idempotent — revoking an already-revoked key returns `204` and changes nothing.
- **Behavior**: affects authentication of subsequent requests; a request already authenticated with the key is not cancelled.
- **Errors**: `404` if not found or belongs to another merchant (never `403`)

### Public-API authentication errors
On every endpoint that accepts an API key: `401` with `WWW-Authenticate: Bearer` and `{ error: { code: "invalid_api_key", message } }` — one generic response for a malformed, unknown, or revoked key, or a key whose merchant is not `ACTIVE`. Rate-limited requests return `429` with `Retry-After`.

---

## Integrations

### `POST /integrations/{provider}/connect`
- OAuth-style or credential-based connection flow, provider-specific — see `../05-integrations/Integration-Architecture.md`
- Integration is created at merchant level; locations are attached through `IntegrationLocationMapping`.
- Phase 06 providers:
  - `webhook` (Generic Webhook): rejects client-supplied `credentials` with `422`; requires `config_json.field_map` (§Generic-Webhook.md). The server generates a `whsec_...` signing secret and returns it **once**, as `webhook_secret` in this `201` response only — never again.
  - `csv` (CSV Import), `api` (Generic REST API): reject any `credentials` with `422`. `api` allows at most one `CONNECTED` integration per merchant; connecting a second returns `409 integration_exists`.
  - `shopify`: **always** returns `400` in the standard `{ error: { code: "validation_error", message, field_errors: { provider: [...] } } }` shape, whatever the request body — the OAuth install/link flow below is the only way to create a Shopify `Integration` (06-shopify-app spec O2, user decision 2026-09-29). Nothing is created, and no Shopify credential from the body is ever read or stored.
  - `woocommerce`, `petpooja`, `gofrugal`, `zapier`, `make`: unregistered in this phase — `422`.

### Shopify app installation (06-shopify-app; see `../05-integrations/Shopify.md`)
No endpoint accepts a merchant-entered shop domain (`shop` comes only from Shopify's own redirect).
- `GET /integrations/shopify/install`: unauthenticated, pre-tenant, per-IP throttled. `shop` (query) must match `^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$`, else `400 shopify_install_invalid`. Stores a single-use, 10-minute OAuth `state` in the session and returns `302` to Shopify's own `/admin/oauth/authorize`.
- `GET /integrations/shopify/callback`: Shopify's OAuth redirect. Verifies the documented `hmac`/`state`/`shop` checks, exchanges the code, and on success stores an encrypted, single-use, 15-minute pending installation in the session, then redirects (`302`) to the configured `SHOPIFY_LINK_PAGE_URL` — with no query string or fragment appended. Creates no `Integration`. Failures: `400 shopify_install_invalid` or `502 shopify_unavailable`.
- `GET /integrations/shopify/pending`: session, OWNER/ADMIN. `200 { shop }`, or `404` if nothing is pending or it has expired.
- `POST /integrations/shopify/link`: session + CSRF, OWNER/ADMIN, empty body (any `shop`/`merchant_id`/`credentials` is ignored). Consumes the pending installation, resolves the shop identity, creates the `Integration` under the session's merchant, and registers the `orders/paid`/`app/uninstalled` webhook subscriptions. `201` with the Integration body, `409 shopify_install_expired` if nothing valid is pending, or `502 shopify_unavailable` if Shopify's API fails (nothing is left behind).
- `PATCH /integrations/{id}` on a `shopify` integration always returns `422` — its `config_json` (`shop_domain`, `shop_id`, `webhook_subscription_ids`) is entirely server-managed.

### `GET /integrations`
- Returns merchant-level integrations. Location mappings are returned separately or embedded as mapping summaries. Never includes `webhook_secret` or any other credential.

### `POST /integrations/{id}/locations`
- **Request**: `{ location_id, config_json?, is_active? }`
- **Validation**: integration and location must belong to the authenticated merchant; duplicate `(integration_id, location_id)` returns `409`.

### `PUT /integrations/{id}/locations`
- **Request**: `{ mappings: [{ location_id, config_json?, is_active? }] }`
- Replaces the authenticated merchant's mapping set atomically.

### `PATCH /integrations/{id}` (update merchant-level `config_json`)
- Provider-specific validation applies (e.g. `webhook`'s `field_map` rules).

### `DELETE /integrations/{id}` (disconnect; mappings remain as historical configuration but become inactive; the integration's pending `RECEIVED`/`FAILED` events become `CANCELLED` in the same transaction and are never processed)

### `POST /integrations/{id}/csv-imports` (Phase 06 spec Decision 10)
- **Auth**: session, OWNER/ADMIN. **Request**: `multipart/form-data`, field `file`.
- The whole file is validated synchronously (required columns, every row) before anything is stored; an invalid file returns `422` with `field_errors: { "row_<n>": [<safe code>], ... }` (first 50 bad rows).
- A valid file is staged in Cloudflare R2 under a server-built key and returns `202 { rows }`; a background task records one `IntegrationEvent` per row and deletes the staged file once every row is recorded.

---

## Billing

Full rules: `../08-billing/Billing-Specification.md` (§N for the provider integration).

**Roles.** OWNER has full billing access (reads and mutations). ADMIN has read-only billing access and can perform no billing mutation. MANAGER and VIEWER have no billing access (`403` on every billing endpoint).

- *Billing read*: `GET /billing/plans`, and `GET /billing/subscription` without its `checkout` object.
- *Billing mutation*: `POST /billing/checkout`, `POST /billing/subscription/cancel`, and opening Razorpay Checkout (which needs the `checkout` object, returned only to an OWNER).

All four dashboard endpoints are session only; none accepts an API key. `merchant_id` comes from the session. Any `merchant_id`, `status`, provider id or price in a request body is ignored.

### `GET /billing/plans`
- **Auth**: session
- **Permissions**: OWNER, ADMIN
- **Response**: `200 { results: [ { id, name, monthly_price, currency, quota_requests, features } ], next_cursor }` — plans with `is_active = true`, ordered by `monthly_price`. Never includes `provider_plan_id`.
- **Pagination**: cursor (`?cursor=`, `?limit=` max 100)

### `GET /billing/subscription`
- **Auth**: session
- **Permissions**: OWNER, ADMIN
- **Behavior**: reads local state only; it never calls Razorpay.
- **Response**: `200`
  ```
  {
    status: "INCOMPLETE" | "ACTIVE" | "PAST_DUE" | "CANCELLED" | "EXPIRED" | null,
    plan: { id, name, monthly_price, currency, quota_requests, features } | null,
    pending_plan: { ...same shape } | null,
    current_period_start, current_period_end,
    cancel_at_period_end: bool,
    past_due_at, grace_ends_at, dunning_stage,
    usage: { requests_used, quota_requests, requests_remaining } | null,
    can_send: bool,
    checkout: { provider: "razorpay", key_id, subscription_id, card_change: bool } | null,
    next_action: { type, at } | null,
    replacement: { target_plan: { id, name }, kind: "UPGRADE" | "DOWNGRADE", authorized: bool, committed: bool, effective_at } | null
  }
  ```
- `status: null` means the merchant has no subscription.
- `replacement` is the pending plan-change replacement (`Billing-Specification.md` §N), or `null`. It is read from local state only and never shows a provider reference. `committed` is true once a downgrade replacement has passed its commit point; `authorized` is derived as equal to `committed`. `effective_at` is the old `current_period_end` for a downgrade (when it takes over) and `null` for an upgrade (it takes effect when it is proven paid). The old `plan`, `can_send` and `usage` are unchanged until the switch.
- `can_send` is true only for `ACTIVE`, inside the current period, with quota remaining.
- `past_due_at`, `grace_ends_at` and `dunning_stage` (`0`, `3` or `6`) are null unless `PAST_DUE`.
- `checkout` is returned **only to an OWNER** (always `null` for an ADMIN), and only for `INCOMPLETE` (`card_change: false`) and for `PAST_DUE` while the provider status is `pending` (`card_change: true`, passed to Razorpay Checkout as `subscription_card_change`).
- `next_action.type`: `SUBSCRIBE` (no subscription, `CANCELLED`, `EXPIRED`); `COMPLETE_CHECKOUT` (`INCOMPLETE`); `UPDATE_PAYMENT_METHOD` (`PAST_DUE`, provider status `pending`); `RESUBSCRIBE` (`PAST_DUE`, any other provider status: the missed invoice can no longer be paid, so the OWNER cancels and checks out again); `CANCELLATION` (`cancel_at_period_end`); `PLAN_CHANGE` (`pending_plan`); otherwise `RENEWAL`.

### `POST /billing/checkout`
- **Auth**: session + CSRF
- **Permissions**: OWNER
- **Rate limit**: `billing_write`, 10/min per user
- **Request**: `{ plan_id, acknowledge_no_credit? }` — an active plan with a Razorpay plan id. Anything else is `422`. `acknowledge_no_credit` (boolean, default false) matters only when an upgrade falls back to a plan-change replacement (`Billing-Specification.md` §N); it is ignored and not recorded when the provider update succeeds.
- **Behavior**: creates/updates the Razorpay subscription/payment flow; provider references are stored server-side. If a provider reference is already stored, the request first reconciles it with Razorpay and then routes on the resulting state (`Billing-Specification.md` §N, "Checkout reconciliation"):

  | State after the reconcile | Result |
  |---|---|
  | no subscription, `CANCELLED`, `EXPIRED` | create a provider subscription; status `INCOMPLETE` (a replacement starts a new lifecycle — see below); `201` with `checkout` |
  | `INCOMPLETE`, provider `created`, same plan | `200` with the existing `checkout` |
  | `INCOMPLETE`, provider `created`, different plan | cancel the old provider subscription, then create a new one; `201` |
  | `INCOMPLETE`, provider `authenticated` (or `active`/`pending`/`halted` not proven paid), same plan | `200` with the existing `checkout`; no provider write |
  | `INCOMPLETE`, provider `authenticated` (or `active`/`pending`/`halted` not proven paid), different plan | `409 subscription_activating` |
  | `INCOMPLETE`, provider `cancelled`/`expired`, or no provider reference | create a provider subscription; `201` with `checkout` |
  | `CANCELLED`/`EXPIRED`, provider `authenticated` | `409 subscription_provider_state_unsupported`; nothing is touched |
  | any row except `PAST_DUE`, provider `paused` or an unrecognized status | `409 subscription_provider_state_unsupported`; nothing is touched |
  | any row except `PAST_DUE`, a known provider status whose plan maps to no ReviewFlow plan | `409 subscription_plan_unsupported`; nothing is touched |
  | `ACTIVE`, higher-priced plan | upgrade now; `200`, `checkout: null` |
  | `ACTIVE`, lower-priced plan | downgrade scheduled for the period end; `200`, `checkout: null` |
  | `ACTIVE` with no provider subscription reference | `409 subscription_provider_state_unsupported`; nothing is touched |
  | `ACTIVE` before this request, same plan | `422` |
  | moved into `ACTIVE` by this request's own reconcile, requested plan = the plan now in force | `200`, `checkout: null`; no further provider call |
  | `ACTIVE` with `cancel_at_period_end` | `409 subscription_cancelling` |
  | `PAST_DUE` | `409 subscription_past_due` |

- An old provider subscription that is still open on a `CANCELLED`/`EXPIRED` row (`created`, `pending`, `halted`, or `active` without a paid current period) is cancelled before a new one is created. If that cancel fails or is refused: `502 billing_provider_unavailable`, nothing changes, and no new provider subscription is created.
- **Same plan after a reconcile.** If the subscription was already `ACTIVE` before the request, the same plan is `422`. If this request's own reconcile moved it into `ACTIVE` (the merchant had paid and the webhook had not arrived yet) and the requested plan is the plan now in force, the request is a `200` no-op with `checkout: null`; any other plan is handled by the `ACTIVE` rows above.
- **Replacement subscription.** When a new provider subscription replaces an old one (a `CANCELLED` or `EXPIRED` subscription, or an `INCOMPLETE` one being replaced), the subscription starts a new lifecycle: `status` is `INCOMPLETE`, `plan` is the requested plan, and `pending_plan`, `current_period_start` and `current_period_end` are `null`, `cancel_at_period_end` is `false` and `usage` is `null` until the new subscription's first payment is confirmed. Earlier usage and payment history is kept and is not shown as the current period.
- **Response**: `{ checkout: { provider: "razorpay", key_id, subscription_id } | null, subscription: <GET /billing/subscription body> }`. The Razorpay key secret and webhook secret are never returned.
- **Errors**: `409 plan_change_unsupported` (Razorpay cannot update the subscription: UPI, eMandate, domestic card — nothing changes); `409 subscription_provider_state_unsupported` (the provider subscription is `authenticated` on a `CANCELLED`/`EXPIRED` row, `paused`, or in an unrecognized state — it is not touched and the merchant contacts support); `409 subscription_plan_unsupported` (the provider subscription's plan maps to no ReviewFlow plan — not touched, contact support); `502 billing_provider_unavailable` (a provider error or timeout, a permanent `4xx` on reading the stored subscription, or an unusable subscription id in Razorpay's create response — nothing is created, replaced, cancelled or stored, and the merchant retries later or contacts support); `503 billing_not_configured`.
- **Plan-change replacement** (off by default; both kinds behind flags): for an `ACTIVE`, not-cancelling row, when Razorpay refuses the provider update with a refusal classified as "cannot be updated", a second provider subscription is created: `201` with `checkout`. Additional responses: `422 no_credit_acknowledgement_required` (an upgrade without the acknowledgement; nothing changed), `409 replacement_in_progress` (any plan other than the pending target while one is pending, or while a retired subscription awaits termination), `409 replacement_activating` (a checkout on an ended row whose replacement the provider reports `active`), and a repeat of the pending target plan is `200` with the existing `checkout` and no provider call. An unclassified refusal, a disabled kind, or a downgrade whose verification is not evidenced or whose commit cutoff has passed stays `409 plan_change_unsupported`; an unset window is `503 billing_not_configured`.
- **Idempotency**: a double submit returns the same provider subscription.
- Entitlement is never granted by this endpoint or by the browser: the subscription becomes `ACTIVE` only after a paid invoice is confirmed with Razorpay.

### `POST /billing/subscription/cancel`
- **Auth**: session + CSRF
- **Permissions**: OWNER
- **Rate limit**: `billing_write`
- **Request**: empty body
- **Behavior**: `ACTIVE` → cancels at the end of the paid period (`cancel_at_period_end = true`; still `ACTIVE` until then). `PAST_DUE` → `CANCELLED` immediately. An `ACTIVE` subscription that is not already cancelling is first reconciled with Razorpay (the provider subscription is fetched and its state applied) before the cancellation is decided; an `ACTIVE` subscription that is already cancelling is a `200` no-op that makes no provider call. `PAST_DUE` makes no reconcile and does not depend on the provider.
- **Response**: `200` with the `GET /billing/subscription` body. Repeating the call is a `200` no-op.
- **Errors**: `409 subscription_not_cancellable` (no subscription, `INCOMPLETE`, `CANCELLED`, `EXPIRED`); `409 subscription_provider_state_unsupported` (an `ACTIVE` subscription whose provider subscription is `paused`, or that has no provider subscription reference: nothing is cancelled or touched, nothing changes, and the merchant contacts support); `502 billing_provider_unavailable` (only for the `ACTIVE` case: reading the provider subscription or the provider cancel failed; no cancellation happens).

### `POST /billing/replacement/cancel`
- **Auth**: session + CSRF
- **Permissions**: OWNER (API keys get `403`)
- **Rate limit**: `billing_write`
- **Request**: empty body (ignored)
- **Behavior**: abandons the pending plan-change replacement: a `created` or `authenticated` one is cancelled at the provider, then retired locally. Nothing pending is a `200` no-op.
- **Response**: `200` with the `GET /billing/subscription` body.
- **Errors**: `409 replacement_activating` (the provider reports it `active`; never cancelled), `409 replacement_committed` (a committed downgrade cannot be abandoned to keep the old plan), `409 subscription_provider_state_unsupported` (a status that is not touched), `502 billing_provider_unavailable` (nothing changed).

### `POST /billing/webhooks/razorpay`
- **Full path**: `POST /api/v1/billing/webhooks/razorpay`
- **Auth**: Razorpay webhook signature verification (`X-Razorpay-Signature`, HMAC-SHA256 over the raw body). No session, no API key, no CSRF.
- **Behavior**: idempotently records the event (`BillingEvent`, deduplicated on `x-razorpay-event-id`) and triggers a reconcile of the `Subscription` with Razorpay; records successful payments as `PaymentAttempt`. See `Webhook-Specification.md` §"Billing Webhook (Razorpay)".
- **Errors**: `401 invalid_signature`, `400 invalid_payload`, `429`.

## Dashboard / Analytics *(read-only endpoints; full spec created during implementation, see `../07-analytics/`)*

### `GET /analytics/dashboard` — top cards summary
### `GET /analytics/funnel` — sales → eligible → sent → delivered → clicked → reviewed
### `GET /analytics/locations` — per-location comparison table
### `GET /analytics/qr` — total QR scans, scans by location (never presented as equal to a Google review)

All: `date_from`/`date_to`/`location_id` filters, backed by direct PostgreSQL aggregation in V1 (see `../02-architecture/SAD.md` §7).

**Metric source split** (see `../06-automation/Campaign-Engine.md` §"Responsibility Split"): review-request-side counts (scheduled, sent, failed, cancelled, quota-exceeded, expired) are queried from `CampaignExecution`; delivery-side counts (queued, sent, delivered, read, failed) are queried from `WhatsAppMessage`. No endpoint conflates the two.

---

## General Conventions

- **Pagination**: cursor-based on all list endpoints; `?cursor=` and `?limit=` (max 100).
- **Errors**: standard shape `{ error: { code, message, field_errors? } }`.
- **Rate limits**: per-API-key and per-IP, enforced via Redis sliding window (see `Authentication.md` and `../02-architecture/Security-Architecture.md`).
- **IDs**: UUIDs (or hashids) in every response — never raw sequential integers — per the security architecture's IDOR mitigation.
