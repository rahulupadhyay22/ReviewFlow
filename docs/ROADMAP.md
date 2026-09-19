# ReviewFlow Build Roadmap

Implementation order for V1. Derived from the existing docs. This
file adds no features and changes no decisions.

Scope is defined by `01-product/Feature-Scope.md`. This file only
sequences it — it is the "roadmap" that `Feature-Scope.md` and
`05-integrations/Integration-Architecture.md` refer to.

Where the docs state an order, it is followed exactly:

- Seed data is built early
  (`10-development/Development-Setup.md`).
- Shopify is built first; Generic REST API, Generic Webhook, and CSV
  Import alongside it; WooCommerce, Petpooja, GoFrugal, Zapier, and
  Make later (`05-integrations/Integration-Architecture.md`).

All other ordering follows the minimum model and feature
dependencies in `03-database/`, `04-api/`, and `06-automation/`.

The phase number is the step number used by `/create-spec`
(for example `/create-spec 4 event-inbox`). A phase may be split into
several specs if it is too large for one branch.

## V1 Phases

| #  | Phase                                     | Depends on         | Status      |
|----|-------------------------------------------|--------------------|-------------|
| 00 | Project scaffold & dev environment        | —                  | Done        |
| 01 | Core tenancy & RLS                        | 00                 | Not started |
| 02 | Accounts, roles & audit log               | 01                 | Not started |
| 03 | Locations, manager assignment & seed data | 02                 | Not started |
| 04 | Event inbox & ingestion core              | 03                 | Not started |
| 05 | Public API keys                           | 02                 | Not started |
| 06 | Priority integrations                     | 04 (+05, REST API) | Not started |
| 07 | Billing & quota foundation                | 02                 | Not started |
| 08 | WhatsApp                                  | 03                 | Not started |
| 09 | Google Business Profile                   | 03                 | Not started |
| 10 | Campaign engine & eligibility             | 04, 07, 08, 09     | Not started |
| 11 | Dispatch, quota reservation & recovery    | 07, 08, 10         | Not started |
| 12 | Customer feedback                         | 10                 | Not started |
| 13 | QR codes                                  | 03, 09, 12         | Not started |
| 14 | Dashboard & analytics                     | 09, 11, 13         | Not started |
| 15 | Privacy, retention & deletion             | 02, 04, 08, 09, 12 | Not started |
| 16 | Internal admin (Django Admin)             | 02, 04             | Not started |
| 17 | Phased integrations                       | 04                 | Not started |
| 18 | Pre-launch security hardening             | 00–17              | Not started |

### 00 — Project scaffold & dev environment

- Django project with `config/` (settings, urls, `celery.py`,
  asgi/wsgi)
- `docker-compose.yml` for Postgres and Redis
- Environment-based settings (`.env`, never committed)
- Celery worker and Beat wiring with queues `events`, `whatsapp`,
  `google_sync`, `default`
- pytest + pytest-django configuration

Source: `10-development/Development-Setup.md`,
`02-architecture/SAD.md` §3, §5, §9.

### 01 — Core tenancy & RLS

- `core` app: base models, `TenantScopedManager`, base exceptions
- Tenant contextvar + middleware
- Transaction-local `SET LOCAL app.current_merchant_id`
- Celery tenant-context entry point (explicit `merchant_id`)
- RLS policy pattern for direct and transitive tenant tables

Source: `02-architecture/Multi-Tenancy.md`,
`FINAL-ARCHITECTURE-REVIEW.md` §8.

### 02 — Accounts, roles & audit log

- `Merchant`, `User`, `TeamMember`
- Roles OWNER / ADMIN / MANAGER / VIEWER as DRF permission classes
- Session auth + CSRF: `/auth/login`, `/auth/logout`,
  `/auth/refresh`; optional TOTP 2FA
- `/merchant`, `/team-members` (invite, role change, revoke)
- `AuditLog` model + middleware

Source: `04-api/Authentication.md`,
`04-api/API-Specification.md` (Auth, Merchant, Team),
`02-architecture/Security-Architecture.md`,
`09-security/Audit-Logging.md`.

### 03 — Locations, manager assignment & seed data

- `Location` + per-location settings; `/locations` endpoints
- `TeamMemberLocation` through-model with the three-way merchant
  match; `PUT /team-members/{id}/locations`
- Seed management command: test `Merchant` with 2+ `Location`s.
  Extended in Phase 07 (`Plan`/`Subscription` pair) and Phase 08
  (`SHARED_POOL` `WhatsAppAccount` fixture).

