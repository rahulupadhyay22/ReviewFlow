# Campaign Engine

## Models

- **`ReviewCampaign`** — per-location configuration: `mode` (`DIRECT_GOOGLE` | `FEEDBACK_FIRST`), `delay_minutes`, `frequency_cap_days`, `message_template_id`, `is_active`.
- **`CampaignExecution`** — owns the ReviewFlow **business-workflow** lifecycle for one attempt to send a review request for one transaction under one campaign. `UNIQUE(campaign_id, transaction_id)`. Does **not** track WhatsApp delivery detail — see "Responsibility Split" below.

## Responsibility Split — `CampaignExecution` vs. `WhatsAppMessage`

These two models own two distinct lifecycles and must not duplicate each other's semantics:

- **`CampaignExecution` owns the business workflow**: was a review request scheduled, sent, cancelled, blocked by quota, or expired? This is what billing, the eligibility engine, and campaign-level analytics read.
- **`WhatsAppMessage` owns WhatsApp delivery**: was the message queued, sent to Meta, delivered to the handset, read, or did it fail? This is what delivery-health monitoring and support tooling read.

```
Transaction
   ↓
CampaignExecution   (business workflow: SCHEDULED, SENDING, SENT, FAILED, CANCELLED,
                      QUOTA_EXCEEDED, EXPIRED)
   ↓
WhatsAppMessage     (delivery lifecycle: QUEUED, SENT, DELIVERED, READ, FAILED)
   ↓
Meta WhatsApp API
```

**Example** — these two statuses evolve independently once the message is sent:

```
CampaignExecution = SENT      WhatsAppMessage = DELIVERED
   ... later ...
CampaignExecution = SENT      WhatsAppMessage = READ     ← CampaignExecution does NOT
                                                             become "READ"; that state
                                                             belongs only to WhatsAppMessage
```

`CampaignExecution` reaching `SENDING` means quota has been reserved; reaching `SENT` is the one-way business-workflow fact ("the review request was dispatched"); it is never reopened or advanced because the underlying `WhatsAppMessage` later transitions to `DELIVERED` or `READ`. Analytics that need delivery/read rates query `WhatsAppMessage` directly, joined through `execution_id` — they do not look for that detail on `CampaignExecution`.

## Eligibility Engine

Full rule set lives in `../01-product/Business-Rules.md` §2 — summarized here as the implementation checklist, checked in order, short-circuiting on the first failure:

1. `Customer.phone` present and valid E.164
2. `Customer.opted_out == False`
3. `Transaction.status == COMPLETED`
4. No existing active/sent `CampaignExecution` for this transaction — checked transactionally, under a row lock (see "Concurrency & Locking" below), across *all* campaigns, not just this one
5. No other review-request execution for this customer across any campaign/location under the merchant within the effective frequency-cap window
6. `ReviewCampaign.is_active` and `Location.is_active`
7. WhatsApp sender configured and template `APPROVED`
8. Merchant subscription is `ACTIVE` and quota can be atomically reserved; if quota is unavailable, create/retain `QUOTA_EXCEEDED` per `../08-billing/Billing-Specification.md`

This logic lives in a `services.py` function (e.g. `campaigns.services.check_eligibility(transaction)`), never inline in a view or Celery task — it must be independently unit-testable with fixture transactions per rule.

## Concurrency & Locking

The `UNIQUE(campaign_id, transaction_id)` constraint alone does **not** prevent two different campaigns from racing to create an execution for the same transaction (e.g. `Campaign A + Transaction X` and `Campaign B + Transaction X` both evaluating eligibility at the same moment). The database constraint only prevents the *same* campaign from double-firing on the *same* transaction.

**Required transactional pattern**, wrapping eligibility rule 4 and execution creation together:

```
BEGIN
  SELECT * FROM transaction WHERE id = :transaction_id FOR UPDATE
  → check: does an active/sent CampaignExecution already exist for this transaction,
           under ANY campaign? ("active/sent" = SCHEDULED, SENDING, or SENT — not
           CANCELLED, FAILED, QUOTA_EXCEEDED, or EXPIRED, which are not blocking)
  → if yes: do not create another execution; return the existing one (no-op, no error)
  → if no: create CampaignExecution (status = SCHEDULED)
COMMIT
```

The `(campaign_id, transaction_id)` unique constraint remains in place underneath this as an additional database-level safety net — the lock is the primary defense, the constraint is the backstop if the lock is ever bypassed by a bug.

