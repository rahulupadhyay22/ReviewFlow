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
- **Response**: `200 { user: { id, email }, merchant: { id, name }, role }` and sets the session cookie
- **Errors**: `401` invalid credentials (one generic response for every failure reason), `403` missing CSRF token, `429` too many attempts (per-IP)

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
- **Permissions**: any role
- **Response**: `200 { id, name, business_type, timezone, plan, status }`

### `PATCH /merchant`
- **Permissions**: OWNER, ADMIN
- **Request**: partial `{ name?, business_type?, timezone? }`
- **Validation**: `timezone` must be a valid IANA name
- **Errors**: `403` insufficient role, `422` validation

---

## Locations

### `GET /locations`
- **Permissions**: any role (VIEWER read-only; MANAGER sees only assigned locations)
- **Pagination**: cursor-based, default page size 25
- **Response**: `200 { results: [Location], next_cursor }`

### `POST /locations`
- **Permissions**: OWNER, ADMIN
- **Request**: `{ name, address?, phone?, timezone? }`
- **Errors**: `422` missing `name`

### `GET /locations/{id}`
- **Errors**: `404` if not found or belongs to another merchant (never `403` — don't reveal existence across tenants)

### `PATCH /locations/{id}`
- **Permissions**: OWNER, ADMIN, or MANAGER assigned to this location

### `DELETE /locations/{id}`
- **Permissions**: OWNER, ADMIN
- **Behavior**: soft-delete (`is_active = False`); does not cascade-delete historical transactions/executions

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
- **Auth**: API key with `sales:write` scope
- **Request**: normalized `SaleCreated` shape (see `Webhook-Specification.md`)
- **Response**: `201 { transaction_id, event_id }`
- **Validation**: phone must be valid E.164; `external_transaction_id` required
- **Idempotency**: repeating the same `external_transaction_id` for the same location is a no-op, returns the existing transaction

### `GET /sales`
- Same filters/pagination as `/transactions`

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

Member body: `{ id, user: { id, email }, role, invited_at, accepted_at }`.
There is no `locations` field yet — assigned locations for `MANAGER`
(read from `TeamMemberLocation`) arrive with Phase 03. A `MANAGER`
invited before then has no location assignment until Phase 03.

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
- **Rules**: nobody can change their own role. Only an OWNER can change an OWNER's role or grant the OWNER role. The last accepted OWNER is protected by these rules: self role change and self revoke are `403`, and only an accepted OWNER can target another OWNER, so an OWNER always remains. There is no reachable `409` for this.
- **Errors**: `404` if `{id}` is not a member of the current merchant (never `403`, to avoid revealing cross-tenant existence); `403 team_permission_denied` for the self/OWNER rules above; `422` for an invalid role

### `DELETE /team-members/{id}` (revoke)
- **Permissions**: OWNER, ADMIN only
- **Response**: `204`. Deletes the `TeamMember` row (pending or accepted); the underlying `User` is kept.
- **Rules**: same self/OWNER rules as the role change above, which also protect the last OWNER.
- **Errors**: `404`, `403 team_permission_denied` — same meanings as `PATCH` above

### `PUT /team-members/{id}/locations` (set assigned locations, MANAGER only)
- **Request**: `{ location_ids: [...] }`
- **Validation**: every `location_id` must belong to the same merchant as the team member — rejected with `422` otherwise (this is the API-level surface of the `TeamMemberLocation` invariant in `../02-architecture/Multi-Tenancy.md`)

---

## Integrations

### `POST /integrations/{provider}/connect`
- OAuth-style or credential-based connection flow, provider-specific — see `../05-integrations/Integration-Architecture.md`
- Integration is created at merchant level; locations are attached through `IntegrationLocationMapping`.

### `GET /integrations`
- Returns merchant-level integrations. Location mappings are returned separately or embedded as mapping summaries.

### `POST /integrations/{id}/locations`
- **Request**: `{ location_id, config_json?, is_active? }`
- **Validation**: integration and location must belong to the authenticated merchant; duplicate `(integration_id, location_id)` returns `409`.

### `PUT /integrations/{id}/locations`
- **Request**: `{ mappings: [{ location_id, config_json?, is_active? }] }`
- Replaces the authenticated merchant's mapping set atomically.

### `PATCH /integrations/{id}` (update merchant-level `config_json`)
### `DELETE /integrations/{id}` (disconnect; mappings remain as historical configuration but become inactive)

---

## Billing

### `GET /billing/subscription`
- **Permissions**: OWNER, ADMIN
- **Response**: current plan, status, billing period, quota, usage, and next billing action.

### `POST /billing/checkout`
- **Permissions**: OWNER
- **Behavior**: creates/updates the Razorpay subscription/payment flow; provider references are stored server-side.

### `POST /billing/webhooks/razorpay`
- **Auth**: Razorpay webhook signature verification
- **Behavior**: idempotently processes payment success/failure/renewal events and updates `Subscription`/`PaymentAttempt`.

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
