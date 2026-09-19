# ReviewFlow — Claude Code Guide

ReviewFlow connects to a merchant's sale-capture system (POS or
e-commerce), waits a configurable delay after each completed sale,
sends the customer a WhatsApp review request, and syncs Google reviews
back into a multi-location merchant dashboard.

The platform never creates, edits, or incentivizes a review. The
customer always writes and submits it themselves, on Google.

## Source of Truth

`docs/` is authoritative. If this file, a spec, or your own judgment
conflicts with `docs/`, then `docs/` wins. Start with `docs/README.md`
for the reading order.

- Locked decisions: `docs/FINAL-ARCHITECTURE-REVIEW.md` and
  `docs/02-architecture/Architecture.md`
- V1 vs V2: `docs/01-product/Feature-Scope.md` (wins over any other
  doc)
- Build order: `docs/ROADMAP.md`
- Never change a locked decision without explicit user sign-off.

## Technology Stack

- Backend: Django + Django REST Framework monolith (Python 3.x;
  exact version pinned at project init)
- Async: Celery worker and Celery Beat, as separate processes
- Redis: Celery broker/result backend and rate-limit counters only;
  never the source of truth for business data
- Database: PostgreSQL (Supabase in production), the single source
  of truth
- Frontend: Next.js (separate repo/package)
- Object storage: Cloudflare R2, blobs only (QR images, CSV imports,
  exports)
- Hosting: Vercel (frontend), Render (API, worker, Beat), Upstash
  Redis, Cloudflare DNS/CDN/WAF
- External providers: Meta WhatsApp Cloud API, Google Business
  Profile API, Razorpay

Local development (see `docs/10-development/Development-Setup.md`):

    docker compose up -d
    python manage.py migrate
    python manage.py runserver
    celery -A reviewflow worker -Q events,whatsapp,google_sync,default -l info
    celery -A reviewflow beat -l info

## V1 / V2 Boundary

- V1 = implement now.
- V2 = documented / planned / do not implement.
- Every V2 item carries this exact status line:
  **Status: V2 — Planned. V1 Implementation: NO.**
- A field existing in the Data Dictionary does not make it V1.
  Example: `GoogleReview.reply_text` and `reply_status` are V2.
- WooCommerce, Petpooja, GoFrugal, Zapier, and Make are V1 scope,
  implemented in a later phase. They are not V2.

## Multi-Tenancy Rules (non-negotiable)

- Shared schema. Every tenant-owned row carries `merchant_id`,
  directly or through one documented FK hop.
- Every tenant-owned model's manager inherits
  `core.TenantScopedManager`. This is required everywhere and is the
  primary application-layer scoping mechanism. Never bypass it with
  `.objects.all()`, raw SQL, or cursors on tenant data.
- `TenantScopedManager` is NOT the security boundary. PostgreSQL
  Row-Level Security is the final database isolation boundary: it
  must still block cross-tenant access if application-layer scoping
  has a bug.
- RLS is enabled on every tenant table. Tenant work runs inside
  `transaction.atomic()` with
  `SET LOCAL app.current_merchant_id` (transaction-local, never
  session-level).
- No `BYPASSRLS` or superuser connection in normal application
  settings. Cross-tenant admin access goes only through an explicit,
  audited privileged path.
- `merchant_id` and `location_id` always come from the authenticated
  principal (session user, API key, or `Integration`), never from
  request data or webhook payloads.
- `IntegrationEvent.merchant_id` is resolved from `Integration`
  before the row is created. It is never null.
- `TeamMemberLocation` must satisfy
  `TeamMemberLocation.merchant_id == TeamMember.merchant_id ==
  Location.merchant_id` before insert.

## Service-Layer Rule (non-negotiable)

Views, serializers, admin actions, and Celery tasks contain no
business logic. They call a function in the owning app's
`services.py`.

External systems always go through their adapter interface:
`BaseAdapter`, `WhatsAppProvider`, or `GoogleSyncProvider`.

## Celery Merchant Context Rule

Every tenant task receives `merchant_id` as an explicit argument and
sets the tenant context (contextvar, plus `SET LOCAL` inside each
database transaction) before any query. Never infer the merchant from
another argument.

