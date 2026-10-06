# Spec: Plan Change Replacement

## Overview
Delivers locked §E (upgrade immediately) and §F (downgrade at the next
period) for subscriptions whose payment method Razorpay cannot update
(UPI, eMandate, domestic card; `Billing-Specification.md` §M V5). The merged
Phase 07 answers such a plan change with `409 plan_change_unsupported`. This
follow-up keeps that provider-update path as the first attempt and, only when
Razorpay refuses the update and the refusal is classified as "update not
supported for this subscription", creates a **plan-change replacement**: a
second Razorpay subscription for the target plan. The merchant's row switches
to it only when it is proven paid by the existing paid-entitlement rule
(which needs D1). Until then the existing plan and entitlement are unchanged.

It exists now because the merged Phase 07 left exactly this item open
("Roadmap Phase", `07-billing-quota-foundation.md`), and because Phase 10 and
Phase 11 read the plan and quota it changes. It belongs to the **automation**
plane (it gates the send loop's plan and quota). It adds no ingestion and no
intelligence feature, and changes no customer-visible sending behavior.

**Terminology.** "Plan-change replacement" is a different thing from the
existing D2/D3 "replacement subscription" (a new checkout on a `CANCELLED`,
`EXPIRED` or `INCOMPLETE` row). Those rules are unchanged.

**Status of this spec: DRAFTED FROM THE APPROVED v1-v5 REVIEW (2026-10-05).**
The decisions below (C1, C2, C3, the rule changes, the §18 answers Q1-Q6) were
approved in review before this file was written. Plan Mode has been entered
and the implementation plan has been reviewed and approved (2026-10-05). The W0
amendment (2026-10-05) applies the planning-review decisions logged under "W0
amendment log" at the end of this file; it is drafted but remains uncommitted
and pending explicit user approval. No implementation code has started. **Both feature flags default to OFF, and no part of this spec grants
entitlement until D1 is resolved.** This spec does not claim production
readiness; every provider-specific assumption is a pre-production gate
("Release gates").

## Source docs
- `docs/08-billing/Billing-Specification.md` §E (upgrade), §F (downgrade),
  §G (no rollover), §I (refunds, unchanged), §K (grace, dunning, recovery),
  §L (period and usage integrity), §M (Razorpay provider facts V1-V9, T1-T7),
  §N ("Checkout reconciliation", "Plan change at the provider",
  "Paid-entitlement rule", "Razorpay status -> ReviewFlow status",
  "Cancellation and expiry")
- `docs/01-product/Business-Rules.md` §6; `docs/01-product/Feature-Scope.md`
  (billing is V1)
- `docs/02-architecture/Multi-Tenancy.md` §Layer 1 and §Layer 2 (RLS, the
  SELECT-only lookup policy), `SAD.md` §3, `Security-Architecture.md`
- `docs/03-database/Data-Dictionary.md` (Subscription), `Database-Design.md`
- `docs/04-api/API-Specification.md` (billing endpoints),
  `Authentication.md`, `Webhook-Specification.md`
- `docs/09-security/Audit-Logging.md`, `Security-Controls.md`
- `docs/10-development/Coding-Standards.md` §6-§8,
  `Testing-Strategy.md`, `Development-Setup.md`
- `docs/FINAL-ARCHITECTURE-REVIEW.md` §4, §5, §6, §8
- `docs/ROADMAP.md` Phase 07
- `.claude/specs/07-billing-quota-foundation.md` (merged; the source of truth
  for everything not changed here: "Upgrade and downgrade", "Checkout
  reconciliation", "Release gates (amended 2026-10-03)", "Open: invoice
  retrieval (D1)", "Open: commercial decisions for the follow-up spec")

## Depends on
- Phase 07 `billing` app as merged (`22b2539`): `Subscription`, `UsageRecord`,
  `PaymentAttempt`, `BillingEvent`, `billing/services.py`, `billing/razorpay.py`,
  the webhook receiver, the maintenance sweep, the `billing_ref_lookup` policy.
- Phase 02 `accounts` (OWNER role, audit log) and Phase 01 tenancy.
- **D1 (invoice retrieval) is a hard dependency for entitlement**, not for
  merging. `fetch_invoices()` stays fail-closed; this spec adds no path around
  it.

## Roadmap Phase
- Phase: 07 — Billing & quota foundation
- Completes entire phase: Yes
- Why `Yes`: the merged `07-billing-quota-foundation.md` records this item as
  the only remaining phase work ("the plan change for subscriptions Razorpay
  cannot update ... by a replacement subscription. It gets its own spec").
  Together with the merged spec, every Phase 07 roadmap item is then covered.
  The pre-production gates (D1, T1, T4, T7, OR1-OR3) are not roadmap work and
  stay open; they are recorded under "Release gates".
- `/ship-feature` still verifies completion against `docs/ROADMAP.md` and asks
  the user before marking the phase Done. This section does not mark it Done.

## Locked decisions touched
- Razorpay is the V1 payment gateway (`FINAL-ARCHITECTURE-REVIEW.md` §6;
  `Billing-Specification.md` §K) — DEPENDS ON
- Upgrade immediate; downgrade at the next period; no rollover
  (`Billing-Specification.md` §E-§G) — DEPENDS ON (delivered for the methods
  Razorpay cannot update; the rules are not amended)
- Quota reserved atomically; `UsageRecord` `UNIQUE(merchant_id, period_start,
  period_end)` (`FINAL-ARCHITECTURE-REVIEW.md` §4; §L) — DEPENDS ON
- Paid-entitlement rule: a qualifying paid invoice is required; provider
  `active` alone grants nothing (`Billing-Specification.md` §N) — DEPENDS ON
  (applied to the replacement's invoices, not relaxed)
- `PAST_DUE` 7-day grace, dunning checkpoints, no automatic downgrade during
  grace, recovery needs a paid invoice (`FINAL-ARCHITECTURE-REVIEW.md` §6;
  §K) — DEPENDS ON (a proven-paid replacement is a new proof source for the
  same recovery rule; see "Rules this follow-up changes", row 12)
- `PaymentAttempt` records payments only; idempotency (§6) — NO CHANGE
- Refund handling (`FINAL-ARCHITECTURE-REVIEW.md` §5; §I) — NO CHANGE
  (no refund or credit is computed or issued)
- Shared schema, `merchant_id` scoping, `TenantScopedManager`
  (`Multi-Tenancy.md` §Layer 1) — DEPENDS ON
- Transaction-local `SET LOCAL app.current_merchant_id`
  (`FINAL-ARCHITECTURE-REVIEW.md` §8) — DEPENDS ON
- `merchant_id` from the authenticated principal, never from request data or
  webhook payloads — DEPENDS ON
- Three auth mechanisms never mixed; webhooks fail closed `401` — DEPENDS ON
- Dispatch is poll-based; no Celery `eta`/`countdown` — DEPENDS ON
- No generic payment-provider abstraction; Razorpay only through
  `billing/razorpay.py` — NO CHANGE
- RLS is the final isolation boundary; pre-tenant reads only through a
  signed-off SELECT-only policy (`Multi-Tenancy.md` §Layer 2; signed off as
  S1, 2026-09-30) — **LOCKED DECISION CHANGE**: `billing_ref_lookup` is
  extended from `payment_provider_ref` to `payment_provider_ref` OR
  `replacement_provider_ref`
- "Two live provider subscriptions never exist for one merchant"
  (`07-billing-quota-foundation.md` "Checkout reconciliation"; a Phase 07
  decision, not a `FINAL-ARCHITECTURE-REVIEW.md` item) — **LOCKED DECISION
  CHANGE**: at most two (see "Rules this follow-up changes", row 2)

`LOCKED DECISION CHANGE — USER SIGN-OFF REQUIRED`
- **What changes:** (1) the SELECT-only `billing_ref_lookup` policy on
  `billing_subscription` matches either of two ref columns; it stays SELECT
  only, signature verification still precedes any read, and no write policy is
  keyed on it. (2) The Phase 07 rule "two live provider subscriptions never
  exist for one merchant" becomes "at most two: the entitled one plus one
  replacement (or, after a switch or an abandon, one subscription awaiting
  confirmed termination)".
- **Why:** a safe replacement needs the old subscription to stay entitled
  while the new one is authorized and paid, so two exist briefly, and webhooks
  for the replacement must reach the row without waiting for the 15-minute
  sweep.
- **Status:** approved in principle in review on 2026-10-05. Formal
  architectural sign-off is still required at PR time (Coding-Standards §8)
  and is asked for by `/ship-feature`.

## Rules this follow-up changes
Every existing Phase 07 rule changed, and the status of each. All apply only
for a kind whose flag is on; with both flags off, nothing changes. No locked
decision is amended silently.

| # | Existing rule (source) | Change | Status |
|---|---|---|---|
| 1 | A provider refusal gives `409 plan_change_unsupported`, with no second subscription and no paid subscription cancelled (07 spec S4, O5; §N) | A classified refusal creates a replacement. The old subscription is cancelled only after the replacement is proven paid (upgrade) or once it is authenticated and verified (downgrade, "commit point"). The 409 stays for the other cases. | approved |
| 2 | "Two live provider subscriptions never exist for one merchant" | At most two: the entitled one plus one replacement, or one awaiting confirmed termination | approved |
| 3 | `billing_ref_lookup` matches `payment_provider_ref` only | Matches `payment_provider_ref` OR `replacement_provider_ref`; still SELECT-only | approved; formal sign-off at PR time |
| 4 | "authenticated, and active with a paid current period, are never cancelled by a checkout"; the shared "which provider states may be touched" rule | Unchanged for checkout reconcile. Separate, kind-aware rules govern the replacement and the retired subscription ("Cancellation and retirement"): an abandoned replacement is cancelled when the provider reports `created` or `authenticated` and never when `active`; the old subscription after a switch is cancelled even when `active`. The existing predicate is not widened. | approved |
| 5 | A provider `cancelled` moves the row to `CANCELLED` (§N mapping) | While a downgrade replacement is committed, the old ref's `cancelled` does not move the row. The guard applies to an `ACTIVE` row with `replacement_provider_ref` set **and** `replacement_committed_at` set. `replacement_committed_at` is the discriminator of a committed downgrade replacement because only the downgrade commit step sets it (an upgrade replacement has no commit point); `pending_plan` is not the discriminator. An uncommitted replacement does not defer it. If the replacement fails, the old `cancelled` applies as today. | approved (W3 review, 2026-10-05) |
| 6 | Cancel endpoint order of checks | New first step: a pending replacement is handled before the merchant's own cancel; a committed downgrade replacement is cancelled too | approved (P1) |
| 7 | `maintenance_due()` categories (W7) | Rows with a replacement or retired ref are due | approved |
| 8 | `pending_plan` is a downgrade scheduled at the next provider charge | Also set by a downgrade replacement; applied at the switch | approved |
| 9 | Checkout error table; `GET /billing/subscription` shape | New codes `replacement_in_progress`, `no_credit_acknowledgement_required`, `replacement_activating`, `replacement_committed`; new `replacement` object | approved |
| 10 | Quota outcome of an upgrade | A replacement upgrade starts a fresh `UsageRecord` at 0. A provider-update upgrade is unchanged. | approved |
| 11 | Data Dictionary and audit action list | Six new `Subscription` columns; new audit actions | approved |
| 12 | Recovery of `PAST_DUE` needs a paid invoice for the current cycle (§K; §N) | A replacement proven paid by D1 proof also recovers it | approved |
| 13 | Authorized provider subscriptions are not cancelled by the sweep or checkout unless proven unpaid | A replacement the provider reports `active` is never cancelled by ReviewFlow | approved |
| 14 | An abandon never changes entitlement | Holds for every failure before the commit point. After the commit point a downgrade can't be abandoned to keep the old plan. | approved |
| 15 | `BillingProviderRejected` "carries the provider's error code only (never the description or body)" (merged `billing/exceptions.py`; Phase 07 log hygiene) | Also carries the HTTP status and a short sanitised `reason`, for the classifier only. They are excluded from `__str__`, logs, audit metadata and API bodies. | approved (planning review) |
| 16 | A replacement the provider reports `active` and proven paid on a row that is `CANCELLED` or `EXPIRED` (this spec had no rule) | Audit event for staff; no switch, no refund; a checkout on that row returns `409 replacement_activating` | approved (planning review) |
| 17 | A replacement is abandoned at the `PAST_DUE` transition (this spec's own rule, row 13 area) | An uncommitted, non-`active` replacement is also abandoned when the row ends (`CANCELLED`, `EXPIRED`) | approved (P5) |
| 18 | The downgrade commit "under the row lock" with the provider call inside the existing OWNER-endpoint exception (this spec's earlier text) | T1 DB transaction, then the provider cancel call outside any transaction and without the row lock, then T2 DB transaction | approved |

**Not changed:** locked §E and §F (delivered, not amended); "no automatic
downgrade during grace"; the paid-entitlement rule and its D1 gate; the
stale-snapshot guard; "refund handling NO CHANGE"; `UsageRecord` uniqueness
and no rollover; OWNER-only, CSRF and the `billing_write` throttle; the D2/D3
rules; Razorpay only through `billing/razorpay.py`.

## Django apps
- `billing` — touched (models, services, views, serializers, urls,
  exceptions, `razorpay.py`, migrations 0003 and 0004, tests)
- `core` — touched (`core/rls.py` helper for the two-column lookup policy;
  `core/tenancy.py` only if the lookup guard text must name both columns)
- `config` — touched (settings)
- `accounts`, `auditlog` — read only (OWNER permission class, `record()`)
- No new app. No `integrations` change.

## Models & database changes
**Migration `0003_subscription_replacement` (`Subscription`, six nullable
columns):**
- `replacement_provider_ref` — `CharField(max_length=64, null=True)`. The
  second Razorpay subscription id. Validated against
  `^[A-Za-z0-9_]{1,64}$` before it is stored.
- `replacement_expires_at` — `DateTimeField(null=True)`. The authorization
  deadline. Required whenever `replacement_provider_ref` is set.
- `replacement_committed_at` — `DateTimeField(null=True)`. The downgrade
  commit-intent marker. Only allowed when the replacement ref is set.
- `replacement_plan` — `ForeignKey(Plan, PROTECT, null=True)`. The plan the pending
  replacement is for; set exactly when `replacement_provider_ref` is (migration
  `0005_subscription_replacement_plan`, constraint
  `billing_subscription_replacement_plan_set`). It is the local record of the target
  for the repeat and in-progress answers and for `GET /billing/subscription`.
  (W5 amendment, approved 2026-10-06.)
- `retired_provider_ref` — `CharField(max_length=64, null=True)`. The one
  subscription awaiting confirmed termination: the old subscription after a
  switch, or an abandoned replacement.
- `retired_kind` — `CharField(max_length=24, null=True)`, choices
  `SWITCHED_OLD` and `ABANDONED_REPLACEMENT`. Set whenever
  `retired_provider_ref` is set. The two kinds are cancelled under opposite rules
  (see "Cancellation and retirement").
- `replacement_cancel_confirmed_at` — `DateTimeField(null=True)`. Set by T2 when
  the old subscription's cycle-end cancel is confirmed. Only allowed when
  `replacement_committed_at` is set.

**Constraints (named, `billing_subscription_*`):**
- `replacement_ref_uniq` — partial `UNIQUE(replacement_provider_ref) WHERE
  replacement_provider_ref IS NOT NULL`
- `retired_ref_uniq` — partial `UNIQUE(retired_provider_ref) WHERE
  retired_provider_ref IS NOT NULL`
- `replacement_ref_differs` — check: `replacement_provider_ref IS NULL OR
  replacement_provider_ref <> payment_provider_ref`
- `replacement_expiry_set` — check: `(replacement_provider_ref IS NULL) =
  (replacement_expires_at IS NULL)`
- `replacement_committed_needs_ref` — check: `replacement_committed_at IS
  NULL OR replacement_provider_ref IS NOT NULL`
- `retired_kind_set` — check: `(retired_provider_ref IS NULL) = (retired_kind IS
  NULL)`
- `replacement_confirmed_needs_committed` — check:
  `replacement_cancel_confirmed_at IS NULL OR replacement_committed_at IS NOT
  NULL`
- The existing constraints, `UNIQUE(merchant)` and
  `billing_subscription_ref_uniq`, are unchanged. Idempotency of replacement
  creation rests on `UNIQUE(merchant)` plus the nullability of
  `replacement_provider_ref` (at most one replacement per row, taken under the
  row lock).

**Migration `0004_billing_ref_lookup_replacement` (RLS):** replace the
`billing_ref_lookup` SELECT policy on `billing_subscription` with one whose
`USING` clause matches `payment_provider_ref` OR `replacement_provider_ref`
against `app.current_billing_ref`. The table keeps exactly two policies
(`tenant_isolation`, `billing_ref_lookup`). Reversible: the reverse restores
the single-column policy. `core/rls.py::rls_select_by_billing_ref` gains a
`columns` argument (default the single column, so migration 0002 is
unchanged); a replace helper issues `DROP POLICY` then `CREATE POLICY` in
one migration.

**Tenant scoping:** all six columns are on an RLS table that is already
ENABLED and FORCED with `tenant_isolation`. No new table. No `BYPASSRLS`.
`SubscriptionManager.for_lookup_ref(ref)` filters on either column and keeps
its guard (it works only inside `billing_ref_lookup_atomic()` for the same
ref).

Neither migration deletes or rewrites existing data. Both are reversible;
reversing 0003 drops the six columns and therefore discards any replacement or
retired data held in them (stated in the migration docstring; the round-trip
test runs with those columns empty).

## API endpoints
Under `/api/v1/billing/`, session auth + CSRF, `billing_write` throttle on
the POSTs.
- `POST /billing/checkout` (existing, extended) — an `ACTIVE`, not-cancelling
  row and a different-priced plan: the provider update is tried first and is
  unchanged. On a classified refusal with that kind's flag on, a replacement
  is created. OWNER only. New body field `acknowledge_no_credit` (boolean,
  default false). Responses added:
  - `201` with `checkout` for a new replacement
  - `200` with the existing `checkout` for a repeat of the same plan (no
    provider call, no audit row)
  - `422 no_credit_acknowledgement_required` for a replacement upgrade without
    the acknowledgement (nothing changed)
  - `409 replacement_in_progress` for any plan other than the pending target
    (including the current plan) while one is pending, or while
    `retired_provider_ref` is set (fallback path only)
  - `409 replacement_activating` for a checkout on a `CANCELLED` or `EXPIRED`
    row holding an `active` replacement
  - unchanged: provider-update `200`s and every existing `409`/`422`/`502`/
    `503`, including `409 plan_change_unsupported`
- `POST /billing/replacement/cancel` — abandon the pending replacement. OWNER
  only. `200` (idempotent), `409 replacement_activating` (the provider reports
  it `active`), `409 replacement_committed` (a committed downgrade).
- `GET /billing/subscription` (existing, extended) — adds `replacement:
  { target_plan: {id, name}, kind: "UPGRADE" | "DOWNGRADE", authorized,
  committed, effective_at } | null`, where `authorized` is derived as equal to
  `committed` (no replacement status is persisted). Read-only; no provider call. OWNER and
  ADMIN, as today. The replacement ref and the retired ref are never exposed.
- API keys get 403 on every billing endpoint, as today. No new webhook URL:
  the existing signed receiver also matches the replacement ref.

## Services & background tasks
Names are indicative; signatures follow `billing/services.py` conventions
(keyword-only, tenant context required, `TenantContextError` without one).
- `is_update_unsupported_refusal(error) -> bool` — the single classifier
  (G-0). It compares the error's `(status, provider_code, reason)` with
  `UPDATE_UNSUPPORTED_REFUSALS`, a frozenset in `billing/razorpay.py` that
  **ships empty**. Anything it does not recognise returns `False`, so the caller
  falls through to `409 plan_change_unsupported`. With the set empty nothing
  classifies, even with a flag on. G-0 evidence is recorded only by adding to
  that set in a reviewed change.
- `start_plan_change_replacement(subscription, plan, *, kind, actor,
  acknowledge_no_credit) -> CheckoutResult` — called by `start_checkout` after
  a classified refusal. Raises `BillingNotConfigured` (window setting unset),
  `NoCreditAcknowledgementRequired`, `ReplacementInProgress`,
  `BillingProviderUnavailable`.
- `commit_downgrade_replacement(subscription) -> bool` — **T1 DB transaction,
  then the provider cancel call outside any transaction and without the row
  lock, then T2 DB transaction** (see "Downgrade commit point"). Never
  schedules the old cancel before verification.
- `switch_to_replacement(subscription, entity, invoices, ...)` — the switch,
  reached only from `_apply_snapshot` after `current_period_paid()` is true for
  the replacement.
- `abandon_replacement(subscription, *, reason)` — moves the replacement ref
  to the retired slot with `retired_kind = ABANDONED_REPLACEMENT`, clears the
  replacement columns, writes the audit row. A local state move: it makes no
  provider call. Never called for a replacement the provider reports `active`.
- `cancel_replacement(*, actor)` — the OWNER endpoint's service.
- `_sync_retired(subscription)` — a sweep step with its own kind-aware
  predicate: it fetches the retired ref; a confirmed terminal status clears the
  slot; otherwise it cancels (outside any transaction) a `SWITCHED_OLD`
  subscription in any non-terminal state, or an `ABANDONED_REPLACEMENT` that is
  `created` or `authenticated`, and never one the provider reports `active`.
- `sync_subscription()` and `maintenance_due()` are extended to fetch and
  process the replacement ref and the retired ref. `advance_dunning()` is
  unchanged except that a `PAST_DUE` transition abandons a non-`active`,
  uncommitted replacement.
- Exceptions (`billing/exceptions.py`, `ReviewFlowError` with `http_status`
  and `code`): `ReplacementInProgress` (409 `replacement_in_progress`),
  `NoCreditAcknowledgementRequired` (422
  `no_credit_acknowledgement_required`), `ReplacementActivating` (409
  `replacement_activating`), `ReplacementCommitted` (409
  `replacement_committed`). The existing `BillingProviderRejected` also carries
  a bounded HTTP `status` and a short sanitised `reason` (row 15), excluded from
  `__str__`, logs and every response.
- `billing/razorpay.py`: `create_subscription(provider_plan_id, *,
  start_at=None, expire_by=None)` (existing callers unchanged);
  `cancel_subscription(ref, at_cycle_end)` already exists. The 4xx path records
  the bounded status and reason on `BillingProviderRejected`. The create body is
  unchanged when `start_at` and `expire_by` are `None`. No new provider seam, no
  provider abstraction.
- **Celery:** no new task and no new Beat entry. The existing
  `run_billing_maintenance` -> `maintain_subscription` /
  `advance_subscription_dunning` (queue `default`) carry the new work. No
  `eta`, `countdown` or retry configuration.
- **Settings:** `BILLING_REPLACEMENT_UPGRADE_ENABLED` (default `False`),
  `BILLING_REPLACEMENT_DOWNGRADE_ENABLED` (default `False`),
  `BILLING_REPLACEMENT_UPGRADE_AUTH_WINDOW` (a duration in **seconds**; required
  when the upgrade flag is on, **no default**),
  `BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN` (a duration in **seconds**;
  required when the downgrade flag is on, **no default**). A required setting that is
  unset gives `503 billing_not_configured` for that kind. Turning a flag off
  only stops new replacements of that kind; pending ones continue to be
  processed, switched or abandoned.

## Decisions (approved)
- **C1 — unused paid time: forfeited.** No automatic refund or credit, and no
  proration or refund calculation by ReviewFlow. `acknowledge_no_credit: true`
  is required **only** on the replacement-upgrade path (it forfeits the old
  plan's unused paid time; ReviewFlow does not measure that time). It is never
  required for a provider-update upgrade (Razorpay prorates) or a downgrade.
  A first request that falls to replacement without it returns `422
  no_credit_acknowledgement_required` with nothing changed; the client resends
  with `true`. If the provider update succeeds, a sent acknowledgement is
  ignored and not recorded. Any goodwill refund is a staff action in the
  Razorpay Dashboard, outside ReviewFlow.
- **C2 — downgrade at the next period.** The existing paid plan, quota and
  consumed usage stay in force until the old period end. The mechanism is a
  replacement subscription scheduled to start at the boundary. Razorpay's
  future-start behavior is documented in §M but **not verified**, and nothing
  depends on it being so (G-2). If the provider cannot do it, a downgrade by
  replacement is not delivered and the merged `409` stands.
- **C3 — Option B.** The provider update is always tried first, and
  international-card proration is retained. Replacement is only the fallback
  for a classified refusal. Consequence, accepted: an unsupported-method
  merchant and an international-card merchant get different upgrade pricing
  and different quota treatment.
- **Q1:** after the old period end a row without proof is not entitled; no
  grace is added. **Q2:** an abandoned-then-charged replacement writes an audit
  event for staff; no automatic switch, no automatic refund (intended
  behavior; the re-check is deferred until D1 and G-4, see "Out of scope"). **Q3:** a new
  replacement is blocked while `retired_provider_ref` is set. **Q4:** a
  replacement the provider reports `active` is never cancelled by ReviewFlow.
  **Q5:** the old subscription is not scheduled for cycle-end cancel until the
  downgrade replacement is created and its provider state is verified; if it
  cannot be safely maintained the downgrade does not proceed and the existing
  plan is unchanged. **Q6:** an unknown outcome of the commit is treated as
  committed.

## Behavior

### Flow on `POST /billing/checkout` (an `ACTIVE`, not-cancelling row)
1. A pending replacement (after the `PAST_DUE` 409, before any reconcile and
   before the `pending_plan` early return, so no provider call is made): the same
   plan returns the existing checkout (`200`); any other plan, including the
   current plan, returns `409 replacement_in_progress`. No update is tried.
2. Otherwise the existing path runs: reconcile, then `update_subscription`
   (`now` for an upgrade, `cycle_end` for a downgrade). Success is the merged
   behavior and nothing here applies.
3. A refusal that `is_update_unsupported_refusal()` classifies, with that
   kind's flag on and its window setting set: replacement (upgrade needs the
   acknowledgement). Anything else is `409 plan_change_unsupported` (flag off,
   unclassified refusal) or `502` (other provider failures), exactly as today.
   A downgrade replacement is also not created, and `409 plan_change_unsupported`
   is returned, when the downgrade verification is not supported (the G-2 field
   is not recorded) or when the commit cutoff (old period end minus
   `BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN`) has already passed. No payer is
   ever asked to authorize a replacement that cannot be committed.
4. The replacement is created at Razorpay (`create_subscription`; an upgrade
   starts immediately with `expire_by` from the upgrade window, a downgrade
   starts at the old `current_period_end` with `expire_by` strictly before the
   commit cutoff: exactly 1 second before it, `expire_by = commit_cutoff - 1 second`,
   where the cutoff keeps its definition, old period end minus
   `BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN`. The 1 second only makes "before"
   strict; it is not a second margin or setting). The returned id is validated, then stored with
   `replacement_expires_at`; a downgrade also sets `pending_plan`. One audit
   row `billing.replacement_started`. The row, plan, period and entitlement
   are unchanged.

### State transitions
Status values are unchanged. Only an `ACTIVE`, not-cancelling row may start a
replacement. "Unpaid or failed" means the provider reports `created`,
`authenticated`, `pending`, `halted`, `cancelled` or `expired`; `active` is
never treated as unpaid.

| Event | Result |
|---|---|
| Update refused, classified, flag on | replacement created; row and plan unchanged |
| Replacement proven paid (D1), row `ACTIVE` | switch; `ACTIVE` on the new plan |
| Replacement proven paid (D1), row `PAST_DUE` | switch; recovers to `ACTIVE`; `past_due_at` and `dunning_stage` clear. Without D1 proof nothing switches. |
| Replacement unpaid or failed, not committed | abandoned; existing plan and entitlement unchanged |
| Replacement `created` past `replacement_expires_at` | abandoned |
| Row becomes `PAST_DUE`, replacement non-`active` and uncommitted | abandoned |
| Replacement `active`, not yet proven paid | never cancelled by ReviewFlow |
| Downgrade replacement `authenticated`, verified, before the cutoff | commit (below) |
| Verification fails or can't be performed, or the cutoff has passed | no commit; abandoned if cancellable; plan unchanged |
| Committed downgrade replacement fails | not undoable (below) |
| Row ends (`CANCELLED`, `EXPIRED`) with an uncommitted, non-`active` replacement | abandoned (row 17) |
| Replacement `active` and proven paid, row `CANCELLED` or `EXPIRED` | no switch; audit event for staff; a checkout on the row gets `409 replacement_activating` (row 16) |
| Merchant cancels the subscription while one is pending | a non-`active` replacement is cancelled first (`502`, nothing changed, on failure); an `active` one gives `409 replacement_activating`; a committed downgrade replacement is cancelled too, then the normal cancel runs (P1) |
| Row `CANCELLED`, `EXPIRED`, `INCOMPLETE`, `PAST_DUE` | no replacement accepted; existing 409s apply |

**Downgrade boundary:** the old plan stays the row's plan until the switch.
Sending entitlement follows the unchanged paid-entitlement rule, so after the
old period end a row without proof is not entitled (fail closed). No grace.

### Billing and payment semantics
- **Proof:** the switch uses the existing `current_period_paid()` over the
  replacement's invoices. Provider `active` alone grants nothing. While D1 is
  open the `NotImplementedError` is caught nowhere and nothing can switch.
- **The switch** is one transaction under the `Subscription` row lock, then
  the `UsageRecord` lock: `plan` becomes the target; the period becomes the
  replacement's; `payment_provider_ref` becomes the replacement ref and the old
  ref moves to `retired_provider_ref`; the replacement columns,
  `replacement_committed_at`, `pending_plan` and `cancel_at_period_end` clear
  (a `PAST_DUE` row also clears its grace fields); a fresh `UsageRecord` for the
  new period is inserted (insert-or-ignore on the existing unique constraint);
  the ledger entry follows the existing activation rule (no new
  `PaymentAttempt` type).
- **Audit rows:** `billing.plan_changed` (effective `REPLACEMENT_IMMEDIATE` or
  `REPLACEMENT_SCHEDULED`; an upgrade also records that the acknowledgement was
  given), `billing.replacement_started`, `billing.replacement_abandoned`,
  `billing.replacement_committed`, and `billing.subscription_recovered` when a
  `PAST_DUE` row recovers. Metadata holds plan names and effect only: no refs,
  payment ids, PII or secrets.
- ReviewFlow computes no proration, credit or refund.

### Quota and usage
A switch always opens a new period with a fresh `UsageRecord` at 0; the old
period's record is untouched; no rollover (§G). A provider-update upgrade is
unchanged (consumed usage is kept when the period does not move), so the two
paths differ by design. Before the switch, the quota, the reservation and the
Phase 10/11 reads see no change. After an upgrade switch, `QUOTA_EXCEEDED`
executions inside their 7-day window are eligible under the new quota (§E).

### Cancellation and retirement
- **Retired slot:** `retired_provider_ref` holds the one subscription awaiting
  confirmed termination (the old subscription after a switch, or an abandoned
  replacement), with `retired_kind` recording which. The sweep retries its
  cancel until Razorpay reports it terminal. A new replacement is blocked while
  it is set. The cancel rule is kind-aware: `SWITCHED_OLD` is cancelled in any
  non-terminal state, including `active`; `ABANDONED_REPLACEMENT` is cancelled
  when `created` or `authenticated` and never when `active`.
- **Upgrade:** the old subscription is cancelled right after the switch and
  only then.
- **Downgrade commit point: T1 DB transaction, then the provider cancel call
  outside any transaction and without the row lock, then T2 DB transaction.**
  The provider call is not a transaction. Each step is fail-closed.
  1. **T1 (DB transaction):** lock, then re-fetch the replacement from the
     provider (never a webhook) and *verify*: status `authenticated`; plan id
     equals the target's `provider_plan_id`; its start equals the old period
     end (the field to read is gated, G-2); it has not expired and its expiry
     equals `replacement_expires_at` (the `expire_by` ReviewFlow set, commit cutoff
     minus 1 second); now is before the commit cutoff (old period
     end minus `BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN`). Any failed check,
     or any field that cannot be read, means no commit. On success record
     `replacement_committed_at` and commit the transaction, so the intent
     survives a crash.
  2. **Provider call, outside any transaction and without the row lock:**
     `cancel_subscription(old_ref, at_cycle_end=True)` with a short timeout.
  3. **T2 (DB transaction):** relock, re-check the refs, and record
     `replacement_cancel_confirmed_at` on success. A first-call
     `BillingProviderRejected` is a definite refusal: clear the intent and
     abandon, leaving the old plan unchanged. `BillingProviderUnavailable` or a
     timeout is an unknown outcome and is **treated as committed**: the
     replacement is kept and the sweep re-issues the cancel. A rejection on a
     re-issue never abandons (how Razorpay answers a repeated cancel is
     evidence-gated, G-6).
- **After commit** the downgrade cannot be abandoned to keep the old plan:
  `POST /billing/replacement/cancel` returns `409 replacement_committed`; the
  merchant may cancel the subscription instead, and that cancel also cancels the
  committed replacement, then runs the normal cancel (P1). Nothing un-cancels the old
  subscription, because Razorpay's documentation does not describe revoking a
  scheduled cancel.
- **Abandon** (uncommitted only), by `POST /billing/replacement/cancel`, an
  unpaid or failed provider status, the deadline while `created`, a
  `PAST_DUE` transition, or the row ending (`CANCELLED`, `EXPIRED`): the
  replacement ref moves to the retired slot, the
  replacement columns clear, the old plan stays, and the provider cancel is
  retried by the sweep.
- **Protected state:** a replacement the provider reports `active` is never
  cancelled by ReviewFlow (not by the sweep, a `PAST_DUE` transition, or the
  merchant); the merchant's request gets `409 replacement_activating`.
- These cancels use their own rules; the existing "which provider states may
  be touched" predicate is not widened.

### Idempotency and race handling
- At most one replacement per row (the column plus its partial unique index,
  taken under the `Subscription` row lock). A repeat of the same plan returns
  the existing checkout without a provider call.
- Lock order is `Subscription`, then `UsageRecord`, as everywhere else. The
  switch and the usage insert are insert-or-ignore on existing constraints, so
  a second run changes nothing.
- The webhook-triggered sync, the sweep, a checkout reconcile, a merchant
  cancel and the `PAST_DUE` transition can race the switch or the commit. The
  stale-snapshot guard and the ref check apply to both refs, every decision
  re-reads the row and the provider status under the lock, and the first
  proven payment wins; the others no-op. An abandon never acts on an `active`
  replacement.
- A crash between "Razorpay created it" and "ref stored" leaves an unpaid
  orphan that lapses at its `expire_by`; the merged checkout has the same
  window.

### Webhook and event handling
- The receive order is unchanged (raw-body signature first, event-id
  validation, ref validation, then the lookup). The lookup matches the current
  or the replacement ref; the merchant still comes from the Subscription row,
  never the payload.
- Events remain triggers only: state is fetched, never replayed.
  `BillingEvent.provider_ref` stores whichever ref the event named.
- Events for the retired ref do not match the row and are acknowledged
  (`200 {}`) and ignored; the sweep drives its cancellation.

### Failure and recovery
- **Create fails or the response is malformed:** `502`, nothing stored.
- **Paid but D1 blocks the proof:** the row stays as it is and nothing is
  granted. A charged replacement with no proof stays pending and protected;
  the failure is not hidden. This is why the flags stay off until D1 closes.
- **Replacement fails or goes unpaid before the commit point:** abandoned; the
  existing plan and entitlement are unchanged.
- **The one residual window:** after a successful commit, the replacement's
  first charge at the boundary can still fail. Then the old subscription's
  `cancelled` applies (row 5), the merchant ends at the old period end and
  re-checks out. Nothing is granted or fabricated. ReviewFlow cannot remove
  this window without provider evidence.
- **Old cancel fails after a switch:** owed and retried by the sweep. If the
  old subscription charges before it is cancelled (a duplicate charge), the
  sweep writes an audit event for staff; detection depends on G-4. No
  automatic refund.
- **Abandoned but then charged: deferred.** Re-checking an abandoned
  replacement for a paid invoice needs D1 (invoice retrieval) and a once-marker.
  It is out of scope for this spec's implementation until D1 is resolved and
  G-4 is evidenced. No stub and no dormant provider logic is added. The intended
  behavior, for that later change: an audit event for staff, no automatic switch
  and no automatic refund.

### Tenant and security requirements
- Every read and write runs in tenant context; `merchant_id` comes from the
  principal only. Endpoints are OWNER-only, session plus CSRF, `billing_write`
  throttle; API keys get 403.
- The lookup extension is SELECT-only and takes no write path. The two refs
  are validated (`^[A-Za-z0-9_]{1,64}$`) before storage or use in a URL.
- Razorpay `notes` carry no PII. The key secret and webhook secret are never
  returned. Logs carry exception class names and merchant ids only: no refs,
  payment ids, invoice ids, event ids or provider bodies.
- The classifier is one function; unclassified refusals can never create a
  second subscription.

## Admin
No admin changes. `Plan` stays the only registered billing model; the new
columns are not editable anywhere.

## Files to change
- `billing/models.py` — six columns, constraints, `for_lookup_ref` matches
  either ref
- `billing/services.py` — classifier, replacement start, commit, switch,
  abandon, cancel; extensions to `start_checkout`, `_apply_snapshot`,
  `sync_subscription`, `maintenance_due`, `advance_dunning`, the subscription
  response builder
- `billing/views.py`, `billing/serializers.py`, `billing/urls.py` — the new
  endpoint, the extended request and response
- `billing/exceptions.py` — four exceptions; `BillingProviderRejected` carries
  the bounded `status` and `reason`
- `billing/razorpay.py` — `create_subscription` gains `start_at` and
  `expire_by`; the empty `UPDATE_UNSUPPORTED_REFUSALS` constant; the 4xx path
  records the bounded status and reason
- `core/rls.py` — `columns` argument and the replace-policy helper
- `core/tenancy.py` — only if the lookup guard text must name both columns
- `config/settings.py`, `.env.example` — the four settings (names only; no
  secrets)
- `docs/08-billing/Billing-Specification.md` (§N "Plan change at the
  provider", "Checkout reconciliation", "Cancellation and expiry"),
  `docs/04-api/API-Specification.md`, `docs/03-database/Data-Dictionary.md`,
  `Database-Design.md`, `docs/02-architecture/Multi-Tenancy.md` (§Layer 2),
  `docs/09-security/Audit-Logging.md`, `docs/10-development/Development-Setup.md`
  (flags and window settings), `docs/10-development/Testing-Strategy.md` (only
  if a new scenario is listed) — kept in sync in the same PR
- Existing tests are changed only where one pins the old single-column lookup,
  the old column list, or `BillingProviderRejected` as "code only"
  (`test_razorpay_contract.py`); any such edit is listed in the PR description.
  The log-hygiene tests that plant provider codes must still pass.

## Files to create
- `billing/migrations/0003_subscription_replacement.py`
- `billing/migrations/0004_billing_ref_lookup_replacement.py`
- `billing/tests/replacement_helpers.py` (plain helpers; no test module imports
  another)
- `billing/tests/test_replacement_services.py`,
  `test_replacement_api.py`, `test_replacement_commit.py`,
  `test_replacement_switch.py`, `test_replacement_abandon.py`,
  `test_replacement_rls.py`, `test_replacement_webhook.py`,
  `test_replacement_races.py` (real threads), `test_replacement_flags.py`,
  `test_replacement_permissions.py`, `test_replacement_log_hygiene.py`

## New dependencies
No new dependencies.

## Rules for implementation
- Django + DRF monolith; business logic only in `services.py`, never in
  views/serializers
- Every tenant-owned model uses `core.TenantScopedManager`; RLS stays enabled
  on `billing_subscription` (FORCEd); no new table
- Never trust client-supplied `merchant_id`/`location_id`; derive them from the
  authenticated principal
- Celery tasks take `merchant_id` explicitly and set tenant context first; no
  new task is added
- Idempotency via database unique constraints, not check-then-insert
- Role checks via DRF permission classes (OWNER for every write)
- External IDs are UUIDs/hashids, never sequential integers
- Secrets/OAuth tokens encrypted (Fernet), never hardcoded or committed;
  Razorpay credentials from the environment only
- Status enums in `UPPER_SNAKE_CASE`; timestamps suffixed `_at`
- No V2 features, no bespoke admin app, no broker-side `eta`/`countdown`
  scheduling
- Every new service function has a unit test; every webhook path has a
  fixture-based contract test
- Razorpay is called only from `billing/services.py` through
  `billing/razorpay.py`; no provider base class or registry
- **Fail closed everywhere:** `fetch_invoices()` stays unimplemented and its
  `NotImplementedError` is caught nowhere; no path grants entitlement, advances
  a period, writes a `PaymentAttempt` or a `UsageRecord`, or writes an audit row
  around it
- **Do not claim any Razorpay behavior as verified.** Mocked responses verify
  ReviewFlow's handling only, never Razorpay's contract. No skip or xfail
  placeholders for the gates below.
- **No stub for deferred work.** The abandoned-replacement paid-invoice
  re-check is not implemented, stubbed or commented in. D1 stays out of scope:
  no pagination, `count`/`skip`, fallback or other provider behavior is inferred
  or implemented.
- `BillingProviderRejected`'s status and reason never reach `__str__`, logs,
  audit metadata or any API body
- The merged behavior is the baseline: with both flags off, every existing
  Phase 07 test passes unchanged and the API behaves byte for byte as before
- Do not cancel or modify a provider subscription except by the rules in
  "Cancellation and retirement"; never un-cancel one
- Conventional Commits; one feature branch; no commit to `main`

## Release gates
Both flags default to **OFF**, so the spec can be built and merged with
nothing enabled; the flags are independent. **Nothing in this table is
verified.** Each is a pre-production gate against an activated Razorpay
account and blocks turning the named flag on in production. The existing
gates (D1, T1, T4, T7, OR1-OR3, R1-R4/V1-V4) stay open and are not closed by
this spec.

| Gate | Evidence needed | Upgrade flag | Downgrade flag |
|---|---|---|---|
| G-0 | The exact refusal (status and error) for an update on UPI, eMandate and domestic card; it defines the classifier, and the known-refusal set stays empty until it is recorded | needed | needed |
| G-1 | Immediate-start create: authorization amount, whether it is refunded, and what an immediate cancel of the old subscription does | needed | not needed |
| G-2 | Future-start create: `start_at`, the authorization charge and its refund, the entity fields read for verification (start, expiry, plan id), statuses, webhook sequence; until recorded no downgrade replacement is created | not needed | needed |
| G-3 | `expire_by` limits, which fix the two window settings and the commit cutoff (no hardcoded margin) | upgrade window | downgrade margin |
| G-4 | Duplicate-charge detection for the retired subscription, and the deferred abandoned-replacement paid-invoice re-check | needed | needed |
| G-5 | Two concurrent subscriptions for one merchant, and mandate limits such as the UPI cap | needed | needed |
| G-6 | Cancelling a `created` or `authenticated` replacement; the response to a repeated cycle-end cancel; whether a scheduled cancel can be revoked | needed | needed |
| D1, T7 | Invoice completeness and the invoice window versus the period; required before any replacement grants entitlement | needed | needed |

This spec claims no production readiness.

## Definition of done
All verifiable with `pytest` on real PostgreSQL unless noted. Razorpay is
mocked at `billing/razorpay.py` (`FakeProvider`).

**Models, migrations, RLS**
- [ ] `python manage.py migrate` applies `billing` 0003 and 0004, and
      `migrate billing 0002` reverses them (data in the six new columns is
      discarded; the test runs with them empty); `makemigrations --check
      --dry-run` is clean
- [ ] Each new constraint has a test that violates it and one that satisfies it
      (partial uniqueness of both refs, `replacement_ref_differs`,
      `replacement_expiry_set`, `replacement_committed_needs_ref`,
      `retired_kind_set`, `replacement_confirmed_needs_committed`)
- [ ] `billing_subscription` still has exactly two policies; inside
      `billing_ref_lookup_atomic(ref)` a raw `SELECT` returns the row whose
      `payment_provider_ref` OR `replacement_provider_ref` equals the ref and
      nothing else; a raw `UPDATE`/`DELETE` affects 0 rows; with no lookup
      context it returns 0 rows; the other tables are unaffected
- [ ] A raw `INSERT`/`UPDATE` naming another merchant on the new columns fails
      `WITH CHECK` (tenant isolation; the existing write-isolation tests stay
      green)

**Flags and fallback**
- [ ] With both flags off, every existing billing test passes unchanged and a
      refused update still returns `409 plan_change_unsupported`
- [ ] A successful provider update never creates a replacement and is
      unchanged (existing tests)
- [ ] A classified refusal creates a replacement only with that kind's flag on
      and its window setting set; unset gives `503 billing_not_configured`
- [ ] Every unclassified 4xx, any 5xx and any malformed refusal returns the
      existing error and never creates a replacement (classifier tests); with
      the shipped empty known-refusal set nothing classifies even with a flag
      on, and the existing refusal codes in the current tests still give 409
- [ ] The two flags work independently (upgrade on with downgrade off, and the
      reverse); turning a flag off mid-flight strands no pending replacement

**Acknowledgement and checkout**
- [ ] A replacement upgrade without `acknowledge_no_credit` returns `422
      no_credit_acknowledgement_required` and changes nothing; with it, `201`
      with `checkout`; a downgrade never needs it; a sent acknowledgement is
      ignored and unrecorded when the provider update succeeds
- [ ] A repeat of the same plan returns the existing checkout (`200`) with no
      provider call; any other plan, including the current plan, returns `409
      replacement_in_progress`; a new replacement is refused while
      `retired_provider_ref` is set
- [ ] A downgrade replacement is not created (`409 plan_change_unsupported`,
      nothing stored, no provider create call) when the verification field is
      not recorded (G-2) or the commit cutoff has passed
- [ ] The created replacement is validated (`^[A-Za-z0-9_]{1,64}$`) before it
      is stored; an invalid or malformed create response is `502` with nothing
      stored

**Switch, quota, payment**
- [ ] A replacement proven paid (`current_period_paid()`) switches the row:
      plan, period, refs, cleared columns, one fresh `UsageRecord` at 0, one
      ledger entry, one `billing.plan_changed` row; a second run changes
      nothing
- [ ] Provider `active` without a qualifying invoice grants nothing and
      switches nothing
- [ ] **D1:** with the real `fetch_invoices()`, no replacement switches on any
      path; nothing is granted, advanced or written around it
- [ ] A replacement upgrade starts a fresh usage period; a provider-update
      upgrade keeps consumed usage when the period is unchanged
- [ ] The old plan's entitlement and quota are untouched until the switch;
      after the old period end a row without proof is not entitled

**PAST_DUE**
- [ ] A replacement proven paid recovers a `PAST_DUE` row to `ACTIVE` on the
      new plan, clearing `past_due_at` and `dunning_stage`; without proof
      nothing switches
- [ ] A `PAST_DUE` transition abandons a non-`active`, uncommitted replacement
      and preserves the existing subscription; an `active` one is never
      cancelled

**Downgrade commit point**
- [ ] T1 commits the intent before the provider call; the provider cancel call
      runs with no transaction open and no row lock held; T2 records the
      confirmation; the intent survives a simulated crash between them
- [ ] The old cancel is never scheduled at creation or before the replacement
      is `authenticated` and verified
- [ ] Each failed or unreadable verification check (status, plan id, start,
      expiry, cutoff) means no commit and an abandon, with the plan unchanged
- [ ] A failing old-cancel call abandons the replacement, clears the intent and
      leaves the old subscription untouched
- [ ] An unknown outcome is treated as committed and the sweep re-issues the
      cancel
- [ ] After commit, `POST /billing/replacement/cancel` returns `409
      replacement_committed`, the old cancelled status follows rule 5 if the
      replacement fails, and nothing un-cancels the old subscription

**Cancel, retire, abandon**
- [ ] `POST /billing/replacement/cancel` abandons an uncommitted, non-`active`
      replacement (`200`, idempotent) and returns `409 replacement_activating`
      for an `active` one
- [ ] A merchant cancel with a non-`active` replacement cancels it first (`502`
      and nothing changed on failure); with an `active` one it is `409
      replacement_activating`; with a committed downgrade replacement it cancels
      the replacement too, then runs the normal cancel
- [ ] An uncommitted, non-`active` replacement is abandoned when the row ends
      (`CANCELLED`, `EXPIRED`) as well as at the `PAST_DUE` transition
- [ ] After a switch the old subscription is cancelled and tracked in the
      retired slot (`SWITCHED_OLD`) until Razorpay reports it terminal; an
      abandoned replacement goes to the retired slot
      (`ABANDONED_REPLACEMENT`); the retired-cancel rule is kind-aware (an
      `active` old subscription is cancelled, an `active` abandoned replacement
      never is, `created` and `authenticated` abandoned replacements are)
- [ ] A replacement the provider reports `active` and proven paid on a
      `CANCELLED` or `EXPIRED` row does not switch, writes an audit event for
      staff, refunds nothing, and a checkout on that row returns `409
      replacement_activating`

**Webhook and sweep**
- [ ] A signed event naming the replacement ref reaches the row through the
      extended lookup and triggers a sync; an unsigned or invalid one is `401`
      with no read; an event naming the retired ref is acknowledged and ignored
- [ ] `maintenance_due()` is true for a row with a replacement or retired ref;
      the sweep processes both refs with no `eta`, `countdown` or retry

**Concurrency (real threads, bounded lock-wait polling, no sleeps)**
- [ ] Switch versus the sweep, switch versus a merchant cancel, switch versus a
      `PAST_DUE` transition, the commit versus a merchant cancel, and a double
      checkout each end in one consistent state (one switch, one commit, one
      replacement)

**Permissions, security, hygiene**
- [ ] OWNER only on the writes; ADMIN, MANAGER and VIEWER get 403; an API key
      gets 403; CSRF is enforced; the `billing_write` throttle applies
- [ ] Merchant A cannot see or affect merchant B's replacement (tenant
      isolation), over both session requests and webhook lookups
- [ ] `GET /billing/subscription` shows the `replacement` object and never a
      ref; it makes no provider call
- [ ] No log line or audit metadata contains a ref, payment id, invoice id,
      event id, provider body or secret

**Whole-suite**
- [ ] The full `pytest` suite passes with nothing skipped or xfailed, all
      pre-existing tests unchanged except those pinning the old lookup column
      (listed in the PR), `manage.py check` and `git diff --check` are clean,
      and `/test-feature` and `/code-review-feature` both pass
- [ ] The docs listed under "Files to change" are updated in the same PR, and
      the gates under "Release gates" are recorded as open, not claimed

## Out of scope
- Any ReviewFlow-side proration, credit or refund; staff refunds (Phase 16)
- Un-cancelling a scheduled provider cancel; any grace period after the old
  period end
- A payment-method detector or a pre-check of the method before the update
  attempt
- A second payment provider or a provider abstraction
- Changing the merged provider-update behavior for payment methods Razorpay can
  update (international cards)
- Implementing `fetch_invoices()` (D1) or any other gate in "Release gates"
- The abandoned-replacement paid-invoice re-check (audit event for a paid
  abandoned replacement): deferred until D1 is resolved and G-4 is evidenced;
  no stub and no dormant provider logic in this change
- Billing usage charts (Phase 14), cross-tenant billing admin (Phase 16)
- Monitoring and the Render readiness items (OR1-OR3; the follow-up
  `07-billing-maintenance-monitoring`)

## W0 amendment log
Amendment of 2026-10-05 (uncommitted, for review). Each change and the approval it
applies:
- Six columns, not four: `retired_kind` and `replacement_cancel_confirmed_at`, with
  two more constraints (planning review, "Add both columns").
- `BillingProviderRejected` carries a bounded status and reason; the known-refusal set
  ships empty (planning review, "Carry bounded fields"; rule row 15).
- Kind-aware retired-cancel rule that includes `authenticated` for an abandoned
  replacement and never `active` (planning review, "Include authenticated").
- A paid `active` replacement on a `CANCELLED`/`EXPIRED` row: audit, no switch,
  `409 replacement_activating` (planning review; rule row 16).
- P1: a merchant cancel after a downgrade commit also cancels the replacement.
- P2: `replacement.authorized` is derived as `committed`.
- P3: a downgrade past the commit cutoff falls through to `409
  plan_change_unsupported`.
- P4: any plan other than the pending target, including the current plan, gets `409
  replacement_in_progress` while one is pending.
- P5: an uncommitted non-`active` replacement is also abandoned when the row ends (row 17).
- P6: the abandoned-replacement paid-invoice re-check is deferred until D1 and G-4,
  documented only (no stub, no dormant logic).
- P7: a downgrade replacement is not created until the G-2 verification field is
  recorded.
- P8: the migration reversal wording (new-column data is discarded).
- The downgrade commit is described as T1 DB transaction, provider cancel call outside
  any transaction and without the row lock, T2 DB transaction (row 18).
- The two window settings are durations in seconds (approved 2026-10-05, at the W2
  review; the spec had named no unit). The setting names are unchanged.
- Rule 5 (row 5) is defined precisely (approved 2026-10-05, at the W3 review): the
  terminal guard applies to an `ACTIVE` row with `replacement_provider_ref` and
  `replacement_committed_at` set. `replacement_committed_at` is exclusive to the
  downgrade commit flow, so it identifies a committed downgrade replacement exactly;
  `pending_plan` does not, because a provider-update downgrade sets it too. No
  `replacement_kind` column is added.
- A seventh column, `replacement_plan` (FK to `Plan`), records the pending replacement's
  target plan; none of the six stored it for an upgrade (approved 2026-10-06, W5).
- A downgrade replacement's `expire_by` (and `replacement_expires_at`) is the commit
  cutoff minus exactly 1 second, so it is strictly before the cutoff (approved
  2026-10-06, W5). The configured margin and its meaning are unchanged.
- `razorpay.DOWNGRADE_START_FIELD` is `None` until G-2 evidence is recorded; W5 only
  checks it for `None` (no downgrade replacement is created while it is), and it makes
  no provider call (P7).
- RESOLVED (approved 2026-10-06, Option A): the W6 commit verification no longer asks
  that the replacement's expiry be "not before the boundary", which could never hold for
  an `expire_by` strictly before the cutoff. It asks that the entity's expiry equals
  `replacement_expires_at`, the `expire_by` ReviewFlow set (commit cutoff minus 1 second),
  so only local state is compared and no provider behavior is assumed. The G-2 gate, the
  start-equals-old-period-end check and "now is before the commit cutoff" are unchanged.
- D1 stays fully out of scope: `fetch_invoices()` stays fail-closed and no provider
  behavior is inferred.

## Open Decisions
None. All decisions (C1, C2, C3, Q1-Q6 and the rule changes in "Rules this
follow-up changes") were approved in review on 2026-10-05. The remaining
unknowns are provider facts, recorded as gates G-0 to G-6, D1 and T7.