Source: `03-database/Database-Design.md`,
`02-architecture/Multi-Tenancy.md`,
`10-development/Development-Setup.md` (Seed Data).

### 04 — Event inbox & ingestion core

- `Integration` (merchant-level), `IntegrationLocationMapping`
- `IntegrationEvent` inbox:
  `RECEIVED -> PROCESSED`, `FAILED -> DEAD_LETTER`
- `integrations/core`: `BaseAdapter`, `SaleCreated`, `schemas.py`,
  `registry.py`
- `Customer`, `Transaction`, normalization, location resolution
- `retry_failed_events` (Beat, every 5 minutes)
- `/integrations`, `/transactions`

Source: `04-api/Webhook-Specification.md`,
`06-automation/Event-Processing.md`,
`05-integrations/Integration-Architecture.md`.

### 05 — Public API keys

- `ApiKey`: sha256-hashed, shown once, scoped, individually revocable
- `Authorization: Bearer rf_live_...` authentication
- Per-key and per-IP rate limiting

Source: `04-api/Authentication.md` §2,
`09-security/Security-Controls.md`.

### 06 — Priority integrations

- Shopify adapter (HMAC signature) — depends on 04
- Generic Webhook (merchant field mapping in `config_json`) —
  depends on 04
- CSV Import (R2 upload, purge after processing) — depends on 04
- Generic REST API: `POST /sales`, `GET /sales` — depends on 04 and
  05, because `POST /sales` requires an API key with `sales:write`
  scope (`04-api/API-Specification.md` §Sales)
- Fixture-based contract test per adapter
- Write the deferred docs `Shopify.md`, `Generic-Webhook.md`,
  `CSV-Import.md`

Source: `05-integrations/Integration-Architecture.md`,
`04-api/API-Specification.md` (Sales),
`04-api/Authentication.md` §3.

### 07 — Billing & quota foundation

- `Plan`, `Subscription`, `UsageRecord`, `PaymentAttempt`
- Tiers: Starter / Growth / Pro / Business
- Razorpay checkout and `/billing/webhooks/razorpay`
- Subscription states `ACTIVE`, `PAST_DUE`, `CANCELLED`, `EXPIRED`;
  7-day `PAST_DUE` grace with dunning on days 0, 3, 6
- `reset_usage_period`; upgrade/downgrade rules; no rollover
- Extend the seed command with a `Plan`/`Subscription` pair

Source: `08-billing/Billing-Specification.md`,
`01-product/Business-Rules.md` §6.

### 08 — WhatsApp

- `WhatsAppAccount` (`OWN_NUMBER` / `SHARED_POOL`),
  `WhatsAppLocationMapping`
- `MessageTemplate` + approval polling
- `WhatsAppProvider` interface, `MetaCloudProvider`
- `WhatsAppMessage`; status and inbound webhooks; opt-out
- Embedded Signup; per-merchant send-rate cap; shared-number
  quality monitoring
- Extend the seed command with the `SHARED_POOL` `WhatsAppAccount`
  fixture

Source: `06-automation/WhatsApp-Architecture.md`,
`01-product/User-Flows.md` §2.

### 09 — Google Business Profile

- `GoogleConnection` (merchant-level OAuth, encrypted tokens)
- `GoogleLocation` (location mapping; `google_location_id` and
  `google_place_id` stored separately)
- `GoogleReview` sync + upsert; review link built from Place ID only
- `refresh_google_tokens`; `NEEDS_REAUTH` handling
- `/reviews`

Source: `06-automation/Google-Reviews.md`,
`01-product/User-Flows.md` §3.

### 10 — Campaign engine & eligibility

- `ReviewCampaign` (direct-to-Google or feedback-first mode)
- Eligibility rules 1–8 in `campaigns/services.py`
- `CampaignExecution` creation under the `Transaction` row lock
- Merchant-wide frequency cap
- Activation rules (Google location connected or skipped, WhatsApp
  sender configured, approved template)
- `/campaigns`

Depends on 07 because eligibility rule 8 checks an `ACTIVE`
subscription and available quota.

Source: `06-automation/Campaign-Engine.md`,
`01-product/Business-Rules.md` §2, §5.

### 11 — Dispatch, quota reservation & send recovery

- `dispatch_due_executions` (poll-based, `SKIP LOCKED`)
- Atomic quota reservation at `SENDING`
- 1:1 `WhatsAppMessage` creation in `QUEUED` state
- Reconciliation after worker crashes or provider timeouts
- Refund cancellation before reservation
- `QUOTA_EXCEEDED` 7-day expiry and automatic resume
- Subscription state gate

