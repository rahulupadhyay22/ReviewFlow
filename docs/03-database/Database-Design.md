# Database Design Document

PostgreSQL is the single source of truth for all business data. This document covers every model's purpose, relationships, indexes, constraints, and tenant ownership. For the exhaustive field-by-field type listing, see `Data-Dictionary.md`. For the visual relationship map, see `ERD.md`.

**Legend — Tenant ownership**: `GLOBAL` = not tenant-scoped (platform-wide reference data); `MERCHANT` = scoped directly by `merchant_id`; `LOCATION` = scoped transitively via `location_id → Location.merchant_id`.

---

### Merchant
**Purpose**: the tenant root. Every other tenant-owned row traces back to one.
**Relationships**: 1───* Location, TeamMember, Customer, Integration, ApiKey, GoogleConnection, WhatsAppAccount, Subscription.
**Delete behavior**: soft-delete only (`status = DELETED`) — never hard-cascade, to preserve billing/audit history.
**Tenant ownership**: GLOBAL (this is the tenant itself).

### User
**Purpose**: a login identity, independent of any one merchant (supports one person on multiple merchant teams, in principle).
**Relationships**: *───* Merchant via `TeamMember`.
**Tenant ownership**: GLOBAL.

### TeamMember
**Purpose**: join table granting a `User` a `role` on a `Merchant`.
**Relationships**: Merchant 1───*, User 1───*; location scoping for `MANAGER` goes through the explicit `TeamMemberLocation` model below, not a default Django M2M.
**Unique constraint**: `(merchant_id, user_id)`.
**Tenant ownership**: MERCHANT.
**RLS**: `tenant_isolation` (direct `merchant_id`) plus the SELECT-only `self_membership` login-lookup policy keyed on `app.current_user_id` — see `../02-architecture/Multi-Tenancy.md` §Layer 2.

### TeamMemberLocation
**Purpose**: explicit through-model assigning a `TeamMember` to specific `Location`s (used for the `MANAGER` role). Introduced specifically to make cross-tenant assignment structurally impossible to represent, not just application-blocked.
**Relationships**: TeamMember 1───*; Location 1───*.
**Unique constraint**: `(team_member_id, location_id)`.
**Invariant enforced at the service layer**: `merchant_id` on this row must equal both the referenced `TeamMember.merchant_id` and `Location.merchant_id` — a mismatch is rejected before insert, never silently allowed and caught later by RLS alone.
**Tenant ownership**: MERCHANT.

### Location
**Purpose**: the unit everything else attaches to — campaigns, WhatsApp mapping, Google mapping, analytics, and integration-location mappings.
**Relationships**: Merchant 1───*; 1───* Integration, ReviewCampaign, QRCode; 1───1 `GoogleLocation`; 1───1 `WhatsAppLocationMapping`.
**Indexes**: `(merchant_id)`.
**Tenant ownership**: MERCHANT.

### Integration
**Purpose**: a connected sale-capture system (Shopify, Petpooja, generic webhook, etc.) at merchant level. One integration may feed multiple ReviewFlow locations.
**Relationships**: Merchant 1───*; 1───* IntegrationLocationMapping; 1───* IntegrationEvent.
**Fields of note**: `credentials_encrypted` (never plaintext), `config_json` (provider settings).
**Tenant ownership**: MERCHANT.

### IntegrationLocationMapping
**Purpose**: maps a merchant-level Integration to one or more internal Locations, with optional location-specific mapping/configuration.
**Unique constraint**: `(integration_id, location_id)`.
**Invariant**: integration, mapping, and location must belong to the same merchant; service layer validates before insert/update.
**Tenant ownership**: MERCHANT.

### IntegrationEvent
**Purpose**: the event inbox — every inbound webhook/API call is stored here before any processing.
**Revised ingestion order (tenant-safety fix)**: the merchant is identified from the `Integration` *before* the `IntegrationEvent` row is ever created — `merchant_id` is never written as `NULL` and resolved later.

```
External Webhook/API
   ↓
Identify Integration (from URL/credentials)
   ↓
Identify Merchant from Integration.merchant_id
   ↓
Create IntegrationEvent — merchant_id already known, non-null
   ↓
Idempotency check — (integration_id, external_event_id)
   ↓
Normalize event
   ↓
Resolve Location (may still require normalization to determine)
   ↓
Create/Update Transaction
```

