# Spec: Billing & Quota Foundation

## Overview
Adds the `billing` app: the four billing tiers as `Plan` rows, one
`Subscription` per merchant, a `UsageRecord` per billing period, the
`PaymentAttempt` ledger, Razorpay checkout, and the Razorpay webhook. It
also builds the billing-side primitives that later phases call: the
read-only entitlement check (Phase 10, eligibility rule 8) and the
atomic quota reservation on the `UsageRecord` row (Phase 11, the
`SCHEDULED -> SENDING` transition).

It exists now because Phases 10 and 11 cannot be built without an
`ACTIVE` subscription and a quota to reserve against, and because the
seed command still lacks its `Plan`/`Subscription` pair. Nothing sends a
message yet (WhatsApp is Phase 08, campaigns Phase 10, dispatch Phase
11), so this phase changes no customer-visible sending behavior.
Plane: **automation** (it gates the send loop); it adds no ingestion and
no intelligence feature.

**Status of this spec: ALL DECISIONS SIGNED OFF (final review,
2026-09-30), AND `docs/` SYNCHRONIZED WITH THEM (2026-09-30).** Plan
Mode has not been entered.
- Signed off: S1 (Change 1), S2 (Change 2), Change 3.
- Approved: O2, O7, O10, O11, O12. Decided: S4 and the O-defaults.
  Resolved: O17 (the paid-entitlement rule).
- **Pre-production gates, no evidence yet (amended 2026-10-03, see
  "Release gates (amended 2026-10-03)"):** D1, T1, T4 and T7 must be
  evidenced against an activated Razorpay account **before production
  deployment and before any affected billing functionality is enabled**.
  They no longer block the PR merge, because no activated account is
  available. Nothing about them is assumed, their stop rules remain
  armed, and no behavior they cover is production-ready until they pass.
- T2 is a production go-live verification. T6 is recorded as an
  observation.
- Phase 07 is not complete with this spec: the plan change for UPI,
  eMandate and domestic cards is a follow-up Phase 07 spec. Until it
  ships, Phases 10 and 11 (which depend on 07) cannot be spec'd. Phases
  08, 09, 16 and 17 are unaffected.
- The approved documentation updates listed under "Files to change"
  (including the `billing_ref_lookup` subsection in `Multi-Tenancy.md`)
  were applied on 2026-09-30, ahead of implementation. `ROADMAP.md`,
  `CLAUDE.md` and earlier specs were not edited.

### Two premises checked against `docs/`
The Phase 07 brief described two items as unconfirmed assumptions.
`docs/` is authoritative, and both are already locked there:

- **Razorpay is the V1 payment gateway.** Locked in
  `FINAL-ARCHITECTURE-REVIEW.md` §6, `Billing-Specification.md` §K
  ("V1 Locked"), `Feature-Scope.md` (V1 list) and `ROADMAP.md` §07. It
  is not an open decision. Choosing another gateway would be a
  `LOCKED DECISION CHANGE`.
- **Quota-exhausted requests are held as `QUOTA_EXCEEDED` for 7 days,
  then `EXPIRED`; never silently dropped.** Locked in
  `FINAL-ARCHITECTURE-REVIEW.md` §4, `Business-Rules.md` §2 rule 8 and
  §6, `Billing-Specification.md` §B–§D. It is not an open decision.
  `CampaignExecution` does not exist yet, so the status itself is
  implemented in Phase 11. Phase 07 only returns the reservation
  outcome that Phase 11 maps to it.

A third preference in the brief, deriving usage from existing records
instead of keeping a counter, is also overridden by a locked decision:
`UsageRecord.requests_used`, incremented under a row lock, is "the
single source of truth for billable request usage"
(`Billing-Specification.md` §L; `CLAUDE.md` Idempotency rules). This
spec adds no other counter, rollup or cache.

### Scope by feature
| Feature | Built in Phase 07 | Existing functionality it integrates with | Deferred |
|---|---|---|---|
| Plans and entitlements | `Plan` model, Admin entry, `GET /billing/plans`; quota read from the plan | Django Admin staff auth | prices and quota numbers (O3); trials, coupons, annual plans |
| Subscription state | `Subscription`, the state machine, `GET /billing/subscription` | `Merchant`, session auth, roles, `GET /merchant`'s `plan` field | staff overrides and suspension (Phase 16) |
| Billing periods and quota reset | provider-driven period, `reset_usage_period()`, no rollover | none | none |
| Usage tracking | `UsageRecord`, `get_entitlement()` | none | usage charts (Phase 14) |
| Quota enforcement | `reserve_quota_unit()` and its concurrency guarantees | none (its caller does not exist yet) | calling it at `SENDING`, `QUOTA_EXCEEDED`, 7-day expiry, resume (Phase 11); eligibility rule 8 (Phase 10) |
| Upgrade / downgrade | `POST /billing/checkout` plan change by provider update, where Razorpay allows it; `pending_plan`; `409` otherwise | role permission classes, `AuditLog` | plan change for UPI, eMandate and domestic cards by a replacement subscription (follow-up Phase 07 spec) |
| Cancellation / expiry | cancel endpoint, grace expiry | `AuditLog` | refunds of subscription fees |
| Payment provider | `billing/razorpay.py`, checkout | the stdlib HTTP helper pattern from the Shopify services | a second provider |
| Webhooks and idempotency | the Razorpay receiver, `BillingEvent`, sync-by-fetch | `WebhookIpRateThrottle`, the pre-tenant url-name skip list, the lookup-policy pattern | invalid-signature alerting (Phase 18) |
| Payment failure handling | `PAST_DUE`, the 7-day clock, dunning checkpoints, the paid-entitlement rule | none | ReviewFlow-sent reminders (not scheduled) |
| Tenant isolation | RLS on four tables, `billing_ref_lookup` | `core/rls.py`, `core/tenancy.py`, `TenantScopedManager` | cross-tenant staff access (Phase 16) |
| Authorization | OWNER changes, ADMIN reads | `IsOwner`, `IsOwnerOrAdmin`; API keys stay excluded | none |
| Audit | the `billing.*` actions listed under "Audit" | `auditlog.services.record()` | platform-level audit rows (Phase 16) |
| Background jobs | sync, maintenance sweep, Beat entry | `tenant_task`, the `retry_failed_events` sweep pattern | none |
| Seed data | plans, subscription, usage record in `seed_dev` | the Phase 03 seed command | WhatsApp fixture (Phase 08) |

## Source docs
- `docs/ROADMAP.md` §07 (scope), §03 (seed extension), §10 and §11
  (what the later phases own)
- `docs/08-billing/Billing-Specification.md` §A–§L (binding policy)
- `docs/01-product/Business-Rules.md` §2 rule 8, §6
- `docs/FINAL-ARCHITECTURE-REVIEW.md` §4 (quota), §6
  (subscription/payment policy), §8 (RLS)
- `docs/01-product/Feature-Scope.md` — V1 list, Billing line
- `docs/01-product/PRD.md` §5 (tiers), role table (Owner: billing;
  Admin: not billing)
- `docs/02-architecture/Multi-Tenancy.md` §Layer 1, §Layer 2 (the three
  signed-off lookup policies), §No Standing Privileged Role, §Data Model
  Note (`Plan` is global)
- `docs/02-architecture/Security-Architecture.md` §Authorization
- `docs/02-architecture/SAD.md` §3 (`billing/` app), §5 (Beat:
  `run_billing_maintenance`; §4 payment-provider seam)
- `docs/02-architecture/Architecture.md` (Beat billing sweep)
- `docs/03-database/Data-Dictionary.md` §Plan, §Subscription,
  §UsageRecord, §PaymentAttempt, §Merchant (`plan_id`)
- `docs/03-database/Database-Design.md` §Plan–§PaymentAttempt, §Design
  Principles (`RESTRICT` on financial tables); `ERD.md`
- `docs/04-api/API-Specification.md` §Billing, §Merchant, §General
  Conventions
- `docs/04-api/Webhook-Specification.md` §Inbound Webhook Endpoints
  (fail-closed rule)
- `docs/04-api/Authentication.md` §3
- `docs/06-automation/Campaign-Engine.md` §Eligibility rule 8, §Quota
  Reservation and Send Recovery, §Subscription State Gate
- `docs/09-security/Audit-Logging.md` ("Billing plan changed")
- `docs/09-security/Security-Controls.md` (secrets, webhook security)
- `docs/09-security/Privacy-Data-Retention.md` rule 4 (financial records)
- `docs/10-development/Testing-Strategy.md` — Billing tests row,
  "Final Consistency Tests Added: Billing / quota reservation"
- `docs/10-development/Coding-Standards.md` §1, §5–§6
- `.claude/specs/05-public-api-keys.md`, `06-priority-integrations.md`
  (Decisions 1, 9, 11, 15) — the pre-tenant lookup, robust enqueue,
  receiver posture and webhook throttle mirrored here
- Razorpay documentation, read 2026-09-30 (webhook validation,
  subscription webhook events, subscription states, update-subscription
  API). See "Razorpay verification gate".

## Depends on
- Phase 02 (Done): `Merchant`, `User`, `TeamMember`, role permission
  classes (`IsOwner`, `IsOwnerOrAdmin`), session auth + CSRF,
  `auditlog.services.record()`, `core.api` error handler,
  `CursorPagination`.
- Phase 01 (Done): `BaseModel`, `TenantScopedManager`, `tenant_context`,
  `tenant_atomic`, `tenant_task`, `core/rls.py`, `TenantMiddleware`.
- Reused from Phase 06 (Done; not a roadmap dependency):
  `integrations.throttling.WebhookIpRateThrottle` and the pre-tenant
  url-name skip list in `accounts/middleware.py`.
- ROADMAP lists only 02 as the dependency; it is Done.
- Shopify dev-store validation stays deferred and does not block this
  phase.

## Roadmap Phase
- Phase: 07 — Billing & quota foundation
- Completes entire phase: No
- If No: remaining phase work: the plan change for subscriptions
  Razorpay cannot update (UPI, eMandate, domestic cards), by a
  replacement subscription. It gets its own spec
  (`07-plan-change-replacement`, not yet written) and needs the
  commercial decisions C1–C3 (last section) first.
- Covered here: the four models; the four tiers (as `Plan` data);
  Razorpay checkout and the webhook; the subscription states with the
  7-day grace and the day 0/3/6 dunning checkpoints;
  `reset_usage_period`; no rollover; the upgrade/downgrade rules where
  the provider supports an update; the seed extension.
- Not part of Phase 07 at all: the `CampaignExecution` side of quota
  (`QUOTA_EXCEEDED`, 7-day expiry, resume, the dispatch gate) is ROADMAP
  §11. Commercial prices and quota numbers are data entered later (O3).

## Locked decisions touched
- Razorpay is the V1 payment gateway (`FINAL-ARCHITECTURE-REVIEW.md` §6;
  `Billing-Specification.md` §K) — DEPENDS ON
- Quota reserved atomically at `SCHEDULED -> SENDING` by locking the
  `UsageRecord`; `SCHEDULED`/`QUOTA_EXCEEDED` consume zero; retries never
  consume another unit (`FINAL-ARCHITECTURE-REVIEW.md` §4) — DEPENDS ON
  (the billing-side primitive is built here; Phase 11 calls it)
- `QUOTA_EXCEEDED` 7-day hold, `EXPIRED`, resume with full re-check
  (`FINAL-ARCHITECTURE-REVIEW.md` §4) — NO CHANGE (Phase 11)
- Refund handling (`FINAL-ARCHITECTURE-REVIEW.md` §5) — NO CHANGE
- `PAST_DUE` 7-calendar-day grace; sending disabled immediately; dunning
  on days 0, 3, 6; recovery to `ACTIVE`; unrecovered by end of day 7 →
  `EXPIRED`; no automatic downgrade during grace
  (`FINAL-ARCHITECTURE-REVIEW.md` §6) — DEPENDS ON for the grace,
  the day-0/3/6 schedule, recovery and expiry; LOCKED DECISION CHANGE
  for what a dunning attempt is (Change 3)
- `PaymentAttempt` makes payment/dunning idempotent, `UNIQUE(provider,
  provider_attempt_id)` (`FINAL-ARCHITECTURE-REVIEW.md` §6) —
  LOCKED DECISION CHANGE (Change 3: it records payments only; dunning
  idempotency moves to the `Subscription` row)
- Upgrade immediate; downgrade at the next period; no rollover
  (`Billing-Specification.md` §E–§G) — DEPENDS ON. The rules are not
  changed. This spec implements them only where Razorpay can update the
  subscription; the rest is the follow-up Phase 07 spec (S4)
- `UsageRecord` `UNIQUE(merchant_id, period_start, period_end)`
  (`Billing-Specification.md` §L) — DEPENDS ON
- Subscription states `ACTIVE`, `PAST_DUE`, `CANCELLED`, `EXPIRED`
  (`Business-Rules.md` §6; `Data-Dictionary.md` §Subscription) —
  LOCKED DECISION CHANGE (adds `INCOMPLETE`, see below)
- RLS is the final isolation boundary; pre-tenant reads only through a
  signed-off SELECT-only policy (`Multi-Tenancy.md` §Layer 2) —
  LOCKED DECISION CHANGE (adds `billing_ref_lookup`, see below)
- Shared schema, `merchant_id` scoping, `TenantScopedManager`
  (`Multi-Tenancy.md` §Layer 1) — DEPENDS ON
- Transaction-local `SET LOCAL app.current_merchant_id`
  (`FINAL-ARCHITECTURE-REVIEW.md` §8) — DEPENDS ON
- No standing privileged role, no `BYPASSRLS`, no `SECURITY DEFINER`
  (`Multi-Tenancy.md`) — NO CHANGE
- `merchant_id` from the authenticated principal, never from request
  data or webhook payloads (`Multi-Tenancy.md` §Layer 1) — DEPENDS ON
- Three auth mechanisms, never mixed; webhooks fail closed `401`
  (`Authentication.md`) — DEPENDS ON
- Celery tasks take explicit `merchant_id`; queues; no `eta`/`countdown`
  (`SAD.md` §5; `Architecture.md` §4) — DEPENDS ON
- Redis is broker and rate-limit counters only (`SAD.md`) — DEPENDS ON
- Django Admin, no bespoke admin app (`Architecture.md` §6) — DEPENDS ON
- Retention (`FINAL-ARCHITECTURE-REVIEW.md` §9) — NO CHANGE

LOCKED DECISION CHANGE — USER SIGN-OFF REQUIRED

### Change 1 — `billing_ref_lookup` (a fourth pre-tenant SELECT-only policy)
**What changes:** `billing_subscription` gets a second RLS policy next
to `tenant_isolation`: a SELECT-only `billing_ref_lookup`,
`USING (payment_provider_ref = NULLIF(current_setting('app.current_billing_ref', true), ''))`.
It is the analogue of `self_membership`, `api_key_lookup` and
`integration_lookup`.

**Why:** Razorpay sends every webhook to one account-level URL. The
request carries no merchant, and the merchant may never be taken from
the payload. The only safe link is the provider subscription id that
ReviewFlow itself stored at checkout. `FORCE ROW LEVEL SECURITY` blocks
that read without a merchant context.

**Alternatives rejected:**
- Reading `notes.merchant_id` from the (signed) payload: the merchant
  would come from a webhook payload, against `CLAUDE.md` Multi-Tenancy
  Rules.
- A global, non-RLS mapping table: `Multi-Tenancy.md` §Data Model Note
  allows only `Merchant` and reference tables such as `Plan` to be
  global.
- A `SECURITY DEFINER` function or a privileged connection: against §No
  Standing Privileged Role.

**Scope of the change:**
- SELECT-only. No write policy is keyed on `app.current_billing_ref`.
- The setting is set only by `SET LOCAL` inside
  `core.tenancy.billing_ref_lookup_atomic()`, which refuses to run in a
  merchant context and is always its own outermost (durable)
  transaction.
- Stronger than `integration_lookup`: the platform webhook signature is
  verified **before** any database read, so an unauthenticated caller
  never triggers the lookup.
- `Subscription.objects.for_lookup_ref(ref)` is the only
  merchant-unscoped `Subscription` read, and only inside that context
  for that same ref.

**Status: signed off by the user on 2026-09-30,** exactly as specified.
The approved invariants must not be weakened in implementation: the
signed webhook → provider subscription ref → lookup flow; signature
verification before any database read; a SELECT-only policy with no
write policy keyed on the ref; and a lookup context that never runs
inside a merchant tenant context. `Multi-Tenancy.md` §Layer 2 gets its
subsection with the implementation PR.

### Change 2 — `Subscription.status` gains `INCOMPLETE`
**What changes:** the enum becomes `INCOMPLETE`, `ACTIVE`, `PAST_DUE`,
`CANCELLED`, `EXPIRED`.

**Why:** the provider subscription id must be stored server-side at
checkout, before any payment, or the webhook cannot be resolved to a
merchant (Change 1). The four documented states have no value for
"checkout started, not yet paid". Reusing `EXPIRED` or `CANCELLED` for
that would misreport history.

**Effect on locked behavior:** none. Only `ACTIVE` may send
(`Campaign-Engine.md` §Subscription State Gate), so `INCOMPLETE` blocks
sending like every non-`ACTIVE` state. The grace, dunning and expiry
rules are untouched.

**Alternative rejected:** activating from the browser's checkout
callback with no server-side row. A merchant who pays and closes the tab
would be charged with no entitlement and nothing to reconcile against.

**Status: signed off by the user on 2026-09-30.** `INCOMPLETE` is never
entitled. Activation happens only through provider state reconciliation
(webhook signal or maintenance sweep, then a fetch), never through a
browser callback.

### Change 3 — dunning is a checkpoint, not a payment attempt
**Locked wording:** "Dunning: payment retry/reminder attempts on day 0,
day 3, and day 6; each attempt is idempotent and recorded against the
subscription/payment record" (`Billing-Specification.md` §K), and
"`PaymentAttempt` makes payment/dunning operations idempotent"
(`FINAL-ARCHITECTURE-REVIEW.md` §6).

**What changes:**
- Day 0, 3 and 6 are ReviewFlow **recovery checkpoints**. Razorpay owns
  every payment retry and every payer notification. ReviewFlow does not
  retry a payment and does not send a reminder on those days.
- A checkpoint is recorded on the `Subscription` row
  (`dunning_stage`), not in `PaymentAttempt`.
- `PaymentAttempt` holds real Razorpay payments only.

**Why:** verified on 2026-09-30 (`Billing-Specification.md` §M, V9, V4).
Razorpay has no API to re-attempt a subscription charge. The manual
charge is a Dashboard button and is not supported for domestic cards.
Razorpay retries on T+1, T+2 and T+3 and then stops, and it emails and
texts the payer on each failure. ReviewFlow has no email channel, and
WhatsApp is Phase 08.

**What stays locked and unchanged:** the 7-day grace, sending disabled
immediately, the day 0/3/6 schedule, idempotency of each step, recovery
to `ACTIVE` on payment, `EXPIRED` at the end of day 7, no automatic
downgrade during grace.

**Status: signed off by the user on 2026-09-30.**

Change 3 is approved: dunning on days 0, 3, and 6 is a ReviewFlow
recovery-checkpoint model, not a payment-retry/reminder model. Razorpay
remains solely responsible for payment retries and payer notifications.
PaymentAttempt records real Razorpay payments only; dunning checkpoint
idempotency is represented by Subscription.dunning_stage and the
corresponding audit event.

Documents whose locked wording was amended to match (done
2026-09-30):
- `docs/08-billing/Billing-Specification.md` §K, the "Dunning" bullet
- `docs/FINAL-ARCHITECTURE-REVIEW.md` §6, the lines "Dunning attempts
  are scheduled for days 0, 3, and 6" and "`PaymentAttempt` makes
  payment/dunning operations idempotent"
- `docs/01-product/Business-Rules.md` §6, "Dunning attempts occur on
  day 0, day 3, and day 6"
- `docs/README.md` §Final Architecture Decisions, "dunning on days
  0/3/6"
- `docs/03-database/Data-Dictionary.md` §PaymentAttempt (the
  "dunning/payment retries" note, `attempt_type`, `status`) and
  §Subscription (`dunning_stage`)
- `docs/03-database/Database-Design.md` §PaymentAttempt, "so dunning is
  idempotent and auditable"

`docs/ROADMAP.md` §07 ("dunning on days 0, 3, 6") stays true as written
and needs no edit.

## Django apps
- `billing`: **created** (listed in `SAD.md` §3 and `CLAUDE.md` Project
  Structure). Models, services, the Razorpay client module, tasks,
  views, the webhook receiver, and the `Plan` admin.
- `core`: **touched**. `billing_ref_lookup_atomic()` and
  `get_current_lookup_billing_ref()` in `core/tenancy.py`;
  `rls_select_by_billing_ref()` in `core/rls.py`.