Source: `06-automation/Campaign-Engine.md`,
`08-billing/Billing-Specification.md`,
`FINAL-ARCHITECTURE-REVIEW.md` §4, §5, §10.

### 12 — Customer feedback

- `Feedback` model + feedback page API (rating, comment, categories)
- Feedback-first flow; the Google CTA is always shown
- `/feedback`

Source: `06-automation/Campaign-Engine.md` (Feedback-First Mode),
`01-product/User-Flows.md` §5.

### 13 — QR codes

- `QRCode`, `QRScanEvent` (salted `ip_hash` only)
- `/q/<code>` redirect to the Google review link or the feedback
  page
- QR images in R2; `/qrcodes` endpoints

Source: `03-database/Data-Dictionary.md`,
`04-api/API-Specification.md` (QR Codes),
`01-product/User-Flows.md` §6.

### 14 — Dashboard & analytics

- `/analytics/dashboard`, `/analytics/funnel`,
  `/analytics/locations`, `/analytics/qr`
- Direct PostgreSQL aggregation
- Review requests, Google CTA clicks, Google reviews, and QR scans
  reported as independent counts
- Write the deferred `07-analytics/Analytics-Specification.md`

Source: `02-architecture/SAD.md` §7,
`04-api/API-Specification.md` (Dashboard / Analytics),
`01-product/Business-Rules.md` §8.

### 15 — Privacy, retention & deletion

- Daily purge/redaction job: raw payloads 90 days, WhatsApp content
  90 days, customer/feedback/Google review PII 24 months, audit logs
  365 days minimum
- Legal/dispute holds
- Audited, idempotent deletion requests

Source: `09-security/Privacy-Data-Retention.md`,
`FINAL-ARCHITECTURE-REVIEW.md` §9.

### 16 — Internal admin (Django Admin)

Per-app Django Admin registrations ship with each app's own phase.
This phase covers the cross-cutting admin controls:

- Customized Django Admin (no bespoke admin app)
- Staff-only access + IP allowlist/VPN
- Dead-letter `IntegrationEvent` review
- Audited privileged cross-tenant service path

Source: `02-architecture/SAD.md` §3,
`09-security/Security-Controls.md`,
`02-architecture/Multi-Tenancy.md` (No Standing Privileged Role).

### 17 — Phased integrations (V1 scope, later phase)

- WooCommerce, Petpooja, GoFrugal, Zapier, Make
- Same adapter contract, fixture-based contract tests, and
  per-provider docs
- These are V1, not V2

Source: `05-integrations/Integration-Architecture.md`,
`01-product/Feature-Scope.md`.

### 18 — Pre-launch security hardening

- `pip-audit` / dependency scanning in CI
- Invalid-signature spike alerting
- Manual pass: auth bypass, IDOR, webhook fail-closed
- Full `09-security/Security-Controls.md` checklist sign-off
- Legal/tax review of the retention and payment defaults
- Write the deferred `10-development/Deployment.md` and
  `11-operations/` docs

Source: `09-security/Security-Controls.md`,
`FINAL-ARCHITECTURE-REVIEW.md` (Remaining work),
`README.md` (Deferred).

## Documented as Future — Not Scheduled in V1

Mentioned in the docs as later optimizations or extensions. Do not
build them unless a new decision schedules them:

- `LocationDailyStats` rollup — only if a dashboard query is measured
  to be slow (`02-architecture/SAD.md` §7)
- Outbound webhooks via `WebhookEndpoint`
  (`04-api/Webhook-Specification.md`)
- `SHARED_POOL` rotation across multiple accounts — V1.1
  (`06-automation/WhatsApp-Architecture.md`)
- Additional WhatsApp providers such as Gupshup or 360dialog
  (`06-automation/WhatsApp-Architecture.md`)

## V2 — Planned. V1 Implementation: NO.

From `01-product/Feature-Scope.md`. Every item below:
**Status: V2 — Planned. V1 Implementation: NO.**

- AI sentiment analysis
- AI review summaries
- AI topic/complaint detection
- AI reply suggestions (including `GoogleReview.reply_text` and
  `reply_status`)
- In-dashboard review reply management
- Advanced customer segmentation
- Visual workflow builder
- Loyalty / referral programs
- Competitor / market intelligence
- Advanced AI-driven analytics reports
- Mobile app
- Additional review platforms (Tripadvisor, Facebook, Zomato,
  Swiggy, etc.)
- POS integrations beyond the nine V1 integrations (e.g. Toast,
  Square, Lightspeed)