**Unique constraint**: `(integration_id, external_event_id)` — **revised** from `(source, external_event_id)`. The same `external_event_id` can legitimately recur across different merchants' integrations (e.g. two Shopify stores both numbering invoices from 1), so scoping to the bare `source` string was not tenant-safe.
**Invariant enforced at creation, not merely documented**: `IntegrationEvent.merchant_id == Integration.merchant_id`.
**Indexes**: `(status, received_at)` — powers retry/dead-letter queries; `(integration_id, external_event_id)` — the unique constraint's index, doubles as the idempotency lookup.
**Delete behavior**: never hard-deleted (audit trail); may be purged per retention policy (see `../09-security/Privacy-Data-Retention.md`).
**Tenant ownership**: MERCHANT (direct, non-null — no longer transitively/nullably via `location_id`).

### Customer
**Purpose**: a merchant's end customer, identified by phone. Deliberately thin — not a CRM.
**Unique constraint**: `(merchant_id, phone)`.
**Relationships**: Merchant 1───*; 1───* Transaction.
**Tenant ownership**: MERCHANT.

### Transaction
**Purpose**: a normalized completed (or refunded/voided) sale.
**Unique constraint**: `(location_id, external_transaction_id)`.
**Indexes**: `(merchant_id, occurred_at)`.
**Relationships**: Customer 1───*; Location 1───*; Integration 1───*; 1───* CampaignExecution.
**Delete behavior**: `RESTRICT` from Merchant — financial records are never cascade-deleted.
**Tenant ownership**: LOCATION.

### ReviewCampaign
**Purpose**: per-location configuration of the review-request flow (mode, delay, frequency cap, template).
**Relationships**: Location 1───*; 1───* CampaignExecution; MessageTemplate *───1.
**Tenant ownership**: LOCATION.

### CampaignExecution
**Purpose**: owns the ReviewFlow **business-workflow** lifecycle for one attempt to send a review request for one transaction, under one campaign. Delivery-lifecycle detail (delivered/read) is owned by `WhatsAppMessage`, not duplicated here — see `../06-automation/Campaign-Engine.md` for the full separation-of-responsibilities rationale and status machine.
**Statuses**: `SCHEDULED, SENDING, SENT, FAILED, CANCELLED, QUOTA_EXCEEDED, EXPIRED`.
**Unique constraint**: `(campaign_id, transaction_id)` — the database-level anti-duplicate guarantee. **Not** a unique constraint on `transaction_id` alone; V1's "only one campaign fires per transaction" rule is enforced in eligibility logic (now transactionally, via row locking — see below), so a future second campaign type doesn't require a migration.
**Concurrency control**: creation is wrapped in `SELECT ... FOR UPDATE` on the `Transaction` row, checking for an existing active/sent execution for that transaction across *any* campaign before creating a new one. The unique constraint remains as a database-level backstop in addition to this locking, not a replacement for it. See `../06-automation/Campaign-Engine.md` §"Concurrency & Locking".
**Indexes**: `(scheduled_at, status)` — powers the dispatch loop.
**Relationships**: Transaction 1───*; Customer 1───*; 1───1 WhatsAppMessage; 1───0..1 Feedback.
**Delete behavior**: `RESTRICT` from Transaction.
**Tenant ownership**: MERCHANT (via campaign → location → merchant).

### MessageTemplate
**Purpose**: an approved WhatsApp template's text, variables, and Meta approval status.
**Tenant ownership**: MERCHANT.

### WhatsAppAccount
**Purpose**: a WhatsApp sender — either merchant-owned (`OWN_NUMBER`) or platform-owned (`SHARED_POOL`).
**Relationships**: Merchant 1───* (null/platform-owner for `SHARED_POOL` rows); 1───* WhatsAppLocationMapping; 1───* WhatsAppMessage.
**Tenant ownership**: MERCHANT for `OWN_NUMBER` rows; GLOBAL for `SHARED_POOL` rows.

### WhatsAppLocationMapping
**Purpose**: resolves which `WhatsAppAccount` a given `Location` currently sends from.
**Unique constraint**: `(location_id)` — a location has exactly one active sender at a time.
**Tenant ownership**: LOCATION.

### WhatsAppMessage
**Purpose**: one sent (or attempted) WhatsApp message and its delivery lifecycle.
**Relationships**: CampaignExecution 1───1; WhatsAppAccount 1───*.
**Indexes**: `(provider_message_id)` — for webhook status updates.
**Tenant ownership**: MERCHANT (via execution).

### GoogleConnection
**Purpose**: a merchant-level Google OAuth authorization.
**Relationships**: Merchant 1───*; 1───* GoogleLocation. (A merchant may have more than one, for the franchise edge case where different locations are managed under different Google accounts — see `../06-automation/Google-Reviews.md`.)
**Tenant ownership**: MERCHANT.