- `accounts`: **touched**. `GET /merchant`'s `plan` stub is filled from
  billing (Decision 5); the webhook url name joins the pre-tenant skip
  list.
- `config`: **touched**. `INSTALLED_APPS`, `urls.py`, Beat, throttle
  rate, `RAZORPAY_*` settings.

## Models & database changes
All models are on `core.BaseModel` (UUID `id`, `created_at`,
`updated_at`). Every FK to `Merchant`, `Plan` and `Subscription` is
`on_delete=PROTECT`: these are financial records
(`Database-Design.md` §Design Principles). No billing row is ever
deleted by application code; there is no delete service and no delete
endpoint. Fields marked **(new)** are not in the Data Dictionary and
need the docs follow-up listed under "Files to change".

### `Plan` (table `billing_plan`) — GLOBAL
Tenant ownership: GLOBAL (`Database-Design.md`; `Multi-Tenancy.md` §Data
Model Note). No `merchant_id`, no RLS, plain manager, like `Merchant`.
Written only through Django Admin (staff) and `seed_dev`.

| Field | Django type | Null | Notes |
|---|---|---|---|
| `name` | `CharField(32)`, choices `Starter`, `Growth`, `Pro`, `Business` | No | tier name, as documented |
| `monthly_price` | `DecimalField(10, 2)` | No | display value; the amount actually charged is defined by the Razorpay plan |
| `currency` **(new)** | `CharField(3)`, default `INR` | No | ISO 4217 |
| `quota_requests` | `PositiveIntegerField` | No | review requests per billing period |
| `features_json` | `JSONField` | Yes | free-form feature flags for the dashboard; no code reads a key from it in this phase |
| `provider_plan_id` **(new)** | `CharField(64)` | Yes | the Razorpay plan id; null only for dev seed plans |
| `is_active` **(new)** | `BooleanField(default=True)` | No | `False` = retired: not offered, existing subscribers keep it |

Constraints and indexes:
- `UNIQUE(name) WHERE is_active` (`billing_plan_active_name_uniq`): one
  offered plan per tier. A price change is a new row plus retiring the
  old one, because a Razorpay plan's amount is immutable.
- `UNIQUE(provider_plan_id) WHERE provider_plan_id IS NOT NULL`: the
  webhook/sync maps a provider plan id to exactly one `Plan`.
- `CHECK (monthly_price >= 0)`.
- Deletion: never (PROTECT from `Subscription`; admin delete disabled).
- Audit: edits happen only in Django Admin, which writes its own
  `LogEntry`. Platform-level `AuditLog` rows (`merchant_id` null) can be
  written only by the Phase 16 privileged path, so none is written here.

No migration inserts plan rows. Prices and quotas are not defined in
`docs/` (O3).

### `Subscription` (table `billing_subscription`) — MERCHANT
One row per merchant, reused across resubscribes (Decision 3).

| Field | Django type | Null | Notes |
|---|---|---|---|
| `merchant` | `FK(Merchant, PROTECT)` | No | |
| `plan` | `FK(Plan, PROTECT)` | No | the plan in force (or chosen, while `INCOMPLETE`) |
| `status` | `CharField(16)`, choices below | No | |
| `current_period_start` | `DateTimeField` | Yes | null only while `INCOMPLETE`: before the first activation, and again after a replacement checkout (see "Replacement subscription reset") |
| `current_period_end` | `DateTimeField` | Yes | same |
| `payment_provider_ref` | `CharField(64)` | Yes | Razorpay subscription id (`sub_...`) |
| `pending_plan` **(new)** | `FK(Plan, PROTECT)` | Yes | a scheduled downgrade, applied at the next period |
| `past_due_at` **(new)** | `DateTimeField` | Yes | start of the grace period; drives the 7-day clock and the checkpoints |
| `dunning_stage` **(new)** | `PositiveSmallIntegerField` | Yes | the last dunning checkpoint reached in the current grace episode: `0`, `3` or `6` |
| `cancel_at_period_end` **(new)** | `BooleanField(default=False)` | No | merchant asked to cancel; still `ACTIVE` until the period ends |
| `provider_status` **(new)** | `CharField(16)` | Yes | the Razorpay status last applied (`created`, `active`, `halted`, ...); used for checkout routing and to know whether a provider cancel is still owed |
| `provider_synced_at` **(new)** | `DateTimeField` | Yes | start time of the provider fetch last applied; the stale-apply guard |

`Status` (`TextChoices`): `INCOMPLETE`, `ACTIVE`, `PAST_DUE`,
`CANCELLED`, `EXPIRED`.

Constraints and indexes:
- `UNIQUE(merchant)` (`billing_subscription_merchant_uniq`): the
  database guarantee behind "Merchant 1───1 Subscription". It also makes
  a concurrent first checkout safe without check-then-insert.
- `UNIQUE(payment_provider_ref) WHERE payment_provider_ref IS NOT NULL`:
  the lookup key for Change 1.
- `CHECK (status = 'INCOMPLETE' OR (current_period_start IS NOT NULL AND current_period_end IS NOT NULL))`.
- `CHECK (current_period_end > current_period_start)` (passes when
  either is null).
- `CHECK ((status = 'PAST_DUE') = (past_due_at IS NOT NULL))`.
- `CHECK ((dunning_stage IS NULL) = (past_due_at IS NULL))` and
  `CHECK (dunning_stage IN (0, 3, 6))`: a checkpoint exists only inside
  a grace episode.
- Manager: `SubscriptionManager(TenantScopedManager)` with
  `for_lookup_ref(ref)`, which raises `TenantContextError` unless
  `get_current_lookup_billing_ref() == ref` (the
  `Integration.objects.for_lookup_id` pattern).
- RLS: `rls_direct("billing_subscription")` plus
  `rls_select_by_billing_ref("billing_subscription")`. ENABLE + FORCE.
- Deletion: never.
- Audit: every status or plan change writes one `AuditLog` row (see
  "Audit").

### `UsageRecord` (table `billing_usagerecord`) — MERCHANT
| Field | Django type | Null | Notes |
|---|---|---|---|
| `merchant` | `FK(Merchant, PROTECT)` | No | |
| `period_start` | `DateTimeField` | No | equals the subscription's `current_period_start` for that period (Decision 6) |
| `period_end` | `DateTimeField` | No | equals `current_period_end` |
| `requests_used` | `PositiveIntegerField(default=0)` | No | incremented only by `reserve_quota_unit()` |

- `UNIQUE(merchant, period_start, period_end)`
  (`billing_usagerecord_period_uniq`): the idempotency guarantee for
  period creation. `reset_usage_period()` is insert-or-ignore on it.
- `CHECK (period_end > period_start)`.
- `requests_used >= 0` comes from `PositiveIntegerField`. There is
  deliberately no `requests_used <= quota` constraint: the quota lives
  on `Plan`, and a record can legitimately sit above a later, lower
  quota.
- The quota is **not** copied onto the record. It is read from
  `Subscription.plan.quota_requests` inside the reservation lock, which
  is what makes an upgrade effective immediately.
- Manager: `TenantScopedManager`. RLS: `rls_direct`. ENABLE + FORCE.
- Deletion: never. Old periods are kept as the billing history.
- Audit: none per increment (not a privileged action). "Manual quota
  adjustment" is a platform-side action for Phase 16.

### `PaymentAttempt` (table `billing_paymentattempt`) — MERCHANT
**A row is one real Razorpay payment. Nothing else is ever stored
here.** Dunning checkpoints are not payments and live on `Subscription`
(Change 3). Columns are those of `Data-Dictionary.md` §PaymentAttempt;
no column is added.

| Field | Django type | Null | Notes |
|---|---|---|---|
| `merchant` | `FK(Merchant, PROTECT)` | No | |
| `subscription` | `FK(Subscription, PROTECT)` | No | |
| `provider` | `CharField(16)`, choices `razorpay` | No | lowercase identifier, like `Integration.Provider` |
| `provider_attempt_id` | `CharField(64)` | No | the Razorpay payment id (`pay_...`), always |
| `attempt_type` | `CharField(16)`, `RENEWAL`, `RETRY` | No | see below |
| `status` | `CharField(16)`, `INITIATED`, `SUCCEEDED`, `FAILED` | No | see below |
| `attempted_at` | `DateTimeField` | No | the provider's payment time |

- `UNIQUE(provider, provider_attempt_id)`
  (`billing_paymentattempt_provider_uniq`): the same payment reported
  twice (a re-delivered webhook, or the webhook and the invoice check)
  inserts one row.
- `CHECK (provider_attempt_id LIKE 'pay\_%')`
  (`billing_paymentattempt_real_payment`): the database itself refuses
  a row that is not keyed by a Razorpay payment id. No synthetic key,
  checkpoint or reminder can be stored, so a count or sum over this
  table is always a count or sum of payments.
- **`attempt_type`**, both values being real captured payments:
  - `RENEWAL`: a payment recorded while the subscription was not
    `PAST_DUE` (the first charge of a subscription included; the Data
    Dictionary has no third value).
  - `RETRY`: a payment recorded while the subscription was `PAST_DUE`,
    that is, a Razorpay retry or a payer-initiated recovery that
    succeeded.
- **`status`**: V1 writes only `SUCCEEDED`. Razorpay delivers no payment
  entity for a failed subscription charge (`subscription.pending` and
  `.halted` carry only the subscription; §M, V8), so there is nothing to
  record for a failure. `INITIATED` and `FAILED` stay in the enum because
  the Data Dictionary defines them; nothing writes them in this phase.
- **One writer, insert-or-ignore on the unique constraint:** the sync,
  from the `payment_id` of the paid invoice that satisfied the
  paid-entitlement rule, with `attempted_at` = that invoice's `paid_at`.
  The webhook receiver writes no `PaymentAttempt` (decided 2026-10-01):
  the `subscription.charged` payload carries no invoice, so it cannot
  supply the `paid_at` the signed-off `attempted_at` rule requires.