**Required concurrency tests** (see `../10-development/Testing-Strategy.md` for full detail):
- Same campaign + same transaction, attempted twice → one execution (constraint-level).
- Different campaigns + same transaction, attempted concurrently → one execution (lock-level — this is the case the constraint alone does not cover).
- Two concurrent workers attempting the same transaction at the same instant → one execution, the other's attempt observes the lock and no-ops.

## Quota Reservation and Send Recovery

Quota reservation is a durable business fact. The database transaction commits `UsageRecord.requests_used += 1` and `CampaignExecution = SENDING` before the external WhatsApp call. The provider call is never held inside the database transaction.

Before calling the provider, the worker creates the 1:1 `WhatsAppMessage` in `QUEUED` state using `execution_id` as its internal idempotency key. A worker crash after quota reservation but before provider response leaves the execution/message recoverable. A reconciliation task finds `SENDING` executions whose message remains unresolved and retries or resolves them according to provider status; it never creates a second `CampaignExecution` or consumes another quota unit.

Provider delivery is treated as an external side effect and therefore is not claimed to be mathematically exactly-once. The implementation must use the provider's supported idempotency/retry mechanism where available and persist provider message IDs/status callbacks. If provider uncertainty remains after a timeout, reconciliation must prefer provider-status lookup before issuing another send.

## Scheduling — Dispatch Loop (locked design)

```
Celery Beat: dispatch_due_executions   (every 1 minute)
  → SELECT * FROM campaign_execution
      WHERE status = 'SCHEDULED' AND scheduled_at <= NOW()
      FOR UPDATE SKIP LOCKED
  → for each locked row:
      re-check transaction status, opt-out, merchant-wide frequency cap, campaign/location status, sender/template, and subscription
      BEGIN atomic quota reservation transaction
          lock current UsageRecord for the merchant/billing period
          if no quota: set execution = QUOTA_EXCEEDED + timestamps; COMMIT; continue
          else: increment requests_used by 1; set execution = SENDING; COMMIT
      create/reuse the 1:1 WhatsAppMessage in QUEUED state
      WhatsAppService.send_template(...)
          on success: CampaignExecution = SENT
          on provider error: CampaignExecution = FAILED (usage remains 1)
      if refund/cancellation is detected before quota reservation: CampaignExecution = CANCELLED
      if quota reservation fails: CampaignExecution = QUOTA_EXCEEDED, quota_exceeded_at = now(),
                                  expires_at = now() + 7 days
```

**Why poll-based, not `apply_async(eta=...)`**: cancelling or rescheduling an execution (e.g. on a refund arriving before the delay window elapses) is a simple status flip on this model, not a fragile broker-side task revocation. The query is cheap given the `(scheduled_at, status)` index, even at thousands of rows.

## Campaign Activation Rules

A campaign cannot be set `is_active = True` until, checked at the service layer (not just the UI):
- A Google location is connected, or explicitly marked skipped by the merchant.
- A WhatsApp sender (`WhatsAppLocationMapping`) is configured for the location.
- At least one `MessageTemplate` with `status = APPROVED` exists for the merchant.

## Subscription State Gate

Only `Subscription.status = ACTIVE` executions may enter `SENDING`. `PAST_DUE` immediately blocks new sends; `CANCELLED` and `EXPIRED` block sending entirely. See `../08-billing/Billing-Specification.md` for the 7-day grace period and recovery rules.

## Feedback-First Mode (`FEEDBACK_FIRST`)

```
Sale -> WhatsApp -> our feedback page (rating + comment + categories)
     -> Google review CTA shown to the customer regardless of the rating given
```

The Google invitation is never conditionally hidden based on the feedback rating — enforced in the feedback-page service logic (see `../01-product/Business-Rules.md` §1), not left to frontend discretion.

## Status Machine — `CampaignExecution` (business workflow only)

```
SCHEDULED → SENDING → SENT
SCHEDULED → SENDING → FAILED        (provider-reported failure at send time)
SCHEDULED → CANCELLED               (refund, opt-out arriving before dispatch)
SCHEDULED → QUOTA_EXCEEDED → EXPIRED   (quota exhausted at dispatch time; held for 7 days,
                                          then expires if quota never becomes available —
                                          see Billing-Specification.md)
QUOTA_EXCEEDED → SCHEDULED           (automatic resume within the 7-day window once quota
                                       becomes available, re-checking all eligibility rules —
                                       see Billing-Specification.md)
```

`DELIVERED` and `READ` are **not** `CampaignExecution` statuses — they exist only on `WhatsAppMessage`. See "Responsibility Split" above.

Independently tracked, not part of the main status machine: `google_click_at`, `feedback_submitted_at`.