### GoogleLocation
**Purpose**: maps one internal `Location` to one Google Business Profile location.
**Unique constraint**: `(location_id)`.
**Fields of note**: store both `google_location_id` (Google Business Profile location resource identifier) and `google_place_id` (Place ID used for the public review URL). They are distinct identifiers and must never be substituted for one another.
**Relationships**: GoogleConnection 1───*; 1───* GoogleReview.
**Tenant ownership**: LOCATION.

### GoogleReview
**Purpose**: a synced Google review.
**Unique constraint**: `(google_location_id, google_review_id)` — upsert key for sync.
**Indexes**: `(google_location_id, review_created_at)`.
**Tenant ownership**: LOCATION (via `google_location_id → location_id`).
**V2 note**: `reply_text`/`reply_status` fields may exist in the schema ahead of time but are **Status: V2 — Planned, V1 Implementation: NO** — see `../01-product/Feature-Scope.md`.

### Feedback
**Purpose**: optional customer feedback (rating, comment, categories) collected before the Google CTA in Mode B campaigns.
**Relationships**: CampaignExecution 1───1.
**Tenant ownership**: LOCATION.

### QRCode
**Purpose**: a trackable redirect to a review or feedback link (`destination_type = GOOGLE_REVIEW | FEEDBACK_PAGE`). Each location can have its own QR code(s).
**Finalized design**: no `scan_count` counter field — scan totals are derived by counting `QRScanEvent` rows (see below).
**Relationships**: Location 1───*; 1───* QRScanEvent.
**Tenant ownership**: LOCATION.

### QRScanEvent
**Purpose**: the primary scan-tracking mechanism (not optional/supplementary — this replaces the earlier `scan_count`-counter design). One row per scan.
**Privacy**: `ip_hash` (a salted hash), never a raw IP address — see `../09-security/Privacy-Data-Retention.md`.
**Relationships**: QRCode 1───*.
**Tenant ownership**: LOCATION.
**Reporting rule**: a scan is never claimed as equivalent to, or reliably attributable to, a specific Google review — same attribution-honesty rule as `GoogleReview` (see `../01-product/Business-Rules.md` §8 and `../06-automation/Google-Reviews.md`).

### LocationDailyStats *(future — not a V1 requirement)*
**Purpose**: a pre-aggregated daily rollup, introduced only once a specific raw-aggregation dashboard query is measurably slow in production. V1 dashboards query the raw tables directly (`Transaction`, `CampaignExecution`, `WhatsAppMessage`, `GoogleReview`) — see `../02-architecture/SAD.md` §7 and `../07-analytics/` (created during implementation).
**Unique constraint (when introduced)**: `(location_id, date)`.
**Tenant ownership**: LOCATION.

### Plan
**Purpose**: a billing tier definition (name, price, quota, features).
**Tenant ownership**: GLOBAL.

### Subscription
**Purpose**: a merchant's current plan and billing-period state.
**Relationships**: Merchant 1───1 (active); Plan *───1.
**Tenant ownership**: MERCHANT.

### UsageRecord
**Purpose**: tracks atomically reserved request quota for a billing period.
**Unique constraint**: `(merchant_id, period_start, period_end)`.
**Rule**: `requests_used` increments only when a `CampaignExecution` atomically enters `SENDING`; `SCHEDULED` and `QUOTA_EXCEEDED` consume zero.
**Tenant ownership**: MERCHANT.

### PaymentAttempt
**Purpose**: records Razorpay renewal/retry attempts so dunning is idempotent and auditable.
**Unique constraint**: `(provider, provider_attempt_id)`.
**Tenant ownership**: MERCHANT.

### ApiKey
**Purpose**: a hashed, scoped credential for public API access.
**Tenant ownership**: MERCHANT.

### WebhookEndpoint
**Purpose**: (future) a merchant-configured URL to receive outbound events from ReviewFlow.
**Tenant ownership**: MERCHANT.

### AuditLog
**Purpose**: records privileged/administrative actions for accountability.
**Tenant ownership**: MERCHANT (nullable for platform-level actions).

---

## Design Principles Applied Throughout

- Financial/audit-relevant tables (`Transaction`, `WhatsAppMessage`, `AuditLog`) use `RESTRICT` rather than `CASCADE` on delete from `Merchant`, so deleting a merchant account can't silently wipe billing-relevant history.
- Every tenant table either carries `merchant_id` directly or has a documented parent path to the merchant. RLS policies use the direct merchant_id where present and an `EXISTS`/parent relationship check for transitive tables. Application scoping remains the primary isolation layer.
- Idempotency-critical constraints (`IntegrationEvent`, `CampaignExecution`) are enforced at the database level, not only in application code.
- Cross-tenant assignment is prevented structurally where practical (`IntegrationEvent.merchant_id` resolved at creation, `TeamMemberLocation`'s three-way merchant-match invariant) rather than relying solely on later-stage checks.