- Index `(merchant, attempted_at)`.
- The service-layer invariant `PaymentAttempt.merchant_id ==
  Subscription.merchant_id` is asserted before insert (RLS filters by
  the row's own `merchant_id` and does not prove the parent match).
- Manager: `TenantScopedManager`. RLS: `rls_direct`. ENABLE + FORCE.
- Deletion: never. No amount, card, UPI, email or phone data is stored.

### `BillingEvent` (table `billing_billingevent`) — MERCHANT **(new model)**
The webhook inbox and dedup row. It stores identifiers only, never the
payload (Decision 7).

| Field | Django type | Null | Notes |
|---|---|---|---|
| `merchant` | `FK(Merchant, PROTECT)` | No | resolved from the `Subscription` before the row is created; never null |
| `provider` | `CharField(16)`, choices `razorpay` | No | |
| `provider_event_id` | `CharField(64)` | No | the `x-razorpay-event-id` header |
| `event_type` | `CharField(64)` | No | e.g. `subscription.charged` |
| `provider_ref` | `CharField(64)` | No | the provider subscription id the event is about |
| `processed_at` | `DateTimeField` | Yes | set when a settled sync whose fetch started more than 5 seconds after the event was received has been applied (clock-skew margin; see step 7) |

- `UNIQUE(provider, provider_event_id)`
  (`billing_billingevent_provider_uniq`): duplicate delivery is a no-op.
- Partial index on `(merchant)` `WHERE processed_at IS NULL` for the
  maintenance sweep.
- Manager: `TenantScopedManager`. RLS: `rls_direct`. ENABLE + FORCE.
- Deletion: never in this phase. It holds no PII, so the Phase 15 purge
  has nothing to redact here.

### `Merchant`
No column is added. `Merchant.plan_id` from the Data Dictionary is
**not** created (Decision 5).

### Migrations
- `billing/0001_initial`: the five tables, constraints and indexes.
- `billing/0002_rls`: `rls_direct` on the four tenant tables, then
  `rls_select_by_billing_ref` on `billing_subscription`. Reversible.
- Additive only. No existing table changes, no data migration, no
  destructive step.

## Subscription lifecycle

### States and what each allows
| Status | Sending entitled | Meaning |
|---|---|---|
| (no row) | No | the merchant never started a checkout |
| `INCOMPLETE` | No | checkout started, first payment not confirmed |
| `ACTIVE` | Yes, while `now < current_period_end` and quota remains | the current period is proven paid |
| `PAST_DUE` | No, immediately | a renewal failed and no paid invoice for the current period is proven; 7-day grace is running |
| `CANCELLED` | No | ended by cancellation |
| `EXPIRED` | No | grace ran out unpaid |

### Paid-entitlement rule
Razorpay's `active` status does **not** prove payment. A `halted`
subscription returns to `active` when the payer changes the payment
method, and "the previous charges are not re-attempted"
(`Billing-Specification.md` §M, V4). So `active` alone never grants
entitlement.

`current_period_paid(entity, invoices)` returns the invoice, from
`GET /v1/invoices?subscription_id=<ref>`, that meets **all** of:
- `subscription_id` equals the stored ref;
- `status == "paid"`, `amount_due == 0`, and `payment_id` is present;
- `billing_start <= entity.current_start < billing_end`.

It returns nothing otherwise. It is one function, so the window test can
be corrected in one place if T7 shows Razorpay's fields differ.

The rule is required for:
- every transition **into** `ACTIVE` (from `INCOMPLETE`, `PAST_DUE`,
  `CANCELLED`, `EXPIRED`);
- every period advance on an `ACTIVE` row (the entity's `current_start`
  differs from the stored `current_period_start`);
- and therefore every `UsageRecord` creation.

It is not required for a plan refresh inside an unchanged period, or for
any transition out of `ACTIVE`.

**Fail closed.** No qualifying invoice, or an invoice fetch that fails,
means no transition: the row keeps its status, the period does not
advance, no `UsageRecord` is created, and the events stay unprocessed
for the next sweep. A `PAST_DUE` row stays `PAST_DUE` and its 7-day
clock keeps running.

When the rule is satisfied, the invoice's `payment_id` is recorded as a
`PaymentAttempt` (insert-or-ignore) in the same transaction.

### Transitions
```
(no row)   --checkout-->                                INCOMPLETE
INCOMPLETE --provider active AND period paid-->         ACTIVE
ACTIVE     --provider pending | halted-->               PAST_DUE   (past_due_at = now, dunning_stage = 0)
PAST_DUE   --provider active AND period paid-->         ACTIVE     (past_due_at, dunning_stage cleared)
PAST_DUE   --provider active, period NOT paid-->        PAST_DUE   (no change; the clock keeps running)
PAST_DUE   --now >= past_due_at + 7 days-->             EXPIRED    (unconditional; provider cancel is best-effort)
ACTIVE     --provider cancelled | completed | expired--> CANCELLED
PAST_DUE   --provider cancelled, inside grace-->        CANCELLED
PAST_DUE   --OWNER cancels-->                           CANCELLED  (local first; provider cancel is best-effort)
CANCELLED | EXPIRED --checkout-->                       INCOMPLETE (new provider subscription, same row)
CANCELLED | EXPIRED --provider active AND period paid--> ACTIVE    (a late successful charge)
```
Every transition not listed is rejected by `apply_provider_snapshot()`
and changes nothing.

### Razorpay status → ReviewFlow status
Applied by `apply_provider_snapshot()` from a fetched subscription
entity and, where the paid-entitlement rule applies, the fetched
invoices. The webhook's event type never drives a transition.

| Razorpay `status` | Local `INCOMPLETE` | Local `ACTIVE` | Local `PAST_DUE` | Local `CANCELLED` / `EXPIRED` |
|---|---|---|---|---|
| `created`, `authenticated` | no change | no change | no change | no change |
| `active`, current period **paid** | → `ACTIVE` | same period: refresh plan. New period: advance it | → `ACTIVE` | → `ACTIVE` |
| `active`, current period **not proven paid** | no change | same period: refresh plan. New period: no change (lapsed, not entitled) | no change (stays `PAST_DUE`) | no change |
| `pending`, `halted` | no change | → `PAST_DUE` | no change | no change |
| `cancelled`, `completed`, `expired` | no change | → `CANCELLED` | → `CANCELLED`, or `EXPIRED` if the grace has ended | no change |
| `paused` or unknown | no change, logged | no change, logged | no change, logged | no change, logged |

- `provider_status` is stored on every apply, including "no change"
  cells, so the dashboard and the sweep know the provider's view.
- Razorpay `halted` never produces `EXPIRED`. ReviewFlow's own 7-day
  clock does (`Billing-Specification.md` §K).
- On a paid `active`: `current_period_start/end` are set from the
  entity's `current_start`/`current_end` (unix seconds, UTC), `plan`
  from `Plan.provider_plan_id == entity.plan_id`, `pending_plan` is
  cleared when it equals the new plan, and `reset_usage_period()` runs
  in the same transaction.
- An entity whose `plan_id` matches no `Plan` row changes nothing, is
  logged, and leaves its events unprocessed (operator error in plan
  setup; never guess a plan).

### Billing period, quota reset and rollover
- The billing period is the provider's charge cycle. It is per merchant
  (anniversary based), not a calendar month.
- A new period begins only when the provider reports `active` with a
  new `current_start`/`current_end` **and** the paid-entitlement rule
  holds for it. That same
  transaction creates the new `UsageRecord` with `requests_used = 0`.
  The old record is left as it is: **no rollover**.
- **Reactivation inside the stored period does not reset quota**
  (clarified 2026-10-03). When a `CANCELLED` or `EXPIRED` row returns to
  `ACTIVE` with a proven paid period equal to the one already stored, that
  period's existing `UsageRecord` is reused with its `requests_used`
  unchanged. This follows from `reset_usage_period()` being insert-or-ignore
  on `UNIQUE(merchant, period_start, period_end)` and from no rollover; a
  fresh `UsageRecord` with `requests_used = 0` is opened only for a new,
  qualifying billing period. There is no other quota-reset path.
- **Lapsed period, no webhook yet:** an `ACTIVE` subscription with
  `now >= current_period_end` is **not** moved to `PAST_DUE`. Only a
  provider failure signal does that. It is simply not entitled until a
  sync advances the period (fail closed; the maintenance sweep forces
  that sync). Phase 11 leaves such executions `SCHEDULED`.
- `SAD.md` used to list `reset_usage_period` as a monthly Beat entry
  (corrected 2026-09-30). A global
  monthly tick cannot match per-merchant anniversaries, so
  `reset_usage_period` is a service function called on period advance,
  and the Beat entry is the maintenance sweep (Decision 8).

### Upgrade and downgrade
**S4, decided by the user on 2026-09-30.** Razorpay allows a plan
update only on `authenticated`/`active` subscriptions, not for UPI, not
for eMandate, and for domestic cards "you can update only the offer
that is linked to them" (`Billing-Specification.md` §M, V5). The provider
update below is therefore documented to work only for international
cards.

- **This spec:** a plan change Razorpay refuses returns
  `409 plan_change_unsupported` and changes nothing. No second provider
  subscription is created, and a paid subscription is never cancelled to
  change plan.
- **What such a merchant can do meanwhile:** cancel (effective at the
  period end) and check out the other plan once the period has ended.
  A checkout while a cancellation is pending is `409`. A
  quota-exhausted merchant on those payment methods cannot upgrade
  mid-period.
- **Locked §E and §F are not amended.** They are delivered for these
  payment methods by the follow-up Phase 07 spec (a replacement
  subscription), which needs the commercial decisions C1–C3.

Where the provider update does apply: an immediate upgrade makes
Razorpay create an invoice and charge the prorated difference; ReviewFlow
computes no proration and follows the period the provider reports
(O13, decided).

"Upgrade" means the target plan's `monthly_price` is higher than the
current plan's; "downgrade" means lower; equal is `422`.

- **Upgrade (immediate):** the provider subscription is updated with
  `schedule_change_at = "now"`. The returned entity is applied through
  `apply_provider_snapshot()` in the same request. `Subscription.plan`
  changes, so the next reservation reads the higher quota. Usage already
  consumed is kept unless the provider starts a new cycle, in which case
  the new period gets a fresh `UsageRecord` only with a paid invoice for
  it (V5, T4).
- **Downgrade (next period):** the provider subscription is updated with
  `schedule_change_at = "cycle_end"` and `pending_plan` is set.
  `plan` and quota are unchanged for the rest of the period. At the next
  successful charge the provider reports the new `plan_id` and the sync
  applies it. Never retroactive; consumed usage is never invalidated.
- **During `PAST_DUE`:** no plan change is accepted (`409`), matching
  "no automatic downgrade during the grace period".
- **Provider refusal:** when Razorpay rejects the update (UPI,
  eMandate, domestic card), the provider error becomes
  `409 plan_change_unsupported` and nothing changes (O5, decided).

### Cancellation and expiry
- **Merchant cancellation** of an `ACTIVE` subscription is
  cancel-at-period-end (O6): the provider is told to cancel at cycle
  end, `cancel_at_period_end = True`, and the status stays `ACTIVE` with
  full entitlement until the provider reports `cancelled`. If that
  provider call fails, nothing changes (`502`): ReviewFlow never says
  "cancelling" while Razorpay would keep charging.
- **Merchant cancellation of a `PAST_DUE` subscription** is immediate and
  local-first: the row becomes `CANCELLED` in the request, whatever the
  provider then allows. The provider cancel is attempted in the same
  request; if it fails or is refused it is owed, and the sweep retries
  it only while the provider subscription may be cancelled (see "Which
  provider states may be touched"). Razorpay documents cancel only for
  `active` and `authenticated` (T1), so the local state must not depend
  on it.
- **A cancelled subscription and later snapshots** (clarified
  2026-10-03; no rule changed). The merchant's cancel is a local
  transition: it does not stamp `provider_synced_at`, so a snapshot
  fetched before the cancel is not stale and is judged by the mapping
  table alone when it is applied after it ("Expiry versus sync: race
  rules"). Provider status alone never moves a `CANCELLED` row: `pending`,
  `halted`, a terminal status, `created`, `authenticated`, `paused`, an
  unrecognized status, or `active` without a qualifying paid invoice
  change nothing (that snapshot's `provider_status` and
  `provider_synced_at` are still recorded). Only the existing late-charge
  rule reactivates it: provider `active` **and** a qualifying paid invoice
  for that snapshot's own current period, the full paid-entitlement
  evidence. The known reconciliation consequence: a payment that Razorpay
  collected before the cancel, seen by a snapshot fetched before it, can
  reactivate the row after the merchant cancelled, while Razorpay itself
  was told to cancel; the next sync observes the provider `cancelled` and
  the row returns to `CANCELLED` by the mapping table. The cancel is not
  sticky. If the snapshot is applied first and recovers the row to
  `ACTIVE`, the cancel then follows the `ACTIVE` rule (cancel at the end of
  the period).
- A payer can also cancel a UPI subscription from their UPI app. The
  next sync sees `cancelled` and applies the mapping table.
- **Grace expiry is unconditional.** At `past_due_at + 7 days` the
  `advance_subscription_dunning` task makes the row `EXPIRED` in its own
  transaction, whatever the provider then allows and whether or not a
  sync has run, is running or has failed. It makes no provider call before
  that commit and does not sync first (W7, decided 2026-10-03: an earlier
  wording had the task reconcile once more before expiring; that coupled
  expiry to the provider and is withdrawn, and it never matched the W4
  implementation). The locked day-7 rule never waits on a provider call.
  A charge that succeeds in the last moments is caught by the
  independently enqueued sync; see "Expiry versus sync: race rules".
- **The provider cancel is best-effort.** After the row is `EXPIRED` the
  task asks Razorpay to cancel the subscription. If that call fails or
  is refused, the row stays `EXPIRED`, the failure is logged, and the
  sweep retries on later ticks only for as long as the provider
  subscription may be cancelled (see "Which provider states may be
  touched"). The sweep never cancels or touches an `authenticated`,
  `paused` or unrecognized provider status.
- **Expiry race:** see "Expiry versus sync: race rules" below.
- **A payer who fixes the payment method after `halted`** leaves the
  missed invoice unpaid, and no API can charge it. The row stays
  `PAST_DUE` and expires on day 7. The way back is to cancel and check
  out again, which the dashboard offers as `RESUBSCRIBE`.
- `CANCELLED` and `EXPIRED` keep their `UsageRecord` and
  `PaymentAttempt` history. Resubscribing is a new checkout.

### Which provider states may be touched
One rule, defined once, used by checkout, by the reconcile that precedes
it and by the maintenance sweep. It is one shared predicate in
`billing/services.py` (`provider_may_be_cancelled(provider_status)`), so
the two never become separate state machines. Approved 2026-09-30.

- **May be cancelled** (only ever on a `CANCELLED` or `EXPIRED` row, and
  only while the row's current period is not proven paid): `created`,
  `pending`, `halted`, `active`. An `active` subscription with a paid
  current period converges to `ACTIVE` and is never cancelled.
- **Never touched, never cancelled, never replaced:** `authenticated`,
  `paused`, and any unrecognized or missing status. The recognized
  statuses are the nine in F6: `created`, `authenticated`, `active`,
  `pending`, `halted`, `cancelled`, `completed`, `expired`, `paused`.
- **Terminal** (`cancelled`, `completed`, `expired`): nothing to cancel.
  The ref may be replaced.

What checkout answers on these states (after "Reconcile first"):

| Provider state after the reconcile | Result |
|---|---|
| `authenticated` on a `CANCELLED`/`EXPIRED` row | `409 subscription_provider_state_unsupported`; no provider write, no local change |
| `paused`, or an unrecognized status | `409 subscription_provider_state_unsupported`; no provider write, no local change |
| a known status whose `plan_id` maps to no `Plan` row | `409 subscription_plan_unsupported`; no provider write, no local change |
| the fetch fails or returns an entity with no `id` or `status` | `502 billing_provider_unavailable` (below) |

- A `409 subscription_provider_state_unsupported` or
  `409 subscription_plan_unsupported` is not retried into success by the
  merchant: the provider subscription is left exactly as it is and the
  merchant contacts support. `409 subscription_activating` is kept for the
  `INCOMPLETE` authorized states only, where it resolves by itself once
  the webhook or the sweep applies the provider state.
- **Permanent `4xx` on the fetch.** A stored ref Razorpay no longer knows
  (or any `4xx` answer to the reconcile fetch) is a failed fetch: `502
  billing_provider_unavailable`, fail closed. No provider subscription is
  created, replaced or cancelled, the local row is not changed, and the
  merchant retries later or contacts support. Nothing replaces such a ref
  automatically: ReviewFlow invents no replacement semantics for it.
- **Reusing an `INCOMPLETE` subscription.** "Same plan" means the fetched
  `plan_id` equals the requested plan's `provider_plan_id`. Otherwise the
  already-defined plan-change and cancel rules apply (table under `POST
  /billing/checkout`); a paid or authorized subscription is never silently
  replaced.
- **The create response.** The subscription id Razorpay returns from the
  create call must match `^[A-Za-z0-9_]{1,64}$` before it is stored. If
  not: `502 billing_provider_unavailable`, the ref is not stored, and no
  unusable local reference is left behind. The provider subscription that
  was created is never paid (the browser never received its id) and lapses
  on the provider side, as in "Provider-created-but-not-committed".

### Dunning checkpoints (days 0, 3, 6)
**Who does what.** Razorpay owns every payment retry (T+1, T+2, T+3,
then none) and every notice to the payer (email and SMS with an "Update
Card" option, because `customer_notify` is true). ReviewFlow cannot
retry a charge and, in V1, sends no reminder of its own. A checkpoint
is a ReviewFlow recovery step, never a payment attempt.

**What a checkpoint does, exactly:**
1. It forces a reconcile: fetch the subscription and, if it is `active`,
   the invoices, then apply the mapping table. This picks up the result
   of Razorpay's retries and of a payer's fix.
2. If the row is still `PAST_DUE`, it advances
   `Subscription.dunning_stage` to the checkpoint's day.
3. It writes one `billing.dunning_checkpoint` audit row
   (`metadata: { stage }`), only when the stage actually advanced.
4. The dashboard reads `dunning_stage` and `grace_ends_at` from
   `GET /billing/subscription` to escalate its banner.

**Schedule and idempotency:**
- Stage `0` is set in the transaction that sets `PAST_DUE`; that
  transition's own audit row records it.
- Stages `3` and `6` are reached by the maintenance task when
  `now >= past_due_at + 3 days` and `+ 6 days`.
- The advance is one conditional update:
  `UPDATE ... SET dunning_stage = N WHERE id = ... AND past_due_at = <episode> AND dunning_stage < N`.
  Zero rows changed means it was already done: no audit row, no other
  effect. Running the task any number of times at the same instant
  leaves one stage change and one audit row.
- A sweep that was down across a boundary goes straight to the highest
  stage that is due and writes one audit row for it.
- When the grace episode ends (recovery, cancellation or expiry),
  `dunning_stage` is cleared together with `past_due_at`.

**Not in this model:** a ReviewFlow-sent reminder. Razorpay's
`POST /v1/invoices/{id}/notify_by/{medium}` could carry one later, but
the documentation does not say it works for subscription invoices (T5),
so it is not used and not relied on.

## Webhook design

`POST /api/v1/billing/webhooks/razorpay` (url name
`webhook-billing-razorpay`). Approved on 2026-09-30 (O2).

### Authentication
- Header `X-Razorpay-Signature`: hex HMAC-SHA256 of the **raw request
  body**, keyed with the platform `RAZORPAY_WEBHOOK_SECRET`. Compared
  with `hmac.compare_digest`.
- An unset/empty `RAZORPAY_WEBHOOK_SECRET` rejects every request. The
  code never computes an HMAC with an empty key.
- No session, no API key, no CSRF: `authentication_classes = []`,
  `permission_classes = [AllowAny]`, `csrf_exempt`,
  `WebhookIpRateThrottle`. The signature is the only authentication.

### Receive order (normative)
1. Per-IP throttle (`429` + `Retry-After`).
2. Verify the signature over the raw body. Missing or invalid →
   `401 invalid_signature`. Nothing is read or stored.
3. Require a valid `x-razorpay-event-id` header: a string matching
   `^[\x21-\x7E]{1,64}$`. Missing, empty, non-string, longer than 64
   characters or with any other character → `401 invalid_signature`
   (the Shopify missing-webhook-id precedent), identical for every
   failure mode; nothing is stored, looked up or marked processed. The
   failure is logged by category only (`missing`, `empty`, `not_string`,
   `too_long`, `invalid_characters`), never the raw id, its length, the
   signature or the body. The id is compared exactly, case-sensitively,
   with no trimming.
4. Parse the JSON. Not an object, no `event` string, or an `event`
   that is empty or longer than 64 characters (the length of
   `BillingEvent.event_type`) → `400 invalid_payload`, nothing stored and
   nothing looked up. An over-long value is never truncated, and the
   value is not logged (decided 2026-10-01).
5. Take `payload.subscription.entity.id` as the provider ref. An event
   without a subscription entity is not a billing event for ReviewFlow →
   `200`, nothing stored. A ref that is present but is not a string, or
   does not match `^[A-Za-z0-9_]{1,64}$`, is handled exactly like step 6's
   no-row outcome: `200 {}`, no `BillingEvent`, no lookup, no
   reconciliation, and the raw value is never logged (a fixed reason
   and the event type only; the provider event id is not logged,
   decided 2026-10-03). The check runs before `billing_ref_lookup_atomic`, using
   the same shared validation as that function in `core/tenancy.py`.
6. `billing_ref_lookup_atomic(ref)` →
   `Subscription.objects.for_lookup_ref(ref)`. No row → `200`, nothing
   stored (a subscription ReviewFlow did not create, or a ref replaced
   by a later checkout). A fixed reason and the event type are logged,
   never the provider event id (decided 2026-10-03).
7. `tenant_context(subscription.merchant_id)` + `tenant_atomic()`:
   - Insert the `BillingEvent` inside a savepoint. A unique violation
     (SQLSTATE `23505`) of the `(provider, provider_event_id)` constraint
     → duplicate → `200`, no enqueue, no other write. Any other
     `IntegrityError` is not a duplicate: it is raised, nothing is
     stored and nothing is enqueued (decided 2026-10-01). The project
     supports PostgreSQL only (`config.settings`), whose driver exposes
     the SQLSTATE and constraint name.
   - No `PaymentAttempt` is written here (decided 2026-10-01): the
     payload carries no invoice and so no `paid_at`. The ledger row is
     written by the sync from the qualifying paid invoice.
   - `transaction.on_commit`: enqueue `sync_subscription(merchant_id)`
     with the robust-enqueue pattern (a broker failure is logged by
     class name only and never becomes a `500`). Every committed new
     event enqueues its own task; nothing coalesces or suppresses an
     enqueue (decided 2026-10-01).
   - The task fetches from Razorpay only while the merchant has an
     unprocessed `BillingEvent`; otherwise it ends with no provider
     call. This is safe because `processed_at` is set only by a settled
     sync whose fetch started more than the clock-skew margin (5
     seconds) after the event was received, and a task is enqueued only
     after its event committed: when it runs, its event is either still
     unprocessed (it fetches) or was already observed by such a sync. An
     event received within the margin of a fetch stays unprocessed, so
     the next task (or the sweep) fetches again. A replayed signed body
     under fresh event ids stores one event and enqueues one task per
     delivery; those tasks collapse into one fetch only once the events
     are older than the margin. While the period is not proven paid (or
     a fetch fails) events stay unprocessed and every task fetches.
   - Clock-skew margin. `created_at` is stamped by the web host and
     `fetch_started_at` by the worker host. The 5-second margin
     (`EVENT_CLOCK_SKEW`) absorbs ordinary skew between NTP-synced hosts.
     It is a mitigation, not a guarantee: a skew larger than the margin
     can still mark an event the fetch did not observe, and that event's
     task would then skip. The cutoff is strict (an event exactly on it
     is not marked), and `processed_at` and `updated_at` take one
     timestamp.
   - Recovery: an event whose enqueue was lost (broker failure, or the
     process exits between commit and enqueue) stays unprocessed and is
     observed by the next event's task, or by the 15-minute sweep. No
     enqueue of a later event is ever suppressed by an earlier one.
8. `200 {}`.

The merchant comes only from the `Subscription` row found by the ref
ReviewFlow stored at checkout. `notes`, customer fields and every other
payload value are ignored for tenancy.

The receiver does one signature check, one lookup and one insert, and
makes no provider call, because Razorpay counts a delivery as failed
unless it gets a `2xx` within 5 seconds (§M, V6).

### Persist first, then process
- The `BillingEvent` row is committed before any processing. The
  webhook request itself makes no provider call.
- Processing is `sync_subscription`: fetch the subscription's **current
  state from the Razorpay API** and converge to it. The webhook is only
  a signal that something changed.
- The payload is not stored, and no payload fact is written to the
  ledger: a payment is recorded only by the sync, from the qualifying
  paid invoice.

### Duplicates, out-of-order, retries
- **Duplicate delivery:** stopped by `UNIQUE(provider,
  provider_event_id)`. Even without it, a second sync converges to the
  same state and writes no transition and no audit row.
- **Out-of-order delivery:** harmless, because transitions come from the
  fetched current state, not from the event sequence. A stale
  `subscription.pending` arriving after `subscription.charged` fetches
  `active` and changes nothing.
- **Stale apply:** two syncs can fetch at different moments and apply in
  the opposite order. Each sync records its fetch start time; under the
  `Subscription` row lock it is applied only if
  `fetch_started_at > provider_synced_at` and the row still holds the
  same ref. A stale fetch is dropped.
- **Provider API failure during sync:** nothing is applied, the events
  stay unprocessed, and the maintenance sweep retries on its next tick.
  No Celery `eta`/`countdown` retry is used.
- **ReviewFlow down when Razorpay delivers:** Razorpay retries with
  exponential backoff for 24 hours, then disables the webhook until it
  is re-enabled by hand in the Razorpay Dashboard (V6). Independently,
  the sweep re-syncs every subscription in
  a non-settled state, so a lost webhook is recovered without it.
- **Payment succeeds, webhook delayed:** the subscription stays
  `INCOMPLETE` (or lapsed `ACTIVE`) until the webhook or the next sweep
  tick (at most 15 minutes). Entitlement is never granted on the
  browser's word (O8).

### Error handling and audit
- Every rejection uses the standard error shape. `401` responses are
  identical for a missing signature, a bad signature and a missing event
  id.
- Logs carry the event id, event type and exception class name only.
  Never the body, the signature, a payment id or a secret.
- Receiving an event writes no `AuditLog` row. The state transition it
  causes does (see "Audit").

## API endpoints
Base `/api/v1/`. All dashboard endpoints are session only
(`SessionAuthentication`); none opts into API keys, so a key can never
read or change billing. `merchant_id` always comes from the session's
tenant context; any `merchant_id`, `status`, `subscription_id` or price
in a request body is ignored. Errors use
`{ error: { code, message, field_errors? } }`.

**Roles (O11, approved 2026-09-30).** OWNER has full billing access.
ADMIN has read-only billing access. MANAGER and VIEWER have no billing
access (`403` on every billing endpoint). "Billing read" is
`GET /billing/plans` and `GET /billing/subscription` without the
`checkout` object; "billing mutation" is everything else, including
opening Razorpay Checkout. PRD, `Security-Architecture.md` and
`API-Specification.md` are updated to state that distinction.

### `GET /billing/plans` **(new; approved 2026-09-30, O12)**
- Auth: session. Roles: OWNER, ADMIN.
- Lists `Plan` rows with `is_active = True`, ordered by `monthly_price`.
- Cursor pagination per the general convention (`?cursor=`, `?limit=`
  max 100): `{ results, next_cursor }`.
- Item: `{ id, name, monthly_price, currency, quota_requests, features }`.
  Never `provider_plan_id`.
- `Plan` is global reference data, so no tenant filter applies.

### `GET /billing/subscription`
- Auth: session. Roles: OWNER, ADMIN.
- Reads only local state. It never calls the provider.
- Response `200`:
  ```
  {
    status: "INCOMPLETE" | "ACTIVE" | "PAST_DUE" | "CANCELLED" | "EXPIRED" | null,
    plan: { id, name, monthly_price, currency, quota_requests, features } | null,
    pending_plan: { ...same shape } | null,
    current_period_start, current_period_end,        // ISO 8601 or null
    cancel_at_period_end: bool,
    past_due_at, grace_ends_at, dunning_stage,       // null unless PAST_DUE
    usage: { requests_used, quota_requests, requests_remaining } | null,
    can_send: bool,
    checkout: { provider: "razorpay", key_id, subscription_id, card_change: bool } | null,
    next_action: { type, at } | null
  }
  ```
- `checkout` is present only for `INCOMPLETE` (`card_change: false`,
  the first payment) and for `PAST_DUE` while `provider_status` is
  `pending` (`card_change: true`, which the dashboard passes to
  Razorpay Checkout as `subscription_card_change`). Only in `pending`
  does a payment-method change make Razorpay charge the unpaid invoice.
  It is returned **only to an OWNER**. For an ADMIN it is always
  `null`: those values are enough to open Checkout and change the
  payment method, and changing billing is OWNER-only. The service
  takes the caller's role and decides; the serializer does not.
  Razorpay exposes no URL for fixing a failed payment: the subscription
  `short_url` is the authorization page only (`Billing-Specification.md`
  §M, V7).
- `status: null` with every other field null/false means no
  subscription row.
- `usage` is the current period's `UsageRecord` against the current
  plan's quota; `requests_remaining = max(quota - used, 0)`; null when
  there is no current period.
- `next_action.type`: `SUBSCRIBE` (no row, `CANCELLED`, `EXPIRED`),
  `COMPLETE_CHECKOUT` (`INCOMPLETE`), `UPDATE_PAYMENT_METHOD`
  (`PAST_DUE` with `provider_status = pending`; `at = grace_ends_at`;
  the dashboard opens Razorpay Checkout from the `checkout` object),
  `RESUBSCRIBE` (`PAST_DUE` with any other `provider_status`;
  `at = grace_ends_at`; the missed invoice can no longer be paid, so the
  OWNER cancels and checks out again),
  `CANCELLATION` (`cancel_at_period_end`; `at = current_period_end`),
  `PLAN_CHANGE` (`pending_plan`; `at = current_period_end`), else
  `RENEWAL` (`at = current_period_end`).
- No pagination (single object). Idempotent read.

### `POST /billing/checkout`
- Auth: session + CSRF. Role: OWNER. Throttle: `billing_write`.
- Request: `{ plan_id: <uuid> }`. `422` for a missing, malformed,
  unknown or inactive plan, or one without a `provider_plan_id`.
- **Reconcile first (normative).** If the row already holds a
  `payment_provider_ref`, `start_checkout` fetches that provider
  subscription (and, when it is `active`, its invoices) and runs them
  through `apply_provider_snapshot()` before anything else, then routes
  on the row's resulting state. A provider
  subscription is never cancelled or replaced on the strength of local
  state alone. This closes two money paths:
  - the merchant already paid the old subscription and its webhook has
    not arrived: a fetched `active` with a paid invoice for the current
    period makes the row `ACTIVE`, and the request is then handled by
    the one rule in "When the reconcile itself activates the row" below,
    instead of cancelling a paid subscription. A fetched `authenticated`, or an `active`, `pending`
    or `halted` whose current period is not proven paid, leaves the row
    `INCOMPLETE` (the mapping table), but the payer has already
    authorized and may have paid, so that provider subscription is
    never cancelled or replaced;
  - an earlier attempt cancelled the old provider subscription and then
    failed to create the new one: the fetch shows `cancelled`, the ref
    is treated as absent, and a fresh provider subscription is created
    instead of handing the browser a dead id.
  If that fetch fails, including with a permanent `4xx`: `502
  billing_provider_unavailable`, nothing changes (see "Which provider
  states may be touched").
- The ref is replaced only when the old provider subscription is
  terminal (`cancelled`, `completed`, `expired`) or has just been
  cancelled by this request. An old subscription that is still open
  (`created`; or `pending`, `halted`, or `active` without a paid current
  period, on a `CANCELLED`/`EXPIRED` row) is cancelled first. If that
  cancel fails or is refused: `502 billing_provider_unavailable`,
  nothing changes, and no new provider subscription is created, so two
  live provider subscriptions never exist for one merchant.
- `authenticated`, and `active` with a paid current period, are never
  cancelled by a checkout. Neither is any authorized provider
  subscription (`active`, `pending`, `halted`) on an `INCOMPLETE` row.
- Behavior by state after the reconcile (one service, under the
  `Subscription` row lock, or the `UNIQUE(merchant)` insert for the
  first checkout):

  | Current state | Result |
  |---|---|
  | no row, `CANCELLED`, `EXPIRED` | create a provider subscription; row → `INCOMPLETE` with the new ref, reset as in "Replacement subscription reset"; `201` with `checkout` |
  | `INCOMPLETE`, provider `created`, same plan | return the existing provider subscription; `200` with `checkout`; no provider create, no audit row |
  | `INCOMPLETE`, provider `created`, different plan | cancel the old provider subscription, then create a new one; `201` with `checkout`. If the cancel fails: `502`, nothing changes. If the cancel succeeds and the create fails: `502`; the retry's reconcile sees `cancelled` and creates afresh |
  | `INCOMPLETE`, provider `authenticated` (or `active`/`pending`/`halted` not proven paid), same plan | return the existing provider subscription; `200` with `checkout`; no provider write |
  | `INCOMPLETE`, provider `authenticated` (or `active`/`pending`/`halted` not proven paid), different plan | `409 subscription_activating`; no provider write, nothing changes |
  | `INCOMPLETE`, provider `cancelled`/`expired`, or no ref | create a provider subscription; `201` with `checkout` |
  | `CANCELLED`/`EXPIRED`, provider `authenticated` | `409 subscription_provider_state_unsupported`; no provider write, nothing changes |
  | any row except `PAST_DUE`, provider `paused` or an unrecognized status | `409 subscription_provider_state_unsupported`; no provider write, nothing changes |
  | any row except `PAST_DUE`, a known provider status whose `plan_id` maps to no `Plan` | `409 subscription_plan_unsupported`; no provider write, nothing changes |
  | `ACTIVE`, higher-priced plan | upgrade now; `200`, `checkout: null` |
  | `ACTIVE`, lower-priced plan | schedule downgrade; `200`, `checkout: null` |
  | `ACTIVE` with no provider reference | `409 subscription_provider_state_unsupported`; no provider call, nothing changes, no audit row |
  | `ACTIVE` before this request, same plan | `422 validation_error` |
  | moved into `ACTIVE` by this request's own reconcile, requested plan = the plan now in force | `200`, `checkout: null`; no further provider call, no further audit row |
  | `ACTIVE` with `cancel_at_period_end` | `409 subscription_cancelling` |
  | `PAST_DUE` | `409 subscription_past_due` (cancel first, then check out) |

- **When the reconcile itself activates the row (approved 2026-09-30,
  D3).** One rule, by what the row was before this request:
  1. The row was already `ACTIVE` before this checkout request → the
     `ACTIVE` rows of the table apply unchanged; the same plan is
     `422 validation_error`.
  2. This request's own reconcile moved the row from another status
     (`INCOMPLETE`, `CANCELLED` or `EXPIRED`) into `ACTIVE`:
     - the requested plan is the plan now in force → `200`,
       `checkout: null`, no further provider call and no further audit
       row (the reconcile's own `billing.subscription_activated` row is
       the only one). It is a same-plan no-op: the merchant has just paid
       for exactly this plan;
     - any other plan → the `ACTIVE` rows of the table apply (upgrade,
       downgrade, or `422` for a different plan at the same price).
- **Replacement subscription reset (approved 2026-09-30, D2).** Whenever
  a new provider subscription is stored on an existing row (a `CANCELLED`
  or `EXPIRED` row, or an `INCOMPLETE` row being replaced), the row is
  initialized for the new lifecycle:

  | Field | Value |
  |---|---|
  | `plan` | the requested plan |
  | `status` | `INCOMPLETE` |
  | `payment_provider_ref` | the new, validated Razorpay subscription ref |
  | `provider_status` | the create response's status |
  | `pending_plan` | `NULL` |
  | `cancel_at_period_end` | `False` |
  | `provider_synced_at` | `NULL` |
  | `current_period_start`, `current_period_end` | `NULL` |

  `INCOMPLETE` means the first payment of the new subscription is not yet
  confirmed. The old period and usage belong to the previous lifecycle and
  are not carried onto the row: `GET /billing/subscription` shows no
  period and `usage: null` until the new subscription is proven paid.
  Historical records are never deleted or changed by a replacement:
  `UsageRecord`, `PaymentAttempt`, `BillingEvent`, `AuditLog`. If the
  replacement fails (`502`), nothing is reset.

- Response: `{ checkout: { provider: "razorpay", key_id,
  subscription_id } | null, subscription: <GET /billing/subscription body> }`.
  `key_id` is the public Razorpay key id. The key secret and the webhook
  secret are never returned.
- Errors: `409 plan_change_unsupported` (Razorpay refuses the update:
  UPI, eMandate, domestic card; S4),
  `409 subscription_provider_state_unsupported` (the provider
  subscription is `authenticated` on a `CANCELLED`/`EXPIRED` row, or
  `paused`, or an unrecognized status; nothing is touched),
  `409 subscription_plan_unsupported` (the provider subscription's plan
  maps to no `Plan` row; nothing is touched),
  `502 billing_provider_unavailable` (provider error or timeout; nothing
  is left behind locally), `503 billing_not_configured`
  (`RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` unset).
- Idempotency: a double submit is serialized by the row lock (or the
  unique constraint on first checkout); the second request lands in the
  `INCOMPLETE, same plan` row and returns the same provider
  subscription. A repeated upgrade/downgrade to the plan already in
  force or already pending changes nothing.

### `POST /billing/subscription/cancel` **(new; approved 2026-09-30, O12)**
- Auth: session + CSRF. Role: OWNER. Throttle: `billing_write`.
- Request: empty body; anything sent is ignored.
- `ACTIVE` → provider cancel at cycle end, `cancel_at_period_end = True`.
  A provider failure here is `502` and changes nothing.
- `PAST_DUE` → status `CANCELLED` at once; the provider cancel is
  attempted and, if it fails or is refused, retried by the sweep. The
  response is `200` either way.
- Response `200` with the `GET /billing/subscription` body.
- Idempotent: a second call on a subscription already cancelling returns
  `200` and writes no second audit row.
- `409 subscription_not_cancellable` for no row, `INCOMPLETE`,
  `CANCELLED`, `EXPIRED`. `502 billing_provider_unavailable` only for
  the `ACTIVE` case.
- **Order of checks (normative; approved 2026-09-30),** under the
  `Subscription` row lock, inside the service's own `tenant_atomic()`:
  1. No row, `INCOMPLETE`, `CANCELLED` or `EXPIRED` →
     `409 subscription_not_cancellable`. No provider call.
  2. `ACTIVE` with `cancel_at_period_end` already `True` → `200`,
     unchanged: no provider fetch, no provider call, no audit row. This is
     the existing idempotency rule and it is checked before any
     reconcile, so the provider's state, `paused` included, can never
     turn a repeat request into an error.
  3. `ACTIVE` with `cancel_at_period_end = False` and no provider
     reference → `409 subscription_provider_state_unsupported`: no
     provider call, no local change, no audit row; repeats give the same
     answer (decided 2026-10-01).
  4. `ACTIVE` with `cancel_at_period_end = False` → **reconcile first**,
     by the same principle as checkout: fetch the provider subscription
     (and its invoices only where the reconcile rule needs them) and apply
     the snapshot through `apply_provider_snapshot()`. If the fetch fails,
     including a permanent `4xx` or a malformed entity:
     `502 billing_provider_unavailable`, no cancellation. Then:
     - the row is still `ACTIVE` and the provider status is `paused` →
       `409 subscription_provider_state_unsupported`: no provider cancel,
       no local cancellation, `cancel_at_period_end` unchanged, no audit
       row. A repeat request gives the same answer while the provider
       stays `paused`. This applies to `paused` only: it is not extended to
       a missing, unknown or `authenticated` status;
     - the row is still `ACTIVE` with any other provider status → the
       normal `ACTIVE` behavior above (provider cancel at cycle end, then
       `cancel_at_period_end = True`; a failed cancel is `502` and the
       cancellation does not happen);
     - the reconcile moved the row to another state → the rule for that
       state applies (`PAST_DUE` below, or `409
       subscription_not_cancellable`).
  5. `PAST_DUE` → no reconcile and no provider dependency: `CANCELLED` at
     once, exactly as above.
  The reconcile's own snapshot write (`provider_status`,
  `provider_synced_at` and any transition the mapping table defines) is
  persisted in its own savepoint, as in checkout, and survives a later
  `409` or `502`. It is the approved reconcile, not a cancellation.

### `POST /billing/webhooks/razorpay`
- Auth: Razorpay webhook signature. See "Webhook design".

### `GET /merchant` (existing, changed)
- `plan` stops being a hard-coded `null`: it is `{ id, name }` of the
  subscription's plan while the status is `ACTIVE` or `PAST_DUE`, else
  `null` (Decision 5). Session and API-key behavior are otherwise
  unchanged.

## Services & background tasks

### `billing/razorpay.py` — the only module that talks to Razorpay
Stdlib `urllib.request` with HTTP Basic auth and an explicit timeout
(the `integrations/shopify/services.py` helper pattern). No business
logic, no database access.
- `verify_webhook_signature(raw_body: bytes, signature: str) -> bool`
- `create_subscription(provider_plan_id: str) -> dict` — sends
  `plan_id`, `total_count = settings.RAZORPAY_SUBSCRIPTION_TOTAL_COUNT`
  (default `1200`: Razorpay has no open-ended subscription, 100 years
  is its maximum, and a monthly plan therefore runs for at most 1200
  cycles; it is a setting only because T2 is unconfirmed) and
  `customer_notify = true`. It sends no `notes`.
- `fetch_subscription(ref: str) -> dict`
- `fetch_invoices(ref: str) -> list[dict]` — `GET /v1/invoices?subscription_id=`
- `update_subscription(ref: str, provider_plan_id: str, schedule_change_at: str) -> dict`
- `cancel_subscription(ref: str, at_cycle_end: bool) -> dict`
- Raises `BillingProviderUnavailable` (network, timeout, 5xx),
  `BillingProviderRejected` (4xx, with the provider error code only),
  `BillingNotConfigured` (credentials unset). Exception text never
  contains a response body or a credential.
- No `PaymentProvider` base class: there is one provider. The module
  boundary is the seam a second provider would be cut along.

### `billing/services.py`
All functions require an active tenant context unless noted; the
merchant is never an argument taken from a caller's input.
- `list_plans() -> QuerySet[Plan]`
- `get_subscription() -> Subscription | None`
- `get_entitlement() -> Entitlement` — read-only, no lock. A frozen
  dataclass: `status`, `plan`, `period_start`, `period_end`,
  `quota_requests`, `requests_used`, `requests_remaining`, `can_send`.
  `can_send` is true only for `ACTIVE`, `now < period_end`, and
  `requests_used < quota_requests`. Phase 10 uses it for eligibility
  rule 8; it backs `GET /billing/subscription`.
- `reserve_quota_unit() -> ReservationResult` — `RESERVED`,
  `QUOTA_EXHAUSTED` or `NOT_ENTITLED`. See "Concurrency rules". Built
  and tested here; its only caller arrives in Phase 11 (Decision 4).
- `reset_usage_period(subscription) -> UsageRecord` — insert-or-ignore
  the `UsageRecord` for the subscription's current period, then return
  it. Safe to call any number of times.
- `start_checkout(*, actor: TeamMember, plan_id) -> CheckoutResult` —
  the state table under `POST /billing/checkout`. Raises
  `PlanNotAvailable` (→ `422`), `SubscriptionPastDue`,
  `SubscriptionCancelling`, `SubscriptionActivating`,
  `PlanChangeUnsupported`, `ProviderStateUnsupported`
  (`subscription_provider_state_unsupported`),
  `SubscriptionPlanUnsupported` (`subscription_plan_unsupported`)
  (→ `409`),
  `BillingProviderUnavailable` (→ `502`), `BillingNotConfigured`
  (→ `503`).
- `cancel_subscription(*, actor: TeamMember) -> Subscription` — raises
  `SubscriptionNotCancellable`, `ProviderStateUnsupported` (→ `409`),
  `BillingProviderUnavailable`.
- `receive_webhook(*, request) -> None` — the receive order above. No
  tenant context on entry. Raises `WebhookRejected` (→ `401`),
  `InvalidWebhookPayload` (→ `400`).
- `sync_subscription() -> None` — fetch the subscription outside any
  transaction; if it is `active` and the row is not `ACTIVE` or the
  period differs, fetch its invoices too, still outside any
  transaction; then apply under the row lock with the stale guard; then
  mark the merchant's `BillingEvent` rows received more than the
  clock-skew margin (5 seconds) before the fetch started as processed,
  only when the snapshot settled. A failed fetch of either kind applies
  nothing.
- `sync_pending_events() -> None` — the sync a webhook enqueues: if the
  merchant has no unprocessed `BillingEvent` it returns with no provider
  call; otherwise it calls `sync_subscription()`. Only the webhook task
  uses it; the maintenance sweep never goes through this gate.
- `current_period_paid(entity: dict, invoices: list[dict]) -> dict | None`
  — the paid-entitlement rule. Pure function, no I/O.
- `apply_provider_snapshot(subscription, entity, invoices, fetch_started_at) -> bool`
  — the single place a status, plan or period changes. Returns whether
  anything changed. Writes the audit row for a change and the
  `PaymentAttempt` for a paid invoice.
- `advance_dunning(now) -> None` — advances `dunning_stage` to the
  highest checkpoint that is due (conditional update, audit row only on
  change), performs the grace expiry at `past_due_at + 7 days`, and
  retries an owed provider cancel, but only where
  `provider_may_be_cancelled(provider_status)` holds. It writes no
  `PaymentAttempt`.
- `provider_may_be_cancelled(provider_status) -> bool` — the one shared
  "provider may be touched" predicate (see "Which provider states may be
  touched"): true for `created`, `pending`, `halted`, `active`; false for
  `authenticated`, `paused`, terminal, unrecognized and missing. Pure.
  Used by `start_checkout` and by the sweep.
- `maintenance_due(now) -> bool` — whether this merchant needs a
  maintenance run: an unprocessed `BillingEvent` at least 2 minutes old;
  an `INCOMPLETE` row with a ref updated in the last 7 days; `ACTIVE`
  with a provider reference and `current_period_end <= now` (a lapsed
  `ACTIVE` row with no reference has nothing to sync and is not swept;
  decided 2026-10-01); any `PAST_DUE`; an `EXPIRED` or
  `CANCELLED` row whose `provider_status` is not terminal.

### `core/tenancy.py`, `core/rls.py`
- `billing_ref_lookup_atomic(ref: str)` and
  `get_current_lookup_billing_ref()`: mirror
  `integration_lookup_atomic`. The ref must match
  `^[A-Za-z0-9_]{1,64}$` (`ValueError` otherwise), which keeps the
  `SET LOCAL` literal injection-safe. Refuses a merchant context;
  `transaction.atomic(durable=True)`. The same validation is exposed as a
  shared `core/tenancy.py` helper so the webhook receiver checks a ref
  before entering this context and the two cannot drift apart.
- `rls_select_by_billing_ref(table, column="payment_provider_ref")`: the
  SELECT-only `billing_ref_lookup` policy.

### Celery tasks (`billing/tasks.py`, queue `default`)
- `sync_subscription(merchant_id)` — `@tenant_task`; calls
  `services.sync_pending_events()` (the pending-event skip).
- `maintain_subscription(merchant_id)` — `@tenant_task`; calls
  `services.sync_subscription()` directly, bypassing the pending-event
  skip so the sweep always fetches. Provider sync only: it never calls
  `advance_dunning`.
- `advance_subscription_dunning(merchant_id)` — `@tenant_task`; calls
  `services.advance_dunning(now)`. Local first: the expiry and checkpoint
  writes commit before any provider call; the owed provider cancel comes
  last and is best-effort. It never fetches or applies a snapshot.
- `run_billing_maintenance()` — Beat. Reads the ids of `Merchant` rows
  that are not `DELETED` (a global, non-RLS table, exactly as
  `retry_failed_events` does) into a list, then for each id enters that
  merchant's own tenant context and, when `maintenance_due()` is true,
  enqueues `maintain_subscription(merchant_id)` **and**
  `advance_subscription_dunning(merchant_id)` on commit, as two
  independent messages. No `BYPASSRLS`, no privileged connection. It
  carries the same `ponytail:` note as `retry_failed_events`: an
  O(merchants) scan per tick.
- Tasks are thin wrappers, safe to run twice, and never use `eta` or
  `countdown`.

#### W7 failure handling (decided 2026-10-03)
- **Per-merchant isolation.** Each merchant is handled in its own
  `try`/`except Exception`. A failure while checking or enqueueing one
  merchant (a database error in its due check, a broker error on enqueue)
  is logged with the merchant id and the exception class name only, and
  the loop continues with the next merchant. Each of the two `.delay`
  calls for one merchant is guarded separately, so a failure of the first
  never skips the second. A failed merchant is tried again only on the
  next tick.
- **Dunning never waits on sync, and does not depend on `finally`.**
  Expiry runs in its own task, so a sync that raises (the D1
  `NotImplementedError`, a provider error, a bug), hangs, is killed or
  times out cannot stall the day-7 rule or the day-3/day-6 checkpoints.
  Neither task calls the other. Neither catches the other's exception, and
  the D1 `NotImplementedError` is still caught nowhere.
- **Expiry is terminal, with one exception that already exists.**
  `EXPIRED` is left only through the approved late-charge rule: provider
  `active` **and** a qualifying paid invoice for the current period (the
  paid-entitlement rule, unchanged). No other input reactivates it: not a
  provider status alone, not an unpaid or non-qualifying invoice, not a
  `pending`, `halted`, `paused`, `created`, `authenticated` or unrecognized
  status, not a stale snapshot, not a repeated or concurrent run of either
  task. Expiry itself is unconditional, idempotent and never waits on a
  provider call. See W7-O1.
- **No tight retry loops.** The only retry is the next 15-minute tick. The
  tasks set no `autoretry_for`, never call `self.retry`, and use no `eta`,
  `countdown` or retry backoff. A merchant that stays due (for example while
  D1 is open) costs at most one sync and one dunning task per tick.
- **Overlap.** A tick can enqueue a merchant again while its earlier tasks
  are still queued or running. That is allowed and uses no lock or flag
  (a Redis flag was rejected for the webhook, same reason): the stale-apply
  guard, the conditional dunning updates and the insert-or-ignore writes
  make a second run harmless, at the cost of an extra provider fetch.
- **Events.** Neither task marks a `BillingEvent` processed except through
  a settled sync (`sync_subscription`). A failed, unsettled or D1-blocked
  sync leaves its events unprocessed, so the merchant stays due.
- **Logging.** Merchant id and exception class name only. Never a provider
  response body, reference, event id, payment id or invoice id. Exceptions
  raised by these tasks carry no provider data (`BillingProviderRejected`
  carries an error code only).
- **Webhook recovery.** The sweep is the recovery path for both W6 gaps: an
  event whose enqueue was lost, and an event left unprocessed by the
  5-second clock-skew margin. Once it is at least 2 minutes old
  `maintenance_due()` is true for it, `maintain_subscription` fetches (its fetch starts more than the
  margin after the event) and marks it. The extra provider fetches this
  causes are accepted (decided 2026-10-03).

### Expiry versus sync: race rules (decided 2026-10-03)
Applies to every interleaving of `advance_subscription_dunning` (and any
other local transition: a merchant's `PAST_DUE` cancel) with
`apply_provider_snapshot`. The `Subscription` row lock, then the current
`UsageRecord` lock, serializes them: each re-reads the row under the lock
and judges its own input against the state it finds.

**Definitions.**
- A snapshot is **stale** when the row no longer holds the snapshot's ref,
  or `fetch_started_at <= provider_synced_at` (an equal fetch time is
  stale). A stale snapshot is dropped whole: nothing is written, nothing is
  marked processed, and it never overwrites a transition that a newer
  snapshot already applied.
- A snapshot fetched before a **local** transition (expiry, a merchant's
  local cancel) is not stale for that reason. Local transitions do not
  stamp `provider_synced_at`; such a snapshot is judged by the mapping
  table alone, rules 2 and 3 below.
- A **qualifying late payment** is a snapshot whose own provider entity is
  `active` and whose own fetched invoices satisfy every condition of the
  paid-entitlement rule for that entity's current period, unchanged. The
  proof is evaluated on the snapshot itself, never carried over from
  another sync, and never inferred from the provider status alone.

**Rules.**
1. **Newer sync wins.** Of two snapshots for the same ref, the one whose
   fetch started later is the one that stands; the older, applied after,
   is dropped (stale).
2. **Expiry first, non-qualifying snapshot after.** Whether that snapshot
   was fetched before or after the expiry, the row stays `EXPIRED`: the
   mapping table rejects `pending`, `halted`, terminal, `paused`,
   `created`, `authenticated`, unrecognized, and `active` without
   qualifying proof. Nothing is cleared and no `UsageRecord` is created.
   Only `provider_status` and `provider_synced_at` are recorded.
3. **Expiry first, qualifying late payment after.** The row moves
   `EXPIRED → ACTIVE` through the existing late-charge rule, whether the
   payment's fetch began before or after the expiry: a payment is a
   fact, not a snapshot that goes out of date. It follows the
   `INCOMPLETE`/`CANCELLED`/`EXPIRED → ACTIVE` effects unchanged: plan and
   period from the snapshot, `cancel_at_period_end = False`, one
   `billing.subscription_activated` audit row, the `PaymentAttempt` from
   the invoice, and a `UsageRecord` for the new period. The grace fields
   were already cleared by the expiry.
4. **Recovery first, expiry after.** A sync that recovers `PAST_DUE →
   ACTIVE` before the expiry runs leaves the expiry nothing to do: it
   re-reads the row under the lock, finds it no longer `PAST_DUE`, and
   changes nothing. The recovery is never undone by a later expiry run.
5. **Repeats are no-ops.** A second expiry or a second identical snapshot
   changes nothing and writes no second audit row.
6. **No interleaving produces** an `EXPIRED` row with a new period or
   `UsageRecord`, an `ACTIVE` row without a qualifying paid invoice, or an
   `EXPIRED` row that a non-qualifying input reactivated.
`EXPIRED` is therefore left only through rule 3 (W7-O1, kept as written).
The same rules apply to a merchant's `PAST_DUE` cancel in place of the
expiry: a `CANCELLED` row is left only through the late-charge rule, and a
recovery applied first turns the cancel into cancel at period end (see
"A cancelled subscription and later snapshots").

### Beat schedule
- `billing-maintenance`: `billing.tasks.run_billing_maintenance`, every
  900 seconds, queue `default`.
- **The entry may ship before D1 closes (decided 2026-10-03), but only
  together with its safeguards:** it is not merged unless the per-merchant
  isolation, dunning-independence, terminal-expiry, no-retry-loop,
  event-handling and Beat-registry tests listed under "Tasks" in the
  Definition of done pass. While D1 is open, the maintenance sync of a due
  merchant that needs an invoice proof raises `NotImplementedError` each
  tick (accepted; fail-closed). D1 remains a separate blocked gate.

### Adapter/provider interfaces
None of `BaseAdapter`, `WhatsAppProvider`, `GoogleSyncProvider` is
implemented or touched. Razorpay is reached only through
`billing/razorpay.py`.

### Concurrency and transaction rules
- **Lock order** is always `Subscription` row, then the current
  `UsageRecord` row. Nothing takes them in the other order.
- **Reservation** (`reserve_quota_unit`) must run inside the caller's
  `tenant_atomic()` so the increment commits with the caller's own
  write (Phase 11: the `SENDING` transition). It:
  1. reads the subscription to find the current period; not `ACTIVE`, or
     `now >= current_period_end` → `NOT_ENTITLED`;
  2. locks that period's `UsageRecord` with `SELECT ... FOR UPDATE`; no
     record → `NOT_ENTITLED` (it never creates one);
  3. re-reads the subscription after the lock is held and re-checks
     status and period (READ COMMITTED gives this statement a fresh
     snapshot);
  4. `requests_used >= plan.quota_requests` → `QUOTA_EXHAUSTED`;
  5. otherwise `requests_used = F("requests_used") + 1` → `RESERVED`.
- **Entitlement-changing transitions** (to `PAST_DUE`, `CANCELLED`,
  `EXPIRED`, a plan change, a period advance) lock the `Subscription`,
  then the current `UsageRecord`, before writing. So a reservation and a
  transition are strictly ordered: a reservation either completes before
  the transition or sees its result. "Sending disabled immediately"
  holds at the database.
- **N concurrent reservations with one unit left:** the row lock
  serializes them; exactly one returns `RESERVED`.
- **No provider call inside a row lock in background work.**
  `sync_subscription` fetches first and locks second. The exception is
  the two OWNER endpoints, which call the provider inside the
  request-wide `tenant_atomic()` with the row lock held and a short
  timeout (the Shopify link precedent). A provider failure rolls the
  request back.
- **Provider-created-but-not-committed:** if the database commit fails
  after Razorpay created a subscription, that provider subscription is
  never paid (the browser never received its id) and lapses on the
  provider side. No local row points at it.
- Phase 07 emits no "quota became available" signal. Phase 11's resume
  path is poll-based and reads `get_entitlement()`.

### Audit
`auditlog.services.record()` inside the transaction that makes the
change. Provider-driven changes have `actor=None`. Metadata holds plan
ids, plan names and statuses only: never a payment id, a provider
payload or a URL.

| Action | Actor | When |
|---|---|---|
| `billing.checkout_started` | OWNER | a provider subscription is created |
| `billing.plan_changed` | OWNER or none | upgrade applied, downgrade scheduled, scheduled downgrade applied (`metadata.effective`: `IMMEDIATE`, `SCHEDULED`, `APPLIED`) |
| `billing.cancellation_requested` | OWNER | cancel endpoint changed state |
| `billing.subscription_activated` | none | `INCOMPLETE`/`CANCELLED`/`EXPIRED` → `ACTIVE` |
| `billing.subscription_past_due` | none | → `PAST_DUE` |
| `billing.dunning_checkpoint` | none | `dunning_stage` advanced to `3` or `6` (`metadata.stage`) |
| `billing.subscription_recovered` | none | `PAST_DUE` → `ACTIVE` |
| `billing.subscription_cancelled` | none | → `CANCELLED` |
| `billing.subscription_expired` | none | → `EXPIRED` |

A sync that changes nothing writes nothing. A period advance with no
status or plan change writes nothing (routine, not privileged).

## Admin
- `Plan` is registered (`billing/admin.py`): it is global, so it needs
  no tenant context. Staff can add and edit plans. `monthly_price`,
  `currency` and `provider_plan_id` are read-only after creation (a
  Razorpay plan's amount is immutable; a price change is a new row).
  Delete is disabled.
- `Subscription`, `UsageRecord`, `PaymentAttempt` and `BillingEvent` are
  **not** registered. `TenantScopedManager` needs a tenant context, and
  the audited cross-tenant path is Phase 16 (the `accounts/admin.py` and
  spec 05 Decision 14 precedent). Manual quota adjustment is Phase 16.

## Files to change
- `config/settings.py`: `INSTALLED_APPS += "billing"`; the
  `billing-maintenance` Beat entry; throttle rate `billing_write`
  (`10/min`); `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`,
  `RAZORPAY_WEBHOOK_SECRET` (empty defaults, like the Shopify settings);
  `RAZORPAY_SUBSCRIPTION_TOTAL_COUNT` (default `1200`).
- `config/urls.py`: include `billing.urls`.
- `core/tenancy.py`, `core/rls.py`: the lookup context and policy helper.
- `accounts/middleware.py`: add `webhook-billing-razorpay` to
  `_PRE_TENANT_URL_NAMES`.
- `accounts/serializers.py`: `MerchantSerializer.get_plan` calls
  `billing.services`.
- `accounts/models.py`: the `Merchant` docstring line "`plan` arrives
  with Plan in Phase 07" is corrected. No field is added.
- `core/management/commands/seed_dev.py`: the billing fixtures
  (Decision 10).
- `.env.example`: the three `RAZORPAY_*` names.
- Docs. Every update below is approved and was applied on 2026-09-30
  in the documentation-sync step, before implementation. The
  implementation PR changes them only if the code ends up differing:
  - `03-database/Data-Dictionary.md`: `Merchant.plan_id` removed, with
    a note that the plan is `Subscription.plan` and `GET /merchant`'s
    `plan` is derived (O7); `UsageRecord.period_start`/`period_end` as
    `datetime`, the exact provider billing-cycle instants (O10);
    `Subscription.status` with `INCOMPLETE` and the period fields
    nullable while `INCOMPLETE` (S2); every **(new)** field; the
    `BillingEvent` model (O12); `PaymentAttempt` semantics and
    `dunning_stage` (Change 3 lists the exact lines).
  - `03-database/Database-Design.md`, `ERD.md`: `BillingEvent`; the
    unique constraints; the `PaymentAttempt` purpose line (Change 3).
  - `01-product/PRD.md` (role table) and
    `02-architecture/Security-Architecture.md` §Authorization: OWNER
    has full billing access, ADMIN read-only billing access, MANAGER
    and VIEWER none (O11).
  - `02-architecture/Multi-Tenancy.md` §Layer 2: a "Billing reference
    lookup — `billing_ref_lookup`" subsection.
  - `04-api/API-Specification.md` §Billing: response bodies, the two new
    endpoints, the error codes, the billing read/mutation role split
    (O11, O12); §Merchant: `plan` is derived (O7).
  - `04-api/Webhook-Specification.md`: the billing webhook row becomes
    `POST /billing/webhooks/razorpay` (O2).
  - `04-api/Authentication.md` §3: the Razorpay signature scheme.
  - `08-billing/Billing-Specification.md`: §M (the verified Razorpay
    facts) was added on 2026-09-30. Still to add after sign-off: the §K
    dunning wording (Change 3), the paid-entitlement rule next to §K
    "Recovery", the state mapping, sync-by-fetch and `INCOMPLETE`.
  - `FINAL-ARCHITECTURE-REVIEW.md` §6 and `README.md` §Final
    Architecture Decisions: the dunning wording (Change 3).
  - `01-product/Business-Rules.md` §6: the state list.
  - `02-architecture/SAD.md` §5: the Beat entry name and cadence.
  - `09-security/Security-Controls.md`: the `RAZORPAY_*` secret names.
  - `10-development/Development-Setup.md`: the `RAZORPAY_*` variables
    (applied). Its seed-output paragraph is **not** updated yet: it
    describes the shipped command, and changes in the implementation
    PR together with `seed_dev`.

## Files to create
- `billing/__init__.py`, `billing/apps.py`
- `billing/models.py`
- `billing/migrations/__init__.py`, `0001_initial.py`, `0002_rls.py`
- `billing/exceptions.py`
- `billing/razorpay.py`
- `billing/services.py`
- `billing/tasks.py`
- `billing/serializers.py`, `billing/views.py`, `billing/receivers.py`,
  `billing/urls.py`
- `billing/admin.py` (`Plan` only)
- `billing/tests/__init__.py`, `billing/tests/conftest.py`
- `billing/tests/fixtures/razorpay_subscription_<event>.json`
  (anonymized Razorpay sample payloads and entities)
- `billing/tests/test_rls.py`, `test_lookup_rls.py`, `test_quota.py`,
  `test_services.py`, `test_api.py`, `test_webhook.py`, `test_tasks.py`,
  `test_razorpay_contract.py`

## New dependencies
No new dependencies. The Razorpay client uses stdlib `urllib`, `hmac`,
`hashlib`, `base64` and `json`, as the Shopify services do. The
`razorpay` SDK is not added.

## Decisions
1. **Provider state is fetched, not replayed.** A webhook records a
   `BillingEvent` and triggers a fetch of the subscription's current
   state; transitions come only from that fetched state. This one
   mechanism covers duplicates, out-of-order delivery, lost webhooks and
   reconciliation.
2. **Webhook → merchant** goes through the provider ref stored at
   checkout and the `billing_ref_lookup` policy (Change 1). The
   signature is verified before any database read.
3. **One `Subscription` row per merchant**, `UNIQUE(merchant)`, reused
   on resubscribe. History lives in `PaymentAttempt`, `UsageRecord` and
   `AuditLog`. A late webhook for a replaced ref resolves to no row and
   is acknowledged and ignored.
4. **Phase boundary with Phase 11.** Phase 07 owns everything on the
   billing tables, including `reserve_quota_unit()` and its concurrency
   tests, because the service-layer rule puts `UsageRecord` logic in
   `billing/services.py`. Phase 11 owns everything on
   `CampaignExecution`: calling the primitive at `SCHEDULED -> SENDING`,
   mapping `QUOTA_EXHAUSTED` to `QUOTA_EXCEEDED`, the 7-day expiry, the
   resume, and leaving `NOT_ENTITLED` executions `SCHEDULED`.
   **Rule Phase 11 inherits:** `reserve_quota_unit()` holds the
   `UsageRecord` row lock until the caller's transaction commits. After
   calling it, the caller must never take a lock on the `Subscription`
   row in that transaction: that is the reverse of the lock order and
   deadlocks against a concurrent transition. Any `Subscription` lock
   the caller needs must be taken before reserving.
5. **The plan has one home: `Subscription.plan`.** `Merchant.plan_id` is
   not created. Two columns for one fact would drift, and every writer
   would have to update both. `GET /merchant`'s `plan` is derived. This
   deviates from the Data Dictionary and from spec 02's note that Phase
   07 "adds the FK". Approved on 2026-09-30 (O7).
6. **`UsageRecord` periods are `datetime`, not `date`.** The provider's
   periods are instants. Truncating them to dates needs a timezone rule
   and lets two adjacent periods share a boundary day. Storing the exact
   instants makes the record's period identical to the subscription's.
7. **`BillingEvent` stores no payload.** Identifiers are enough for
   dedup and for the sweep, and no payment or contact data is retained.
8. **`reset_usage_period` is a service, the Beat entry is the sweep.**
   Periods are per-merchant anniversaries driven by the provider's
   charge, so a monthly global tick (`SAD.md` §5) is replaced by a
   15-minute sweep that also drives dunning, expiry and reconciliation.
9. **`PaymentAttempt` is a payment ledger and nothing else.** Every row
   is a captured Razorpay payment, keyed by its `pay_...` id, enforced
   by a `CHECK` constraint. `RETRY` means a real payment captured while
   the subscription was `PAST_DUE`; `RENEWAL` means any other real
   payment. Dunning checkpoints are one column on `Subscription`
   (`dunning_stage`) plus an audit row. A separate checkpoint table was
   rejected as more schema than three stages need; a third
   `attempt_type` was rejected because a filter that analytics can
   forget is not a guarantee.
10. **Seed.** `seed_dev` creates four dev `Plan` rows (the four tier
    names, `monthly_price = 0`, placeholder quotas, no
    `provider_plan_id`) and, for the seed merchant, an `ACTIVE`
    `Subscription` on one of them with a one-year period and its
    `UsageRecord`. The values are development fixtures and say so in
    code; they are not pricing. The billing step is idempotent on its
    own, so a database seeded before Phase 07 gains it on the next run.
11. **Grace arithmetic is in absolute time.** `grace_ends_at =
    past_due_at + 7 days`; dunning checkpoints at `+0`, `+3 days`,
    `+6 days` (O9).
12. **Entitlement needs proof of payment.** Provider `active` plus a
    paid invoice for the current period, never `active` alone (the
    paid-entitlement rule).
13. **Local state never waits on a provider cancel** where the merchant
    is already not entitled: grace expiry and the `PAST_DUE` cancel are
    unconditional, and the provider cancel is owed and retried.
14. **Admin** registers `Plan` only.
15. **Payment-provider seam (decided by the user on 2026-09-30).**
    Phase 07 V1 uses `billing/razorpay.py` as the payment-provider
    seam. A generic payment-provider interface/adapter abstraction is
    not introduced in Phase 07. If a second payment provider is added
    later, provider abstraction will be introduced as part of that
    future provider work. Phase 07 implementation must not add an
    unused generic payment-provider interface.
    - `CLAUDE.md`'s Service-Layer Rule names `BaseAdapter`,
      `WhatsAppProvider` and `GoogleSyncProvider`. None of them covers a
      payment gateway, and the rule's purpose (no business logic outside
      `services.py`, one boundary per external system) is kept:
      `billing/razorpay.py` is the only module that talks to Razorpay,
      has no business logic and no database access, and is called only
      from `billing/services.py`.
    - The decision is recorded in `docs/02-architecture/SAD.md` §4.
      `docs/` wins over `CLAUDE.md`, which is not edited by this spec.

## Rules for implementation
- Django + DRF monolith. Business logic only in `billing/services.py`;
  views, serializers, the receiver, tasks and admin call it.
- Every tenant-owned billing model uses `TenantScopedManager`; RLS is
  ENABLED and FORCED on its table. `Plan` is global and documented as
  such in its model docstring, so it is not mistaken for a missed RLS
  table.
- `for_lookup_ref` is the only merchant-unscoped read, only inside
  `billing_ref_lookup_atomic` for that ref. That context never runs
  inside a merchant context, is always `durable=True`, sets its value
  only with `SET LOCAL`, and has no write policy.
- Never trust a client-supplied `merchant_id`, `plan` price, status or
  provider id. The only request input to a billing write is `plan_id`,
  validated against active `Plan` rows.
- Never take the merchant, the plan or the status from a webhook
  payload. The payload contributes only the event id, the event type
  and the subscription ref used for the lookup.
- Verify the webhook signature over the raw body before parsing and
  before any database read. Fail closed; never verify with an empty
  secret.
- PostgreSQL is the only source of truth for subscription, quota and
  payment state. Nothing billing-related is stored in or read from
  Redis except throttle counters.
- Idempotency via database constraints: `UNIQUE(merchant)`,
  `UNIQUE(merchant, period_start, period_end)`,
  `UNIQUE(provider, provider_attempt_id)`,
  `UNIQUE(provider, provider_event_id)`. Never check-then-insert.
- Every status, plan or period change goes through
  `apply_provider_snapshot()` or the two OWNER services, under the
  `Subscription` row lock, in the documented lock order.
- `reserve_quota_unit()` never creates a `UsageRecord`, never sends,
  and never runs outside a caller's transaction.
- No code path moves a row to `ACTIVE`, advances a period or creates a
  `UsageRecord` without a `current_period_paid()` result. The seed
  command is the only exception (it has no provider).
- `PaymentAttempt` rows are created only from a Razorpay payment id.
  Nothing in dunning, cancellation or expiry writes to that table.
- ReviewFlow never claims, in code names, audit actions, API fields or
  dashboard copy, to have retried a payment.
- Celery tasks take `merchant_id` first and use `@tenant_task`. No
  `eta`, no `countdown`.
- Role checks via DRF permission classes (`IsOwner`, `IsOwnerOrAdmin`),
  never inline.
- External ids are UUIDs. `provider_plan_id` is never returned by the
  API.
- Razorpay is reached only through `billing/razorpay.py`, and that
  module is called only from `billing/services.py`. Do not add a
  `PaymentProvider` base class, registry or any other unused
  payment-provider abstraction (Decision 15).
- Secrets come from the environment only. `RAZORPAY_KEY_SECRET` and
  `RAZORPAY_WEBHOOK_SECRET` are never stored in the database, logged,
  audited or returned.
- Never log a request body, a signature, a payment id, a payment URL or
  a credential. Log ids, event types and exception class names.
- Status enums `UPPER_SNAKE_CASE`; timestamps end in `_at`.
- No prices or production quotas in a migration, a fixture used outside
  tests, or settings.
- No V2 features, no bespoke admin app, no new app beyond `billing`.
- Every service function has a unit test. The webhook has fixture-based
  contract tests on anonymized Razorpay sample payloads. Razorpay is
  mocked at the `billing/razorpay.py` boundary; no test makes a network
  call.
- Do not build: `CampaignExecution` or anything on it, WhatsApp or
  Google code, a payment-history endpoint, invoices or tax documents,
  proration maths, trials, coupons, a manual sync endpoint, reminder
  delivery, staff quota adjustment.

## Razorpay verification gate
Read from Razorpay's documentation on 2026-09-30 and relied on here:
- F1. Webhook signature: `X-Razorpay-Signature`, HMAC-SHA256 over the
  raw body, keyed with the webhook secret.
- F2. `x-razorpay-event-id` is unique per event; duplicate deliveries
  are expected.
- F3. Events may arrive out of order.
- F4. Subscription events: `subscription.authenticated`, `.activated`,
  `.charged`, `.pending`, `.halted`, `.cancelled`, `.completed`,
  `.updated`, `.paused`, `.resumed`. All carry the subscription entity;
  `.charged` also carries a payment entity.
- F5. Subscription entity fields include `id`, `plan_id`, `status`,
  `current_start`, `current_end`, `short_url`, `has_scheduled_changes`.
- F6. States: created, authenticated, active, pending, halted,
  cancelled, completed, expired, paused. A failed charge → `pending`
  with provider retries; retries exhausted → `halted`; both can return
  to `active`.
- F7. Update accepts `plan_id` and `schedule_change_at` (`now` |
  `cycle_end`), only for `authenticated`/`active` subscriptions, and
  **not** for subscriptions authenticated by UPI or eMandate.

- F7 is superseded by V5 below: the limit is wider than UPI and
  eMandate.

**Verified on 2026-09-30.** Each answer, with its source URL and the
quoted text, is recorded in `docs/08-billing/Billing-Specification.md`
§M. In short:
- V1. Endpoints confirmed. There is no open-ended subscription:
  `total_count` (or `end_at`) is required and the maximum is 100 years,
  so a monthly plan uses `total_count = 1200`.
- V2. `X-Razorpay-Signature`, HMAC-SHA256 over the raw body with the
  webhook secret; `x-razorpay-event-id` is unique per event. The
  encoding is a hex digest (T3, settled from Razorpay's SDK source).
- V3. `cancel_at_cycle_end` boolean, default `false`. Cycle-end cancel
  is refused before the first billing cycle; `created`/`authenticated`
  must be cancelled immediately. Cancel on `pending`/`halted` is not
  documented (T1).
- V4. Provider retries on T+1, T+2, T+3, then `halted` with no further
  auto-charge. Razorpay emails and texts the payer an "Update Card"
  option on each failure. A `halted` subscription that returns to
  `active` does not re-attempt the missed charge.
- V5. Immediate upgrade: Razorpay invoices and charges the prorated
  difference. Updates are allowed only on `authenticated`/`active`, not
  for UPI, not for eMandate, and for domestic cards only the offer can
  be updated.
- V6. `2xx` within 5 seconds; exponential backoff for 24 hours; then the
  webhook is disabled until re-enabled in the Dashboard.
- V7. No. `short_url` is the authorization page. The payer fixes a
  payment through Razorpay's emailed link or through Checkout with
  `subscription_card_change`.
- V8. Sample payloads are published for every subscription event.
- V9. No. There is no API retry; the manual charge is Dashboard-only and
  not supported for domestic cards.

**Test-mode confirmations (T1–T7).** No Razorpay test credentials
exist in this environment, so **none of these has test-mode evidence**.
Each is classified by what it blocks. Results go into
`Billing-Specification.md` §M with the date and the observed request and
response.

| # | Question | Status | Blocks |
|---|---|---|---|
| T1 | Does cancel succeed on a `pending` or `halted` subscription? | Unresolved | **Production deployment and enabling billing (pre-production gate, amended 2026-10-03).** Not the start: local state never waits on it (Decision 13). If it fails, a `CANCELLED`/`EXPIRED` merchant whose old subscription is still open cannot check out again, and the re-checkout rule must be redesigned: stop and ask. |
| T2 | Is `total_count = 1200` accepted on a monthly plan for each payment method? | Unresolved | **Production go-live only.** The value is a setting. |
| T3 | Is the signature a lowercase hex digest? | **Resolved from primary source**, not test mode: Razorpay's official Python SDK computes `hmac.new(key, body, sha256).hexdigest()` and compares with `hmac.compare_digest` (`razorpay-python`, `razorpay/utility/utility.py`, read 2026-09-30). | Nothing. The first test-mode delivery confirms it in passing. |
| T4 | Does an immediate update between two monthly plans move the period, and if it does, does Razorpay's prorated upgrade invoice satisfy `current_period_paid()` for the new period? | Unresolved | **Production deployment and enabling billing (pre-production gate, amended 2026-10-03).** If the period moves and the upgrade invoice does not qualify, the mapping yields "new period, not proven paid": the plan is not refreshed and a merchant who paid the difference gets no higher quota. |
| T5 | Does `notify_by` work on a subscription invoice? | **Not needed.** The checkpoint model sends no reminder. | Nothing. |
| T6 | Does a domestic-card subscription reject a `plan_id` update? | Unresolved | Nothing in this spec: any provider refusal becomes `409`. It sizes the follow-up spec. |
| T7 | Do a subscription invoice's `billing_start`/`billing_end` bracket the subscription's `current_start` for the cycle that invoice pays, and does a `halted → active` recovery leave the missed invoice `issued`? | Unresolved | **Production deployment and enabling billing (pre-production gate, amended 2026-10-03).** The paid-entitlement rule fails closed, so a mismatch blocks activation instead of granting it; the window test lives in one function. |

The final review (2026-09-30) signed off Change 3 and approved O2, O7,
O10, O11 and O12, and the approved documentation updates were applied
on 2026-09-30. Nothing blocks the start of implementation.
**T1, T4 and T7 are mandatory Razorpay pre-production gates (amended
2026-10-03; they were merge gates):** each must be run against an
activated Razorpay account (test mode) and its request and observed
response recorded in `Billing-Specification.md` §M before production
deployment and before any affected billing functionality is enabled. No
evidence may be assumed or inferred from the documentation. T2 is
verified before production go-live. T6 is recorded as an observation.

### Release gates (amended 2026-10-03)
Requested by the user and approved on 2026-10-03: the activated
Razorpay account needed for D1, T1, T4 and T7 is not available, so these
move from pre-merge gates to pre-production gates. **No behavior, rule,
test assertion or fail-closed path changes.** `fetch_invoices()` keeps
raising `NotImplementedError`; no pagination or subscription behavior is
assumed.

**Pre-merge (the PR may merge when all hold):**
- Every Definition-of-done item that is verifiable locally passes: the
  full required `pytest` suite green with nothing skipped or xfailed,
  `manage.py check`, `makemigrations --check --dry-run`, the
  `/test-feature` and `/code-review-feature` gates.
- The fail-closed behavior around D1 is tested: the real
  `fetch_invoices()` raises on every path that needs a paid proof (C1-C4,
  K1, the sync and maintenance paths), and nothing grants entitlement,
  advances a period, writes a `PaymentAttempt` or `UsageRecord`, or
  writes an audit row around it.
- D1 and T1/T4/T7 are recorded as open: D1 in this spec ("Open:
  invoice retrieval (D1)"), T1/T4/T7 as Unresolved in
  `Billing-Specification.md` §M (already so). Nothing in `docs/`
  describes unverified Razorpay behavior as verified.

**Verified locally (existing tests; no Razorpay account):**
- Webhook signature verification over the raw body, event-id validation,
  duplicate delivery, out-of-order delivery, merchant isolation, the
  pre-tenant lookup and its RLS policy.
- The paid-entitlement rule as a pure function over invoice fields **in
  the shape this spec assumes**, the status mapping, the stale-snapshot
  guard, the expiry-versus-sync and cancel-versus-snapshot race rules,
  dunning checkpoints and expiry, quota reservation, the maintenance
  sweep, Beat registration, and role and tenant permissions.
- D1 fail-closed behavior and the C4 window (no local claim after
  Razorpay's update).
- A real Celery worker and a real Beat tick on disposable
  infrastructure, with Razorpay disabled.
- Mocked provider responses verify ReviewFlow's handling only. They do
  not verify Razorpay's contract.

**Requires the activated Razorpay account (pre-production gates):**
- **D1:** V-INV-1, V-INV-2 and V-INV-3 evidenced and recorded, then
  `fetch_invoices()` implemented from that evidence through its own
  reviewed change (spec, tests, `/test-feature`, `/code-review-feature`).
  Until then it stays fail-closed.
- **T1:** cancel on a `pending` or `halted` subscription.
- **T4:** whether an immediate upgrade moves the period, and whether the
  prorated invoice satisfies `current_period_paid()`.
- **T7:** invoice `billing_start`/`billing_end` versus `current_start`,
  and the `halted -> active` recovery invoice state.
- The armed stop rules (T1 fails; T7 shows the invoice fields cannot
  identify the paid cycle; T4 shows a moved period whose invoice does not
  qualify) still apply when these run: any one stops the work and comes
  back for a decision.

### Production controls and operational readiness (proposed)

**Production restriction until D1, T1, T4 and T7 pass: operational
controls and verification evidence only.** R1-R4 are operational controls:
conditions on how production is configured. V1-V4 are the verification
evidence that those conditions held at a given release. **Neither is
runtime enforcement, and no technical guarantee is implied.** Nothing in
this spec or in the code prevents someone from setting the secrets,
creating plans or registering the webhook in production; the controls
work only if the checks are performed and recorded by the people named
below. "Affected billing functionality" means checkout, plan change,
cancellation, webhook processing, reconciliation and the maintenance
sync of real subscriptions: every path that calls Razorpay or acts on
its answers.

Operational controls:
- R1. No production environment sets `RAZORPAY_KEY_ID`,
  `RAZORPAY_KEY_SECRET` or `RAZORPAY_WEBHOOK_SECRET`.
- R2. No Razorpay plan is created for production use, and no production
  `Plan` row has a `provider_plan_id`.
- R3. The production webhook URL is not registered in the Razorpay
  Dashboard.
- R4. No production `Subscription` row has a `payment_provider_ref`.

Verification evidence, required before every production deployment while
any of D1, T1, T4 and T7 is open:
- V1. Configuration: the host's secret manager shows the three
  `RAZORPAY_*` variables absent or empty, checked by name and presence,
  never by printing a value (R1).
- V2. Data: read-only counts of production `Plan` rows with a
  `provider_plan_id` and of `Subscription` rows with a
  `payment_provider_ref` are both 0 (R2, R4).
- V3. Behavior, after the deployment: `GET /api/v1/billing/plans` as an
  OWNER returns no offered plan, and a signed-looking
  `POST /api/v1/billing/webhooks/razorpay` is rejected `401` (R1, R2).
  Neither request creates or changes anything.
- V4. Razorpay Dashboard: no webhook registered for the production URL
  and no production plan (R2, R3).

**Who performs and records the checks.** The release owner of each
production deployment (the person who approves that deployment, named in
its release record) performs and records V1, V2 and V3. The Razorpay
account owner performs and records V4. Each result is recorded with the
date, the person and the outcome in that deployment's release record
(names and counts only; no secret value is displayed or recorded).

**A failed or unverified check blocks production release.** A check that
fails, cannot be performed, or has no recorded result blocks that
production release until it passes and is recorded. A missing record
counts as a failed check.

**Existing code behavior these controls rely on** (verified locally by
tests only; not a production guarantee):
- With credentials unset, every provider call raises
  `BillingNotConfigured`
  (`test_unset_credentials_raise_billing_not_configured`), and the
  maintenance sweep's provider calls stop there (the 2026-10-03
  disposable-runtime run).
- Checkout has two distinct results that must not be conflated:
  - **No offered plan** (R2: no `Plan` row with a `provider_plan_id`, or
    the requested plan is unknown, inactive or without one): `422
    validation_error`, because the plan is validated first
    (`test_checkout_rejects_a_missing_malformed_unknown_inactive_or_providerless_plan`).
  - **An offered plan with Razorpay credentials unset:** `503
    billing_not_configured`, with no provider call
    (`test_checkout_unset_credentials_is_503_and_nothing_is_called`).
- With the webhook secret unset, every delivery is rejected `401`
  (`test_an_empty_webhook_secret_rejects_even_an_empty_key_signature`).

**Lifting the restriction.** Only when D1, T1, T4 and T7 evidence is
recorded in `Billing-Specification.md` §M, `fetch_invoices()` has been
implemented from that evidence through its own reviewed change, and the
user records the decision in this spec's decision log. No subscription,
checkout, plan-change, cancellation, reconciliation or invoice behavior
is described or treated as production-ready before then.

**Operational readiness blockers (separate from D1, T1, T4 and T7).**
The billing maintenance sweep depends on a Celery Beat process and a
worker consuming the `default` queue. Neither is verified for the
deployed environment, and none of the gates above closes these:
- OR1. The deployed Render configuration is not in the repository and is
  unconfirmed: the exact Beat and worker start commands, that the worker
  consumes `default`, that exactly one Beat instance runs (no autoscaling,
  no second `beat` or `worker -B`), and how often Beat restarts or
  redeploys (a fresh Beat waits a full 900 s before its first tick).
- OR2. Alert delivery is unconfirmed: whether Render Cron Jobs or any
  other alert channel is available, and whether a failure notification
  would reach the intended person.
- OR3. Billing maintenance monitoring is a proposal only (the follow-up
  spec `07-billing-maintenance-monitoring`). It is not approved, not
  implemented, and no monitoring is operational. Until it is, a stopped
  or duplicated Beat, or a stuck worker, would raise no alert; the
  task-loss audit's "Beat misbehaves" row stays NOT VERIFIED.
Each of OR1-OR3 blocks production release until it is evidenced and
recorded by the release owner, or the user explicitly accepts the risk
in this spec's decision log. They change no billing rule or behavior.

**Stop rules — outcome:**
- "An open-ended subscription is impossible": true by the letter.
  Handled with `total_count = 1200` (100 years). Not a blocker.
- "An immediate upgrade cannot be done as described" and "the update
  limit covers the merchants' main payment method": fired. Resolved by
  the user's S4 decision.
- V9 "no API retry": fired. Resolved by Change 3.
- Still armed: T1 fails; T7 shows the invoice fields cannot identify
  the paid cycle; or T4 shows an immediate upgrade moves the period and
  its invoice does not satisfy `current_period_paid()`. Any one: stop
  and ask.

## Rollout
- Additive migrations only; no backfill, no downtime step, no backup
  step required.
- Existing merchants have no `Subscription` row after deploy. Nothing
  sends yet, so nothing regresses. Whether they must subscribe, get a
  trial, or are granted a plan before Phase 11 ships is O1.
- **Pre-production gate (amended 2026-10-03):** nothing below happens
  in production until D1, T1, T4 and T7 pass ("Release gates (amended
  2026-10-03)").
- Before production use: create the Razorpay plans, enter the four
  `Plan` rows in Django Admin with their `provider_plan_id`, set the
  three `RAZORPAY_*` secrets in the host's secret manager, and register
  the webhook URL in Razorpay for the `subscription.*` events.
- If the webhook endpoint fails for 24 hours Razorpay disables it and
  emails the configured alert address. Re-enabling is a manual Dashboard
  step; the maintenance sweep keeps non-settled subscriptions correct
  meanwhile.
- A tier priced above ₹15,000 per cycle cannot be paid by UPI AutoPay
  (§M). This constrains O3.
- With the secrets unset, checkout of an offered plan returns `503
  billing_not_configured` and the webhook rejects everything; the rest of
  the API is unaffected. When no eligible plan exists (the requested plan
  is unknown, inactive or has no `provider_plan_id`), checkout returns
  `422 validation_error` instead, because the plan is validated first.
- Billing tables are financial records and are excluded from the Phase
  15 generic purge (`Privacy-Data-Retention.md` rule 4).

## Definition of done
All verifiable with `pytest` on real PostgreSQL unless noted. Razorpay
is mocked at `billing/razorpay.py`.

**Models, migrations, RLS**
- [ ] `python manage.py migrate` applies `billing` 0001 + 0002 and
      `migrate billing zero` reverses them.
- [ ] `billing_subscription`, `billing_usagerecord`,
      `billing_paymentattempt` and `billing_billingevent` have RLS
      ENABLED + FORCED. `billing_subscription` has exactly two policies
      (`tenant_isolation`, SELECT-only `billing_ref_lookup`); the other
      three have exactly one. `billing_plan` has none (checked via
      `pg_policies`).
- [ ] Inside `tenant_atomic` for merchant A, a raw `SELECT` on each of
      the four tenant tables returns only A's rows; a raw `INSERT` with
      B's `merchant_id` fails `WITH CHECK`. With no context, a raw
      `SELECT` returns 0 rows.
- [ ] Inside `billing_ref_lookup_atomic(ref)`, a raw `SELECT` on
      `billing_subscription` returns only the row with that ref; a raw
      `UPDATE`/`DELETE` affects 0 rows; the other three tables return 0
      rows.
- [ ] `billing_ref_lookup_atomic` raises `TenantContextError` inside a
      tenant context, `ValueError` for a ref outside
      `^[A-Za-z0-9_]{1,64}$`, and `RuntimeError` when nested in another
      `atomic()`. `for_lookup_ref` raises outside the context or for a
      different ref.
- [ ] Each unique and check constraint listed under "Models" raises
      `IntegrityError` when violated (one test per constraint).
- [ ] `Subscription.objects.all()` without a tenant context raises.
- [ ] `admin.site.is_registered(Plan)` is true; it is false for the
      other four models. Plan delete is not offered.

**Quota primitive (Testing-Strategy "Billing / quota reservation")**
- [ ] `reserve_quota_unit()` returns `RESERVED` and increments
      `requests_used` by exactly 1 for an `ACTIVE` subscription with
      quota left.
- [ ] It returns `QUOTA_EXHAUSTED` and increments nothing at
      `requests_used == quota_requests`.
- [ ] It returns `NOT_ENTITLED` and increments nothing for: no row,
      `INCOMPLETE`, `PAST_DUE`, `CANCELLED`, `EXPIRED`, a lapsed period,
      and a missing `UsageRecord` (which it does not create).
- [ ] **Concurrency:** N threads reserving against one remaining unit →
      exactly one `RESERVED`, the rest `QUOTA_EXHAUSTED`, and
      `requests_used == quota_requests` afterwards.
- [ ] **Concurrency:** a reservation racing a transition to `PAST_DUE`
      never returns `RESERVED` after that transition has committed.
- [ ] A reservation rolled back by its caller leaves `requests_used`
      unchanged.
- [ ] After an upgrade, the next reservation succeeds against the higher
      quota with `requests_used` carried over.
- [ ] `get_entitlement()` reports the right `can_send`, quota and usage
      for each state above and takes no lock.
- [ ] `reset_usage_period()` called twice for one period leaves one
      `UsageRecord`; a new period creates a second record with
      `requests_used = 0` and leaves the old one untouched (no
      rollover).

**Lifecycle (services)**
- [ ] `apply_provider_snapshot()` produces exactly the cell in the
      Razorpay → ReviewFlow table for every (provider status, local
      status) pair, including "no change".
- [ ] `halted` on a `PAST_DUE` row does not produce `EXPIRED`.
- [ ] An `ACTIVE` row with a lapsed period and a provider status still
      `active` on the old cycle stays `ACTIVE`, is not `PAST_DUE`, and
      is not entitled.
- [ ] `ACTIVE → PAST_DUE` sets `past_due_at` and `dunning_stage = 0`
      and writes one `billing.subscription_past_due` audit row in one
      transaction. It writes no `PaymentAttempt`.
- [ ] At `past_due_at + 7 days` still unpaid → status `EXPIRED`,
      `past_due_at` and `dunning_stage` null, then a provider cancel is
      attempted. If the provider cancel raises or is refused, the row is
      still `EXPIRED`, and the next maintenance tick retries the cancel
      while the provider status is one that may be cancelled (`created`,
      `pending`, `halted`, `active`).
- [ ] The maintenance sweep never cancels or calls the provider for a
      `CANCELLED`/`EXPIRED` row whose `provider_status` is `authenticated`,
      `paused`, unrecognized or missing, nor for a terminal one. The
      Razorpay mock records no cancel call for them.
- [ ] Recovery inside the grace (provider `active` and a paid invoice
      for the current period) → `ACTIVE`, `past_due_at` and
      `dunning_stage` null, the period's `UsageRecord` present.

**Dunning checkpoints (Change 3)**
- [ ] `advance_dunning` sets `dunning_stage` to `3` at
      `past_due_at + 3 days` and to `6` at `+ 6 days`, with one
      `billing.dunning_checkpoint` audit row each.
- [ ] Run repeatedly at the same instant, it changes the stage once and
      writes one audit row.
- [ ] A first run at `past_due_at + 6.5 days` goes straight to stage
      `6` with one audit row.
- [ ] Each checkpoint run performs a provider fetch (the reconcile),
      and makes no charge, retry or notification call: the Razorpay mock
      records only `fetch_subscription`/`fetch_invoices`.
- [ ] After a full grace episode that ends in `EXPIRED`,
      `PaymentAttempt` has zero rows for that episode.
- [ ] A second grace episode starts again at stage `0`.
- [ ] The constraints reject `dunning_stage = 1`, a stage without
      `past_due_at`, and `past_due_at` without a stage.

**Payment ledger (Decision 9)**
- [ ] Inserting a `PaymentAttempt` whose `provider_attempt_id` does not
      start with `pay_` raises `IntegrityError` (for example a
      `dunning:` key or an empty string).
- [ ] A payment recorded while `PAST_DUE` is `RETRY`; any other is
      `RENEWAL`; every row written in this phase is `SUCCEEDED`.
- [ ] The same paid invoice applied by two syncs leaves one row. The
      webhook receiver writes no `PaymentAttempt`.
- [ ] A sync that finds a paid invoice whose `charged` webhook never
      arrived creates the `PaymentAttempt` from the invoice's
      `payment_id`.

**Paid-entitlement rule (O17)**
- [ ] `current_period_paid()` returns the invoice only when it is
      `paid`, has `amount_due == 0`, a `payment_id`, the stored
      `subscription_id`, and a billing window containing
      `current_start`. One test per failing condition: `issued`,
      `partially_paid`, `amount_due > 0`, no `payment_id`, another
      subscription's invoice, a window for another cycle, an empty list.
- [ ] **Halted recovery without payment:** `PAST_DUE`, provider
      `active`, the cycle's invoice still `issued` → the row stays
      `PAST_DUE`, `can_send` is false, `reserve_quota_unit()` returns
      `NOT_ENTITLED`, no `UsageRecord` is created, no
      `billing.subscription_recovered` audit row is written, and the
      grace clock is unchanged.
- [ ] The same row at `past_due_at + 7 days` → `EXPIRED`, and the
      provider subscription is cancelled.
- [ ] `PAST_DUE`, provider `active`, paid invoice for the current
      period → `ACTIVE`.
- [ ] `INCOMPLETE`, provider `active`, no paid invoice → stays
      `INCOMPLETE`. With a paid invoice → `ACTIVE`.
- [ ] `ACTIVE`, provider `active` on a new period, no paid invoice for
      it → the stored period does not advance, no `UsageRecord` is
      created, the row is not entitled. With the paid invoice → the
      period advances and the new `UsageRecord` exists.
- [ ] `CANCELLED` or `EXPIRED`, provider `active`, no paid invoice → no
      change. With a paid invoice → `ACTIVE`.
- [ ] An invoice fetch that raises applies nothing and leaves the events
      unprocessed.
- [ ] `GET /billing/subscription` on `PAST_DUE` returns
      `UPDATE_PAYMENT_METHOD` with a `checkout` object while
      `provider_status` is `pending`, and `RESUBSCRIBE` with
      `checkout: null` for `halted` and for an unpaid `active`.
- [ ] A sync whose `fetch_started_at` is older than
      `provider_synced_at`, or whose ref no longer matches, changes
      nothing.
- [ ] A snapshot with an unknown `plan_id` changes nothing and leaves
      events unprocessed.
- [ ] A sync that changes nothing writes no `AuditLog` row; each real
      transition writes exactly one, with no payment id or URL in its
      metadata.

**Checkout and cancel API**
- [ ] `CANCELLED`/`EXPIRED` + provider `authenticated` →
      `409 subscription_provider_state_unsupported`; any row except
      `PAST_DUE` + provider `paused` or an unrecognized status → the same
      `409`; a known status whose `plan_id` maps to no `Plan` →
      `409 subscription_plan_unsupported`. In every case no provider
      create, update or cancel call is made and the row is unchanged.
- [ ] A permanent `4xx` from the reconcile fetch → `502
      billing_provider_unavailable`, with no provider write and no local
      change. A create response whose id fails `^[A-Za-z0-9_]{1,64}$` →
      `502`, the ref is not stored and the row is unchanged.
- [ ] An `INCOMPLETE` row is reused for a same-plan checkout only when the
      fetched `plan_id` equals the requested plan's `provider_plan_id`.
- [ ] A stale `cancel_at_period_end` cannot survive a paid reactivation
      and cause `409 subscription_cancelling`.
- [ ] `POST /billing/checkout` as OWNER with no row → `201`, row
      `INCOMPLETE`, ref stored, one provider create call, one
      `billing.checkout_started` audit row.
- [ ] Repeating it with the same plan → `200`, the same
      `subscription_id`, no second provider create, no second audit row.
      Two concurrent first checkouts leave one row and one usable ref.
- [ ] `INCOMPLETE` + a different plan, provider status `created`:
      cancels the old provider subscription and stores the new ref; if
      that cancel fails → `502` and the old ref is kept.
- [ ] **Paid but not yet webhooked, then plan switched:** the row is
      `INCOMPLETE`, the provider already reports `active` with a paid
      invoice for the current period, and the OWNER posts a different
      plan → no provider cancel call is made, the row becomes `ACTIVE`
      on the paid plan, and the request is handled as an
      upgrade/downgrade of that subscription.
- [ ] **Active but not proven paid:** the row is `INCOMPLETE` and the
      provider reports `active` with no qualifying invoice. A same-plan
      checkout returns the existing id; a different-plan checkout
      returns `409 subscription_activating`; no cancel and no create
      call is made, and the row stays `INCOMPLETE`.
- [ ] **Authorized, not yet active:** the row is `INCOMPLETE` and the
      provider reports `authenticated`. A same-plan checkout returns the
      existing id with no provider write; a different-plan checkout
      returns `409 subscription_activating`; in both cases no cancel and
      no create call is made.
- [ ] **Cancel ok, create failed, retry:** the first request returns
      `502`; the retry's reconcile sees the old ref `cancelled`, creates
      a new provider subscription and returns its id, never the dead
      one.
- [ ] A checkout on an `EXPIRED` or `CANCELLED` row whose old ref the
      provider reports `active` with a paid invoice for the current
      period (a late charge) ends `ACTIVE` without creating a second
      provider subscription.
- [ ] **`ACTIVE` with no provider reference:** checkout and cancel →
      `409 subscription_provider_state_unsupported`, no provider call, row
      unchanged, no audit row, same answer on repeats; an
      already-cancelling row stays a `200` no-op; a lapsed such row is not
      `maintenance_due()` and its sync makes no provider call.
- [ ] **Reconcile activates the row (D3):** a checkout whose own
      reconcile moves the row from `INCOMPLETE`, `CANCELLED` or `EXPIRED`
      into `ACTIVE`, for the plan now in force → `200`, `checkout: null`,
      no provider call after the reconcile fetches, and exactly one audit
      row (`billing.subscription_activated`). The same request repeated
      (the row is now `ACTIVE` before the request) → `422`. A different
      plan at the same price → `422`.
- [ ] **Replacement reset (D2):** after a replacement on a `CANCELLED`,
      `EXPIRED` or replaced `INCOMPLETE` row: `plan` is the requested
      plan, `status` `INCOMPLETE`, the new ref stored, `provider_status`
      from the create response, and `pending_plan`,
      `provider_synced_at`, `current_period_start` and
      `current_period_end` are null and `cancel_at_period_end` is false.
      `GET /billing/subscription` shows no period and `usage: null`. The
      old `UsageRecord` (with its `requests_used`), `PaymentAttempt`,
      `BillingEvent` and `AuditLog` rows are all still present and
      unchanged. A failed replacement resets nothing.
- [ ] A failed reconcile fetch → `502` with no provider write and no
      local change.
- [ ] Upgrade on `ACTIVE` where the provider keeps the period →
      provider update with `now`, plan changed immediately, audit
      `effective = IMMEDIATE`. Where the returned entity carries a new
      period, the plan and period change only with a paid invoice for it
      (T4).
- [ ] Downgrade on `ACTIVE` → provider update with `cycle_end`,
      `pending_plan` set, `plan` and quota unchanged; the next period's
      sync applies it and clears `pending_plan`.
- [ ] `PAST_DUE` → `409 subscription_past_due`; same plan → `422`;
      provider rejection → `409 plan_change_unsupported`; provider
      timeout → `502` with no local change; unset credentials → `503`.
- [ ] Unknown, inactive, malformed or provider-less `plan_id` → `422`.
      `merchant_id`, `status` or a price in the body is ignored.
- [ ] `POST /billing/subscription/cancel`: `ACTIVE` → `200`,
      `cancel_at_period_end` true, still entitled; a second call → `200`
      and no second audit row; a provider failure on `ACTIVE` → `502`
      and no change; other states → `409`.
- [ ] Cancel on `PAST_DUE` → `200` and `CANCELLED`, also when the
      provider cancel raises or is refused; the owed cancel is retried
      by the next maintenance tick. No reconcile fetch is made, and a
      failing provider does not stop it.
- [ ] Cancel on an `ACTIVE` row (not yet cancelling) reconciles first:
      the Razorpay mock records `fetch_subscription` before any
      `cancel_subscription`. A failed fetch (including a `4xx` or a
      malformed entity) → `502 billing_provider_unavailable`, no cancel
      call, `cancel_at_period_end` still `False`.
- [ ] **`ACTIVE` + provider `paused`:** cancel → `409
      subscription_provider_state_unsupported`; no `cancel_subscription`
      call, `status` still `ACTIVE`, `cancel_at_period_end` unchanged, no
      `AuditLog` row; a second request gives the same `409`. A missing,
      unknown or `authenticated` provider status is not blocked by this
      rule.
- [ ] Cancel on an `ACTIVE` row already `cancel_at_period_end = True`
      → `200`, even when the provider is now `paused`; no fetch, no
      provider call, no audit row.
- [ ] Cancel on an `ACTIVE` row whose reconcile moves it to `PAST_DUE`
      follows the `PAST_DUE` rule; to `CANCELLED`/`EXPIRED` → `409
      subscription_not_cancellable`. The reconcile snapshot persists after
      a `409` or `502`.
- [ ] A checkout on a `CANCELLED`/`EXPIRED` row whose old provider
      subscription is still open cancels it before creating a new one;
      if that cancel fails → `502`, no new provider subscription, no
      local change.
- [ ] A plan change the provider refuses → `409 plan_change_unsupported`
      with no provider create call, no provider cancel call, and no
      local change.
- [ ] Roles: OWNER and ADMIN can `GET /billing/subscription` and
      `GET /billing/plans`; MANAGER and VIEWER get `403`. Only OWNER can
      `POST` checkout and cancel; ADMIN, MANAGER, VIEWER get `403`.
      Without CSRF → `403`. A valid API key with no session → `403` on
      every billing endpoint.
- [ ] `GET /billing/subscription` returns `checkout` to an OWNER for
      `INCOMPLETE`, and for `PAST_DUE` while `provider_status` is
      `pending`; it returns `checkout: null` to an ADMIN in every state.
- [ ] `GET /billing/subscription` with no row returns `status: null`;
      for each state it returns the documented `next_action.type`; it
      makes no provider call.
- [ ] `GET /billing/plans` excludes inactive plans, never returns
      `provider_plan_id`, and is cursor-paginated.
- [ ] **Tenant isolation (priority scenario):** merchant A's session
      sees only A's subscription and usage; A's checkout and cancel
      never touch B's row.
- [ ] `GET /merchant` returns `plan: { id, name }` for `ACTIVE` and
      `PAST_DUE`, `null` otherwise, for both a session and an API key.
      Every existing `accounts` test passes unchanged.

**Webhook (priority scenario: duplicate webhook)**
- [ ] A validly signed event whose subscription ref is present but not a
      string, or fails `^[A-Za-z0-9_]{1,64}$` → `200 {}`, no
      `BillingEvent`, no lookup (no `billing_ref_lookup_atomic` entered),
      no reconciliation, and the raw ref appears in no log record.
- [ ] A validly signed `subscription.charged` fixture → `200`, one
      `BillingEvent`, no `PaymentAttempt` from the receiver, a sync
      enqueued; with Celery eager, and the provider mock returning
      `active` plus a paid invoice for that period, the subscription
      becomes `ACTIVE` with the fixture's period, a `UsageRecord`, and one
      `SUCCEEDED` `PaymentAttempt` written by the sync from that invoice.
- [ ] A signed event whose `event` is empty or longer than 64 characters
      → `400 invalid_payload`, nothing stored, no lookup, no truncated
      value anywhere, the value not logged.
- [ ] The same delivery posted 5 times → one `BillingEvent`, one
      `PaymentAttempt`, one transition, one audit row.
- [ ] Out of order: `subscription.pending` delivered after
      `subscription.charged` while the provider reports `active` →
      status stays `ACTIVE`.
- [ ] Missing signature, wrong signature, a body altered after signing,
      and a missing event-id header each → the identical
      `401 invalid_signature`, with no row written and no lookup run.
- [ ] A validly signed event whose event id is empty, longer than 64
      characters, or contains whitespace, a control character or a
      non-ASCII character → the identical `401 invalid_signature`, no
      `BillingEvent`, no lookup, nothing marked processed. A non-string
      value fails the validator the same way. The log record carries the
      failure category only: never the id, its length, the signature or
      the body.
- [ ] An event id that differs only in case, or has surrounding
      whitespace, is not treated as a duplicate of the valid one.
- [ ] With `RAZORPAY_WEBHOOK_SECRET` empty, a request signed with an
      empty key → `401`.
- [ ] A signed event for an unknown ref, and a signed event with no
      subscription entity → `200`, nothing stored.
- [ ] A signed non-JSON body → `400 invalid_payload`.
- [ ] `notes.merchant_id` (or any payload field) naming merchant B on an
      event for A's ref changes only A.
- [ ] A webhook request carrying a logged-in dashboard session for
      another merchant is not tenant-wrapped and is judged by its
      signature alone.
- [ ] A broker failure at enqueue still returns `200`; the event stays
      unprocessed and the sweep picks it up.
- [ ] A replayed body under five fresh event ids stores five events and
      enqueues five tasks; running them makes one provider fetch and
      processes all five.
- [ ] A task whose merchant has no unprocessed `BillingEvent` makes no
      provider call; another merchant's unprocessed event never makes it
      fetch.
- [ ] An event received while a sync is fetching stays unprocessed after
      that sync, and its own task fetches and processes it.
- [ ] While the period is not proven paid, every task fetches and every
      event stays unprocessed.
- [ ] An event whose enqueue was lost is processed by the next event's
      task.
- [ ] Clock-skew margin: an event more than 5 seconds older than the
      fetch start is marked by a settled sync; an event inside the
      margin, exactly on the cutoff, at the fetch start or received during
      the fetch is not, and a later sync (started beyond the margin)
      marks it. `processed_at` equals `updated_at`.
- [ ] An event inserted before a fetch but committed after the sync
      marked events is not marked by it, and its own task fetches.
- [ ] A unique violation of another constraint on the `BillingEvent`
      insert is not answered as a duplicate.
- [ ] The per-IP throttle returns `429` + `Retry-After` before signature
      verification.
- [ ] No log record from these tests contains the body, the signature
      or a secret.

**Tasks**
- [ ] `sync_subscription`, `maintain_subscription` and
      `advance_subscription_dunning` take `merchant_id` first; given A's id they never read or write B's rows
      (Celery tenant-context scenario).
- [ ] Running either task twice in a row produces the same state and no
      extra rows.
- [ ] `run_billing_maintenance` enqueues only merchants for which
      `maintenance_due()` is true, skips `DELETED` merchants, and uses
      no `eta`/`countdown`.
- [ ] A provider failure inside `sync_subscription` leaves state and
      events untouched and raises nothing that loses the event.
- [ ] **Beat registry:** `CELERY_BEAT_SCHEDULE["billing-maintenance"]` has
      the task `billing.tasks.run_billing_maintenance`, a schedule of
      exactly `900.0` seconds and `options["queue"] == "default"`; that task
      name resolves to a registered Celery task (the app's task registry,
      not a string comparison), as do `maintain_subscription` and
      `advance_subscription_dunning`.
- [ ] **Fan-out:** a due merchant gets exactly one
      `maintain_subscription` and one `advance_subscription_dunning`
      message, each with its `merchant_id` as the only argument; a merchant
      that is not due gets none; `DELETED` merchants get none and
      `SUSPENDED` merchants are handled.
- [ ] **Per-merchant failure isolation:** with three due merchants, an
      error in the due check or in `.delay` for the second does not stop the
      first or the third; the failure is logged with the merchant id and
      exception class name only; a failure of the first `.delay` for a
      merchant does not skip its second; the failed merchant is not
      retried inside the same run.
- [ ] **Dunning independence:** with `sync_subscription` raising
      `NotImplementedError` (D1), `BillingProviderUnavailable`, or any other
      exception, and also when the sync task is never run at all, the
      `advance_subscription_dunning` task still expires a `PAST_DUE` row at
      `past_due_at + 7 days` and still advances the day-3 and day-6
      checkpoints. Neither task calls the other.
- [ ] **Terminal expiry:** after `EXPIRED`, running `advance_dunning` again
      changes nothing and writes no second audit row; a sync whose provider
      status is `pending`, `halted`, `paused`, `created`, `authenticated`
      or unrecognized, or `active` without a qualifying paid invoice,
      leaves the row `EXPIRED`, clears nothing and creates no `UsageRecord`;
      provider status alone never reactivates.
- [ ] **Race rules 1–6** ("Expiry versus sync: race rules"), each
      tested deterministically, with the interleaving forced between
      fetch and apply (no sleeps):
      - *Rule 1:* an older snapshot applied after a newer one is dropped
        (stale, `settled` false) and overwrites nothing, including when the
        newer one is a recovery or a late-charge reactivation.
      - *Rule 2:* a non-qualifying snapshot fetched before the expiry and
        applied after it leaves the row `EXPIRED`, for every status in the
        rule; one fetched after the expiry does the same.
      - *Rule 3:* a qualifying late payment fetched **before** the expiry
        and applied after it, and one fetched after it, both end `ACTIVE`
        with exactly one `billing.subscription_activated` audit row, one
        `PaymentAttempt` and one `UsageRecord`, and `cancel_at_period_end`
        false.
      - *Rule 4:* a recovery followed by an expiry run leaves the row
        `ACTIVE`, writes no `billing.subscription_expired` row and
        advances nothing.
      - *Rule 5:* repeating the expiry or the snapshot changes nothing.
      - *Rule 6 (two real threads):* one thread expiring and one applying
        a non-qualifying snapshot end `EXPIRED` in both orders; one expiring
        and one applying a qualifying snapshot end `ACTIVE` with a new
        period and `UsageRecord` only when the sync applies second, and
        `ACTIVE` untouched by the expiry when the sync applies first; in no
        order is there an `EXPIRED` row with a new period or an `ACTIVE` row
        without a qualifying paid invoice.
- [ ] **PAST_DUE cancel versus snapshot:** in both serial orders and
      with real threads (the second thread observed waiting on the row
      lock), for `pending`, `halted`, terminal, `created`,
      `authenticated`, `paused`, unknown, unpaid `active` and qualifying
      paid `active` snapshots: provider status alone never moves the
      `CANCELLED` row; a qualifying paid invoice fetched before the cancel
      reactivates it through the late-charge rule; a recovery applied
      first turns the cancel into cancel at period end; the cancel never
      stamps `provider_synced_at`.
- [ ] **No tight loop:** the tasks have no `autoretry_for` and call no
      `retry`; a failing merchant causes at most one sync and one dunning
      enqueue per run; no enqueue passes `eta` or `countdown`.
- [ ] **Events:** a failed, unsettled or D1-blocked maintenance sync leaves
      its `BillingEvent` rows unprocessed and the merchant still
      `maintenance_due()`; the dunning task never changes `processed_at`.
- [ ] **Recovery:** an event whose enqueue was lost, and an event left
      unprocessed by the clock-skew margin, are marked processed by the
      next `maintain_subscription` run once they are at least 2 minutes old.
- [ ] **Idempotency and isolation:** running either task twice leaves the
      same state and no extra rows; given A's id a task never reads or writes
      B's rows; no task or log line contains a provider response body,
      reference, event id, payment id or invoice id.
- [ ] **D1 behaviour:** with the real `fetch_invoices`, the maintenance
      sync raises `NotImplementedError` out of the task, activates nothing,
      advances no period and creates no `PaymentAttempt` or `UsageRecord`.

**Seed and contract**
- [ ] `python manage.py seed_dev` on a fresh database creates four
      plans, an `ACTIVE` subscription and its `UsageRecord`; on an
      already-seeded pre-Phase-07 database it adds them; a second run
      changes nothing.
- [ ] `test_razorpay_contract.py` asserts, on anonymized Razorpay sample
      payloads, the header names, the signature computation, and every
      entity field the code reads.
- [ ] `billing/razorpay.py` sends Basic auth, a timeout, and maps
      network errors, 5xx and 4xx to the three documented exceptions
      without leaking a response body.
- [ ] T1, T4 and T7 are recorded as open pre-production gates
      (amended 2026-10-03) in this spec and in `Billing-Specification.md`
      §M before merge; nothing marks them verified.
- [ ] **Pre-production (not a merge condition):** D1, T1, T4 and T7 are
      evidenced against the activated Razorpay account and recorded in
      `Billing-Specification.md` §M before production deployment and
      before any affected billing functionality is enabled. The T6
      observation is recorded. T2 is recorded before production.
- [ ] **Production controls and operational readiness (not a merge
      condition):** before each production release, the release owner
      records V1-V3 and the Razorpay account owner records V4 ("Production
      controls and operational readiness"); OR1-OR3 (Render Beat/worker
      configuration, alert delivery, monitoring not yet operational) are
      evidenced or explicitly accepted. A failed, unperformed or unrecorded
      check blocks the release. These are operational controls and evidence,
      not runtime enforcement.
- [ ] Every item of the verification list is recorded with its
      source, `docs/` still matches the shipped behavior, and no
      armed stop rule (T1, T4, T7) has
      fired.
- [ ] Full `pytest` is green. No test is skipped or xfailed.

## Out of scope
**Follow-up Phase 07 spec (`07-plan-change-replacement`):**
- Plan change for UPI, eMandate and domestic-card subscriptions by a
  replacement subscription.

**Later phases (V1):**
- `CampaignExecution`, `QUOTA_EXCEEDED`, the 7-day expiry, automatic
  resume, the dispatch-time subscription gate, refund handling —
  Phase 10 / Phase 11.
- WhatsApp sending and any WhatsApp notification — Phase 08.
- Google review features — Phase 09.
- Staff quota adjustment, merchant suspension, cross-tenant billing
  views, registering tenant billing models in Admin — Phase 16.
- Purge/retention jobs — Phase 15. Invalid-signature spike alerting and
  the legal/tax/invoice review of payment defaults — Phase 18.
- Billing usage charts — Phase 14.

**Not scheduled (would need a new decision):**
- Payment-history endpoint, invoices, GST/tax documents, receipts.
- Trials, coupons, proration maths, annual billing, multiple currencies.
- A second payment provider or a provider abstraction.
- A manual "sync now" endpoint or browser-callback activation.
- ReviewFlow-sent dunning reminders (Change 3 excludes them; T5).
- Webhook-secret rotation with a previous secret.
- Any AI, segmentation, workflow or other V2 feature.

## Open Decisions

### Signed off or decided
- **S1. `billing_ref_lookup` policy** — approved 2026-09-30.
- **S2. `INCOMPLETE` subscription status** — approved 2026-09-30.
- **Change 3 (S3 and the `PaymentAttempt` model)** — signed off
  2026-09-30. Dunning is a recovery checkpoint; `PaymentAttempt` holds
  real Razorpay payments only.
- **O2. Webhook path** — approved 2026-09-30:
  `POST /api/v1/billing/webhooks/razorpay`. `Webhook-Specification.md`
  is updated to match.
- **O7. `Merchant.plan_id`** — approved 2026-09-30: not added.
  `Subscription.plan` is the single source of truth and
  `GET /merchant`'s `plan` is derived from it.
- **O10. `UsageRecord` periods** — approved 2026-09-30:
  `period_start` and `period_end` are `DateTimeField` and hold exactly
  the provider's billing-cycle instants.
- **O11. Billing roles** — approved 2026-09-30: OWNER full billing
  access; ADMIN read-only; MANAGER and VIEWER none.
- **O12. New surface** — approved 2026-09-30, exactly as specified:
  `GET /billing/plans`, `POST /billing/subscription/cancel`, the
  `BillingEvent` model, and the **(new)** fields (`currency`,
  `provider_plan_id`, `is_active`, `pending_plan`, `past_due_at`,
  `dunning_stage`, `cancel_at_period_end`, `provider_status`,
  `provider_synced_at`).
- **S4. Plan change** — decided 2026-09-30: `409` in this spec; the
  replacement-subscription flow in a follow-up Phase 07 spec.
- **O17. `halted → active`** — resolved by the paid-entitlement rule.
- **O1.** No V1 trial. No subscription means no sending.
- **O3.** Production prices and quotas stay configurable `Plan` data.
  (Constraint: UPI AutoPay caps a subscription charge at ₹15,000.)
- **O5.** A plan change Razorpay cannot perform returns
  `409 plan_change_unsupported`.
- **O6.** `ACTIVE` cancels at period end; `PAST_DUE` cancels
  immediately (local-first, so it no longer depends on T1).
- **O8.** Activation through the webhook and the maintenance sweep only.
- **O9.** Grace expires at exactly `past_due_at + 7 days`.
- **O13.** Follow Razorpay's verified upgrade behavior. ReviewFlow
  computes no proration.
- **O14.** Billing keeps synchronizing for a `SUSPENDED` merchant.
- **O15.** Maintenance sweep every 15 minutes.
- **O16.** `billing_write` = `10/min` per user.
- **Payment-provider seam** — decided 2026-09-30 (Decision 15):
  `billing/razorpay.py` is the seam; no generic payment-provider
  interface in Phase 07.
- **`BillingEvent` processing** — decided 2026-09-30 (implementation
  checkpoint W1–W4). An event is `processed` when a provider snapshot
  fetched after it was received has been successfully reconciled and
  persisted. That does not mean `ACTIVE`, entitled or paid. A `paused` or
  unknown provider status, and every other successfully applied "no
  change" cell, marks the events processed (`provider_status` is stored and
  the "no change, logged" rule applies). Events stay unprocessed only where
  this spec says so: no qualifying invoice or a failed invoice fetch, a
  stale or dropped fetch, an unknown `plan_id`, a provider API failure.
  Not addressed by the spec, and kept unprocessed (fail closed): a
  malformed snapshot with no status or an unusable period.
- **R1 and R2 resolutions** — approved 2026-09-30 (F1–F5, two of them
  modified):
  - F1. A validly signed webhook whose subscription ref is present but
    malformed or not a string is treated as "no matching subscription":
    `200 {}`, nothing stored, no lookup, no reconciliation, raw ref not
    logged; validated before `billing_ref_lookup_atomic`, with the shared
    check kept in `core/tenancy.py`.
  - F2 (modified). `CANCELLED`/`EXPIRED` + provider `authenticated` →
    `409 subscription_provider_state_unsupported`, no provider mutation,
    no local mutation. Not `subscription_activating`.
  - F3 (modified). Provider `paused` → `409
    subscription_provider_state_unsupported`; unknown or unrecognized
    provider status → the same; a known status whose `plan_id` maps to no
    `Plan` → `409 subscription_plan_unsupported`; a provider API failure →
    the existing `502 billing_provider_unavailable`. A reused `INCOMPLETE`
    subscription must match the requested plan's `provider_plan_id`.
  - F4. A permanent `4xx` on the provider fetch fails closed: `502
    billing_provider_unavailable`, no provider subscription created or
    replaced, no local or provider mutation, retry or support
    intervention. No automatic replacement semantics.
  - F5. The id in a create-subscription response is validated against
    `^[A-Za-z0-9_]{1,64}$` before it is persisted; invalid → `502`, nothing
    persisted.
  - Consistency fix (W4). The maintenance sweep's owed provider cancel and
    checkout use one shared predicate, `provider_may_be_cancelled`: only
    `created`, `pending`, `halted`, `active`. The sweep never touches
    `authenticated`, `paused` or an unrecognized status.
  - A1 (approved). A `PAST_DUE` checkout answers `409
    subscription_past_due`, before any provider-state answer, with no
    provider or local mutation.
  - A2 (approved). `ACTIVE` or `INCOMPLETE` + a `paused` or unrecognized
    provider status → `409 subscription_provider_state_unsupported`, no
    provider or local mutation. The cancel endpoint on such an `ACTIVE` row
    is not addressed by this decision.
  - A3 (approved). A malformed provider entity (no `id`, or no string
    `status`) → `502 billing_provider_unavailable`, no local or provider
    mutation.
  - A4 (approved). A missing `provider_status` on a `CANCELLED`/`EXPIRED`
    row counts as unrecognized: neither checkout nor the maintenance sweep
    ever cancels it.
  - A5 (approved). Webhook event id: `x-razorpay-event-id` must match
    `^[\x21-\x7E]{1,64}$` (1 to 64 printable ASCII characters; no
    whitespace, no control characters). Missing, empty, non-string, longer
    than 64 characters or containing any other character → `401
    invalid_signature`, identical for every failure; no `BillingEvent`, no
    lookup, nothing marked processed; Razorpay's retries of a non-2xx are
    accepted. One warning logs the failure category only (`missing`,
    `empty`, `not_string`, `too_long`, `invalid_characters`), never the raw
    id, its length, the signature or the body. Valid ids are unchanged:
    `UNIQUE(provider, provider_event_id)`, compared case-sensitively with no
    trimming, so a redelivery is a `200` no-op. A non-string header cannot
    arrive over HTTP; the check is defensive.
- **Cancel endpoint, `ACTIVE` + provider `paused`** — approved 2026-09-30
  (S1, S3, and S2 as changed). The endpoint reconciles the provider
  subscription first, under the row lock, for an `ACTIVE` row that is not
  already cancelling. If the row is still `ACTIVE` and the provider status
  is `paused`: `409 subscription_provider_state_unsupported`, no provider
  cancel, no local cancellation, `cancel_at_period_end` unchanged, no audit
  row, the same answer on repeats while paused. A failed fetch is `502
  billing_provider_unavailable` with no cancellation. The rule is for
  `paused` only, and is not extended to missing, unknown or `authenticated`
  statuses. An `ACTIVE` row already `cancel_at_period_end = True` keeps the
  existing `200` no-op, checked before any reconcile. `PAST_DUE` is
  unchanged. See "Order of checks" under the cancel endpoint.
- **`PaymentAttempt.attempted_at`** — signed off 2026-09-30. It is the
  qualifying Razorpay invoice's `paid_at` ("the Unix timestamp, indicates at
  which the payment was made", `Billing-Specification.md` §M). `paid_at` is
  required to record a `PaymentAttempt` but is **not** a condition of
  `current_period_paid()`, whose rules are unchanged. If an otherwise
  qualifying paid invoice has no usable `paid_at`, the subscription still
  reconciles under the paid-entitlement rule and only the `PaymentAttempt`
  fails closed: no row is written, no other timestamp is substituted
  (neither ReviewFlow's current time, nor `invoice.created_at`, nor
  `payment.created_at`), no provider call is added, and the condition is
  logged with the merchant id only. No `GET /v1/payments/{id}` call is
  introduced. This replaces the earlier recording-time behavior.
- **`cancel_at_period_end` on reactivation** — decided 2026-09-30. A valid
  paid recovery into `ACTIVE` from `INCOMPLETE`, `CANCELLED` or `EXPIRED`
  sets it to `False`, so a stale cancellation cannot survive reactivation
  and cause `subscription_cancelling`. Still undefined, and left as the
  code has it: the flag on `ACTIVE` → `CANCELLED`, and on `PAST_DUE` →
  `ACTIVE`. A replacement checkout clears it (D2, below).
- **`ACTIVE` with no provider reference** — decided 2026-10-01. Only the
  dev seed creates such a row: every production `ACTIVE` comes from a
  provider snapshot, which needs a matching reference. Checkout and cancel
  answer `409 subscription_provider_state_unsupported` with no provider
  call, no local change and no audit row, the same answer on repeats; for
  cancel, the existing `200` no-op for a row already
  `cancel_at_period_end = True` is checked first, and `PAST_DUE` still
  answers `subscription_past_due` on checkout. `sync_subscription` stays a
  no-op for it. `maintenance_due()` does not count a lapsed `ACTIVE` row
  without a reference, so it is not enqueued every 15 minutes; the row is
  still not entitled once its period ends.
- **W7 decisions** — 2026-10-03. (1) Per-merchant failure isolation in
  the Beat fan-out. (2) `advance_dunning` runs in its own task, enqueued
  independently of the sync, so it runs when the sync fails, hangs or is
  killed (a `finally` alone does not survive a killed worker); expiry is
  terminal except through the existing late-charge rule, with tests for
  stale, delayed and concurrent syncs. (3) The `billing-maintenance` Beat
  entry may ship before D1 closes only with the safeguards and tests
  above; D1 stays a separate blocked gate. (4) The extra provider fetches
  caused by the 5-second webhook margin are accepted. (5) The registered
  task path and the exact 900-second interval are tested. (6) W7-R2: the
  `maintain_subscription` / `advance_subscription_dunning` split and the
  independent fan-out are approved; `EXPIRED` returns to `ACTIVE` only
  through the existing qualifying paid-invoice rule (W7-O1, not made
  permanent); the expiry-versus-sync race rules are written out in
  "Expiry versus sync: race rules", and the older wording "the
  maintenance task syncs once more before expiring" is withdrawn.
- **Release-gate amendment** — 2026-10-03, requested by the user
  (approved 2026-10-03). D1, T1, T4 and T7 move from pre-merge
  to pre-production gates because the activated Razorpay account is
  not available. No behavior, rule, test assertion or fail-closed
  path changes; production deployment and enabling affected billing
  functionality stay blocked until they pass. The production
  restriction is an operational control with recorded verification,
  not runtime enforcement. Render Beat/worker/alert delivery and the
  unimplemented monitoring are separate operational readiness
  blockers (OR1-OR3). See "Release gates (amended 2026-10-03)".
- **Code-review fix-all decisions** — 2026-10-03. (1) A merchant's
  `PAST_DUE` cancel is not sticky: the existing late-charge rule can
  reactivate the row from a snapshot fetched before the cancel; provider
  status alone never can (documented, no mapping change). (2) Reactivation
  inside the stored period reuses its `UsageRecord`; quota is not reset.
  (3) The two webhook "ignored" log lines no longer carry the provider
  event id (receive order steps 5 and 6 updated accordingly).
- **W7-O1 (resolved 2026-10-03, W7-R2: kept as written).** "Expiry is
  terminal" is implemented as: nothing but the approved late-charge rule (provider
  `active` and a qualifying paid invoice for the current period, state
  table `CANCELLED | EXPIRED --provider active AND period paid--> ACTIVE`)
  can leave `EXPIRED`. If the intent was that `EXPIRED` can never become
  `ACTIVE` through a sync at all, that changes the approved lifecycle, the
  paid-entitlement rule, checkout paths C1/C2 and the late-charge DoD
  items, and is not done here.
- **W6 review decisions** — 2026-10-01. (1) The webhook receiver writes
  no `PaymentAttempt`; the sync writes it from the qualifying paid
  invoice, with `attempted_at` = `paid_at`. No invoice-by-id fetch is
  introduced and D1 is unchanged. (2) An `event` that is empty or longer
  than 64 characters is `400 invalid_payload` at step 4: never
  truncated, never stored, not logged. (3) Code review: a replayed signed
  body under fresh `x-razorpay-event-id`s must not cause one provider
  fetch per delivery, and no committed event may be left without a sync
  that can observe it. A Redis "sync pending" flag was rejected: a lost
  claim or lost task would suppress later events' enqueues. Approved
  instead: every committed event enqueues its task (step 7 unchanged),
  and the task skips the provider fetch when the merchant has no
  unprocessed `BillingEvent`. No Redis use, no migration, no sweep.
  (4) Code review: a sync marks only events received more than 5 seconds
  before its fetch began (strict), absorbing ordinary web/worker clock
  skew. It is a mitigation, not a guarantee under arbitrary drift; the
  cost is that an event received within the margin of a fetch stays
  unprocessed until a later sync. (5) Only a unique violation of the
  `(provider, provider_event_id)` constraint is a duplicate delivery;
  any other `IntegrityError` surfaces. (6) The enqueue guard stays broad
  on purpose: a 500 after commit would make Razorpay retry the same id,
  which is then a duplicate that enqueues nothing.
- **D2. Replacement subscription reset** — approved 2026-09-30. A
  replacement on a `CANCELLED`/`EXPIRED` row, or on an `INCOMPLETE` row
  being replaced, sets `plan` (requested), `status = INCOMPLETE`, the new
  validated ref, `provider_status` (create response), and resets
  `pending_plan`, `provider_synced_at`, `current_period_start` and
  `current_period_end` to `NULL` and `cancel_at_period_end` to `False`.
  `UsageRecord`, `PaymentAttempt`, `BillingEvent` and `AuditLog` rows are
  never deleted; the old period and usage are not carried onto the new
  `INCOMPLETE` subscription. See "Replacement subscription reset".
- **D3. Reconcile → `ACTIVE`, same plan** — approved 2026-09-30. If the
  checkout request's own reconcile moves the row into `ACTIVE` and the
  requested plan is the plan now in force: `200`, `checkout: null`, no
  further provider call, no further audit row. A row already `ACTIVE`
  before the request keeps `422 validation_error` for the same plan. See
  "When the reconcile itself activates the row".

### Open: invoice retrieval (D1) — BLOCKER (pre-production gate, amended 2026-10-03)
`billing/razorpay.py::fetch_invoices` is intentionally unimplemented and
fail-closed. Nothing about Razorpay's invoice list is assumed: no
pagination, no `count`/`skip`, no completeness, no other provider call, no
fallback timestamp, and no change to `current_period_paid()`. The
`NotImplementedError` is not caught anywhere, so no path pretends a
reconciliation succeeded.

Paths blocked until it is resolved (the labels C1–C4 and K1 here name
code paths; they are not the commercial decisions C1–C3 further below):
- **C1.** Checkout reconcile of an `INCOMPLETE` row whose provider
  subscription is `active`.
- **C2.** Checkout reconcile of a `CANCELLED`/`EXPIRED` row whose provider
  subscription is `active`.
- **C3.** Checkout reconcile of an `ACTIVE` row when the provider period
  differs from the stored one.
- **C4.** An immediate upgrade, after `update_subscription("now")`, if the
  response moves the period.
- **K1.** Cancel reconcile of an `ACTIVE` row when the provider period
  differs from the stored one.
- The W4 sync and maintenance paths that need the invoice proof (every
  activation and every period advance).
- The W6 paths that depend on that reconciliation.

**C4 failure window (must stay documented).** Razorpay's update succeeds →
Razorpay may change the plan and charge the prorated difference →
ReviewFlow cannot fetch or verify the invoices → the request fails without
claiming success, and no entitlement, plan or period is fabricated
locally. The merchant can be charged for a plan ReviewFlow does not yet
serve. This is not changed until the provider behavior is verified; T4
(whether the period moves at all) gates production deployment
(amended 2026-10-03; it gated the merge).

Open verification gates (not implementation assumptions):
- **V-INV-1.** Whether Razorpay's subscription-invoices response is
  complete, paginated, or supports `count`/`skip`.
- **V-INV-2.** Its ordering and default page or list size.
- **V-INV-3.** Whether a reliable pagination-free way to retrieve the
  qualifying invoice exists.
- **T4** (immediate upgrade and proration) and **T7** (invoice and
  payment-cycle fields), as already listed.

**Evidence status (2026-10-03).** Razorpay's documentation for the
subscription-invoices endpoint documents only `subscription_id`; its
response `count` is described as the number of invoices generated for the
subscription; `count`/`skip`, defaults, maximums, ordering, a last-page
indicator and `has_more` are not documented (a CLI example shows
`--count 10 --skip 0`). An earlier test-mode probe reported `count` and
`skip` accepted, `count` capped at 100 and a response `count` equal to the
page size, but its raw output is not recorded here, no real invoices
existed to show `skip` acting as an offset, and the page-size reading
contradicts the documented meaning of `count`; none of it is relied on.
Razorpay support confirmed that the current test account offers only
standard Checkout and needs KYC for subscriptions, so test-mode evidence
is unavailable; completing KYC is the user's decision, not a workaround
made here. A support follow-up was prepared (questions: official
`count`/`skip` semantics and defaults, ordering, pagination termination,
the meaning of the response `count`, the qualifying paid-invoice fields,
and upgrade/proration behaviour). D1 is not resolved and
`fetch_invoices` stays fail-closed.

**Gate (amended 2026-10-03):** D1 is a pre-production gate, not a
merge gate. It must be resolved (V-INV-1 to V-INV-3 evidenced, and
`fetch_invoices()` implemented from that evidence in its own reviewed
change) before production deployment and before any affected billing
functionality is enabled.

### Open: Razorpay test mode
- **T1, T4, T7** are mandatory pre-production gates (amended
  2026-10-03; they were merge gates). **T2** is a production go-live
  verification. **T6** is recorded as an observation.
- None has evidence yet, and none is to be assumed. An activated
  Razorpay account is needed to run them.
- **Observation for the test-mode session (not a gate).** Record the real
  `x-razorpay-event-id` length and characters from the first deliveries, to
  confirm they fit `^[\x21-\x7E]{1,64}$`. This is an observation only, not
  an implementation gate.

### Open: cancel endpoint reconcile follow-ups
Not yet approved; they do not change the rules recorded above.
- **The reconcile needs invoices** for an `ACTIVE` row whose fetched
  period differs from the stored one: path K1 of "Open: invoice retrieval
  (D1)".

### Open: commercial decisions for the follow-up spec
They do not block this spec. They block `07-plan-change-replacement`.
- **C1. Unused paid time on an upgrade.** A replacement subscription
  charges the full new-plan price at authorization (verified: the
  authorization amount of an immediate-start subscription is the plan
  amount and is not refunded). Is the unused time on the old plan
  forfeited, refunded by staff, or refunded automatically?
- **C2. Downgrade mechanism.** Cancel and resubscribe at period end, or
  authorize the cheaper subscription now with a future start date.
- **C3. One path or two.** Use the replacement flow for every payment
  method, or keep the provider update for international cards.
