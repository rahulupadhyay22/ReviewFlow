# System Architecture Document (SAD)

## 1. Purpose

This document specifies how ReviewFlow's components work together technically: responsibilities, communication patterns, background processing, external integrations, scalability, failure handling, and deployment. It assumes familiarity with `Architecture.md` in this folder.

## 2. Component Responsibilities

| Component | Responsibility | Talks to |
|---|---|---|
| API Gateway (DRF) | Auth, tenant resolution, CRUD, dashboard reads, public API | Postgres, Redis (cache) |
| Webhook Receiver | Verify signature, write to Event Inbox, return 200 fast | Postgres |
| Event Processor (Celery) | Normalize → SaleCreated → eligibility → schedule | Postgres, Redis (locks) |
| Campaign Scheduler (Beat + tasks) | Fire delayed WhatsApp sends at the right time | Postgres, Redis |
| WhatsAppService | Adapter-based sending, status webhooks, opt-out | Meta Cloud API / shared pool |
| GoogleSyncService | OAuth token refresh, review polling, location mapping | Google Business Profile API |
| Analytics Aggregator | Dashboard read queries (direct aggregation in V1) | Postgres |
| Billing Service | Subscription state, quota enforcement | Payment provider, Postgres |
| Admin (Django Admin) | Internal ops visibility | Postgres (read-mostly) |

## 3. Django App Structure

```
reviewflow/
├── config/                      # settings, urls, celery.py, asgi/wsgi
├── core/                        # base models, tenant mixins, permissions, exceptions
├── accounts/                    # Merchant, User, TeamMember, TeamMemberLocation, roles, auth
├── locations/                   # Location model, location settings
├── integrations/
│   ├── core/                    # adapters.py, events.py, schemas.py, registry.py
│   ├── shopify/                 # V1 implementation priority
│   ├── woocommerce/  petpooja/  gofrugal/   # V1 product scope, phased implementation
│   ├── webhook/                 # generic inbound webhook — V1 implementation priority
│   ├── csv_import/              # V1 implementation priority
│   ├── zapier/  make/           # V1 product/architecture scope, implementation phase per roadmap
├── events/                      # IntegrationEvent inbox, idempotency, normalization
├── customers/                   # Customer model, opt-out
├── transactions/                # Transaction model
├── campaigns/                   # ReviewCampaign, CampaignExecution, eligibility engine
├── whatsapp/                    # WhatsAppService, providers/, templates, webhooks
├── google_reviews/              # GoogleConnection, GoogleLocation, GoogleReview, sync
├── feedback/                    # Feedback model + page API
├── qrcodes/                     # QRCode model + generation
├── analytics/                   # dashboard read/aggregation queries
├── billing/                     # Subscription, Plan, UsageRecord, payment webhooks
├── apikeys/                     # ApiKey, scoping, rate limiting
└── auditlog/                    # AuditLog model + middleware
```

**Rule**: views/serializers never contain business logic — they call a `services.py` function in the owning app. Eligibility, sending, and sync logic must be testable and reusable from Celery tasks, management commands, and the API alike. Internal admin for V1 is customized Django Admin registrations across these apps — there is no separate `admin_panel` app.

## 4. Integration Architecture

Every external sale-capture system normalizes into one internal event via the adapter pattern:

```python
class BaseAdapter(ABC):
    def verify(self, request) -> bool: ...
    def parse(self, raw_payload: bytes) -> dict: ...
    def normalize(self, parsed: dict) -> SaleCreated: ...
```

`integrations/core/registry.py` resolves the adapter from the receiving `Integration` row (identified before the `IntegrationEvent` is created — see `Multi-Tenancy.md` §"Two Points Hardened After Architecture Review"). See `../05-integrations/Integration-Architecture.md` for full detail.

## 5. Background Processing (Celery + Redis)

- **Redis roles**: Celery broker/result backend, rate-limit counters, nothing else. Redis is never the source of truth for business data.
- **Queues**: `events`, `whatsapp`, `google_sync`, `default` — run by separate worker pools so a slow Google API call never starves WhatsApp sending.
- **Celery Beat** (separate process from web and worker): `dispatch_due_executions` (every 1 min), `retry_failed_events` (every 5 min), `sync_google_reviews` (every 30–60 min per location, staggered), `refresh_google_tokens` (daily), `reset_usage_period` (monthly).
- **Scheduling model (locked)**: poll-based dispatch, not broker-side `eta` scheduling. `dispatch_due_executions` queries the indexed `CampaignExecution(scheduled_at, status)`, locks rows with `SELECT ... FOR UPDATE SKIP LOCKED`, re-checks `Transaction.status`, and sends. Cancellation (e.g. a refund) is a status flip, not a task revocation.

## 6. External Integrations

- **WhatsApp**: Meta Cloud API via `WhatsAppProvider` interface; see `../06-automation/WhatsApp-Architecture.md`.
- **Google Business Profile**: OAuth 2.0, `GoogleSyncProvider` interface; see `../06-automation/Google-Reviews.md`.
- **POS/e-commerce**: adapter pattern; see `../05-integrations/Integration-Architecture.md`.

## 7. Scalability

- Indexes on every FK and on `(merchant_id, created_at)` patterns keep V1's direct aggregation queries fast at MVP volume — no data warehouse.
- `LocationDailyStats` is a planned future optimization if a specific dashboard query is measured to be slow, not part of V1's initial scalability plan.
- Stateless Django/Celery workers scale horizontally by adding instances; Postgres read replicas are a V2-scale concern.

## 8. Failure Handling

- Duplicate webhooks: `(integration_id, external_event_id)` unique constraint makes retries a no-op (revised from `(source, external_event_id)` for tenant safety — see `Multi-Tenancy.md`).
- Duplicate campaign executions: `(campaign_id, transaction_id)` unique constraint.
- Failed event processing: retried with backoff, then moved to a dead-letter state for manual review.
- WhatsApp send failures: retried for transient errors, not retried for permanent ones (invalid number, opted-out).
- Google API rate limits: back off per `Retry-After`, resume on next scheduled tick.

## 9. Deployment Architecture

```
Cloudflare (DNS, CDN, WAF)
      │
      ▼
Vercel — Next.js frontend
      │
      ▼
Render — Django API + Celery Worker + Celery Beat (separate services, same codebase)
      │
      ▼
Supabase PostgreSQL — primary database
      │
Upstash Redis — broker/result backend, cache, rate-limit counters
      │
Cloudflare R2 — object storage (QR images, CSV imports, exports — never business records)
```

Celery worker and Beat run as separate services from the Django web process — a slow Google sync or a WhatsApp send-burst must never compete with request-handling for the API.
