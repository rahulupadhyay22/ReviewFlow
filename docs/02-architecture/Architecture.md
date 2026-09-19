# Architecture Overview

*High-level orientation document. For full technical detail see `SAD.md`, `Multi-Tenancy.md`, and `Security-Architecture.md` in this folder, plus the master Architecture Blueprint.*

## Executive Summary

ReviewFlow connects to a merchant's existing sale-capture system, waits a configurable delay after each completed sale, sends the customer a WhatsApp review request, and syncs Google reviews back into a merchant dashboard with multi-location analytics.

Built as a Django + DRF monolith with Celery/Redis for async work, PostgreSQL as the single source of truth, and an adapter layer isolating every external dependency (POS systems, WhatsApp providers, Google APIs) so V2 features extend the system without rewriting the core.

## Three Logical Planes

- **Ingestion plane** — receives sale events from any connected system, normalizes them, deduplicates them.
- **Automation plane** — decides eligibility, schedules and sends WhatsApp messages, tracks lifecycle, handles the optional feedback step.
- **Intelligence plane** — syncs Google reviews, aggregates analytics, renders the dashboard.

All three share one PostgreSQL database, tied together by `merchant_id`/`location_id` on every row.

## High-Level System Diagram

```
                        ┌─────────────────────────────┐
                        │        Next.js Frontend      │
                        └───────────────┬──────────────┘
                                        │ HTTPS (Cloudflare)
                        ┌───────────────▼──────────────┐
                        │   Django + DRF (API server)   │
                        │  - Auth, CRUD, dashboard API   │
                        │  - Webhook receivers            │
                        │  - Public API (API keys)        │
                        └───────┬───────────────┬───────┘
                                │               │
                 enqueue tasks  │               │ reads/writes
                                ▼               ▼
                        ┌───────────────┐ ┌──────────────┐
                        │ Redis (broker, │ │  PostgreSQL   │
                        │ cache, locks)  │ │ (source of    │
                        └───────┬───────┘ │  truth)       │
                                │         └──────┬────────┘
                    ┌───────────┴───────────┐    │
                    ▼                       ▼    │
            ┌───────────────┐     ┌──────────────▼──┐
            │ Celery Worker  │     │  Celery Beat     │
            │ - normalize    │     │  - poll due       │
            │ - eligibility  │     │    executions      │
            │ - send WA msg  │     │  - retry failed WA  │
            │ - sync Google  │     │  - quota resets      │
            └───────┬───────┘     └──────────────────┘
                    │
        ┌───────────┼─────────────────────┬─────────────────┐
        ▼           ▼                     ▼                 ▼
  ┌──────────┐ ┌──────────┐        ┌─────────────┐   ┌─────────────┐
  │ WhatsApp  │ │  Google   │        │ POS/E-comm   │   │ Cloudflare R2│
  │ (Meta     │ │ Business  │        │ Webhooks     │   │ (media,      │
  │  Cloud API│ │ Profile   │        │ (Shopify,    │   │  exports,    │
  │  / Shared  │ │  APIs     │        │  Petpooja…)  │   │  QR images)  │
  │  Number)  │ │           │        │             │   │             │
  └──────────┘ └──────────┘        └─────────────┘   └─────────────┘
```

Django never talks to WhatsApp/Google synchronously in a request-response cycle (except short read-only dashboard calls); all sends and syncs happen through Celery.

## Foundational Decisions (locked)

1. **Multi-tenancy**: shared PostgreSQL schema, `merchant_id` scoping, application-level `TenantScopedManager` + Postgres RLS as defense-in-depth. See `Multi-Tenancy.md`.
2. **WhatsApp**: Meta Cloud API primary provider; merchant-owned `WhatsAppAccount` preferred, platform `SHARED_POOL` account as fallback; `WhatsAppLocationMapping` joins locations to accounts.
3. **Google**: merchant-level OAuth (`GoogleConnection`), location-level mapping (`GoogleLocation`), reviews stored per mapped location (`GoogleReview`). The platform never creates a review — only the customer, on Google, does.
4. **Scheduling**: poll-based dispatch (`dispatch_due_executions`, every minute, indexed query, `SELECT ... FOR UPDATE SKIP LOCKED`) — not broker-side `eta` scheduling — because it makes cancellation (e.g. on refund) a simple status flip.
5. **Analytics**: direct PostgreSQL aggregation for V1; a `LocationDailyStats` rollup is a later optimization, not a day-one dependency.
6. **Admin**: customized Django Admin for V1; no bespoke admin application.

## Where to Look Next

- Full component breakdown, Django app structure, deployment topology → `SAD.md`
- Tenant isolation mechanics and tests → `Multi-Tenancy.md`
- Auth, encryption, RLS, rate limiting → `Security-Architecture.md`
- Every model's fields/relationships → `../03-database/Database-Design.md`
- Sale → review pipeline → `../06-automation/Event-Processing.md` and `Campaign-Engine.md`