Queues: `events`, `whatsapp`, `google_sync`, `default`.

## Idempotency & Concurrency Rules

- Every inbound-event and task code path must be safe to run twice.
- Use database unique constraints, not check-then-insert:
  - `UNIQUE(integration_id, external_event_id)`
  - `UNIQUE(location_id, external_transaction_id)`
  - `UNIQUE(campaign_id, transaction_id)`
- One transaction receives at most one active/sent review request
  across all campaigns: `SELECT ... FOR UPDATE` on the `Transaction`
  row around eligibility and `CampaignExecution` creation.
- Dispatch is poll-based: `dispatch_due_executions` runs every
  minute with `FOR UPDATE SKIP LOCKED`. Never use Celery `eta` or
  `countdown` scheduling for sends.
- Quota is reserved atomically at `SCHEDULED -> SENDING` by locking
  the `UsageRecord`. `SCHEDULED` and `QUOTA_EXCEEDED` consume zero
  quota. Retries never consume another unit.
- The frequency cap is merchant-wide, across campaigns and
  locations.
- `DELIVERED` and `READ` exist only on `WhatsAppMessage`, never on
  `CampaignExecution`.

## Security Rules

- Three authentication mechanisms, never mixed:
  - Dashboard: Django session + CSRF
  - Public API: `Authorization: Bearer rf_live_...` (sha256-hashed,
    scoped, individually revocable, rate-limited)
  - Provider webhooks: signature verified in the adapter; missing or
    invalid signature fails closed with `401`
- Role checks via DRF permission classes: OWNER, ADMIN, MANAGER,
  VIEWER.
- OAuth tokens and integration credentials are encrypted with
  Fernet. Secrets come from environment variables only.
- Externally exposed IDs are UUIDs or hashids, never sequential
  integers.
- Never log phone numbers, tokens, message bodies, or raw payloads.
  `QRScanEvent` stores only a salted `ip_hash`, never a raw IP.
- Privileged and cross-tenant actions write an `AuditLog` row.
  See `docs/09-security/`.
- Review integrity: no review gating. The Google CTA is shown
  regardless of the feedback rating.

## Testing Rules

- pytest + pytest-django against real PostgreSQL (concurrency and
  RLS tests require it). Celery eager mode for integration tests.
- Every service function has a unit test. Every adapter and webhook
  has a fixture-based contract test using an anonymized real payload.
- Tests assert behavior required by the spec and docs, not
  implementation details.
- Cover the priority scenarios in
  `docs/10-development/Testing-Strategy.md` that a feature touches.
- Mock an external provider only when the feature uses it.
- Never skip, `xfail`, or weaken a test to make it pass.

## Git & Branch Rules

- Never commit directly to `main`. One feature = one branch,
  `feature/<slug>`, created by `/create-spec`.
- Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`),
  lowercase, under 72 characters.
- Never commit `.env` files or secrets.
- Never force-push. Never hard-reset.
- PRs are squash-merged only after explicit user confirmation. A
  locked-decision change needs separate architectural sign-off first.
- A destructive migration requires a backup step noted in the PR.

## Feature Workflow

1. `/create-spec <phase> <feature>` — from a clean `main`. Creates
   `feature/<slug>` and `.claude/specs/<NN>-<slug>.md`. The step
   number is the phase number in `docs/ROADMAP.md`.
2. Implement in Plan Mode (Shift+Tab twice), following the spec.
3. `/test-feature <NN>-<slug>` — `reviewflow-test-writer` writes the
   tests, `reviewflow-test-runner` runs and classifies them. Neither
   fixes code.
4. `/code-review-feature <NN>-<slug>` — `reviewflow-security-reviewer`
   and `reviewflow-quality-reviewer` run in parallel and produce one
   unified report. Fixes are applied only after approval. Requires a
   feature branch.
5. `/ship-feature` — tests, commit, push, PR. Stops for explicit merge
   confirmation, then squash-merges and cleans up.
6. Update the phase status in `docs/ROADMAP.md`.
