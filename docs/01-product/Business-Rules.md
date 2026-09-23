# Business Rules

## 1. Review Integrity (non-negotiable)

- The platform never creates, submits, or edits a Google review. The customer always writes and submits it themselves, on Google's own interface.
- No fake reviews, no fake customer identities, no writing on a customer's behalf.
- Every eligible customer is invited **neutrally** — the platform never routes only high-rated customers to Google, never hides or discourages negative reviews, and never asks for specific positive wording.
- In feedback-first campaigns, the Google CTA is shown regardless of the rating the customer gave in the feedback step.

## 2. Eligibility Rules (checked in order, short-circuiting)

1. The transaction has a `Customer` (a sale recorded without a customer phone has none and is never eligible), and `Customer.phone` is valid E.164
2. `Customer.opted_out == False`
3. `Transaction.status == COMPLETED`
4. No existing active/sent `CampaignExecution` for this transaction under **any** campaign (V1 rule: one transaction receives only one review request, period). This is checked **transactionally**, under a row lock on the `Transaction`, not just as a pre-check — two campaigns (or two concurrent workers) racing to create an execution for the same transaction at once must still result in exactly one execution. See `../06-automation/Campaign-Engine.md` §"Concurrency & Locking" for the locking pattern; the `(campaign_id, transaction_id)` unique constraint remains as a database-level backstop underneath the lock.
5. No other review-request execution for this customer, across **any campaign or location under the merchant**, within the merchant-wide frequency-cap window. The effective window is the requesting campaign's `frequency_cap_days`. The check is performed against the merchant's execution history, not only the current campaign.
6. `ReviewCampaign.is_active` and `Location.is_active`
7. WhatsApp sender configured and template approved
8. Merchant's monthly quota not exhausted (exhausted requests are held as `QUOTA_EXCEEDED`, never silently dropped)

## 3. Opt-Out

- A customer replying with an opt-out keyword (e.g. "STOP") is marked `opted_out = True` and permanently excluded from future campaign executions for that merchant.
- Opt-out can also be set manually from the dashboard.
- Opting out never deletes existing message history.

## 4. Refunds

- A refunded transaction (`status = REFUNDED`) that arrives **before** the scheduled send time blocks that execution from being dispatched.
- A refund that arrives **after** the message has already been sent does not retract or follow up on the sent message.

## 5. Frequency Capping

- No customer receives more than one review request within `ReviewCampaign.frequency_cap_days`, regardless of how many eligible transactions they have in that window.

## 6. Quota & Billing (finalized)

Full detail and rationale in `../08-billing/Billing-Specification.md`; the binding rules:

- **What counts as usage**: one `CampaignExecution` becomes one billable review-request usage unit when it atomically enters `SENDING` and successfully reserves one unit of the merchant's quota. `SCHEDULED` and `QUOTA_EXCEEDED` executions consume zero quota. A Google review, Google CTA click, WhatsApp delivery/read event, retry attempt, duplicate webhook, or duplicate transaction never creates additional usage.
- **Quota exhausted**: an otherwise-eligible transaction with no quota available results in `CampaignExecution.status = QUOTA_EXCEEDED`. The WhatsApp message is **not** sent.
- **QUOTA_EXCEEDED retention**: held for **7 days** (`quota_exceeded_at` + `expires_at` fields). If `expires_at` passes with quota still unavailable, the execution transitions to `EXPIRED` and must never be sent.
- **Automatic resume**: when the merchant upgrades their plan, or a new billing period begins with available quota, eligible `QUOTA_EXCEEDED` executions still within their 7-day window automatically resume — but only after **re-checking every eligibility rule in §2 from scratch** (transaction status, opt-out, phone validity, campaign/location active status, frequency cap, quota, WhatsApp configuration/template). Resumed executions go back through the normal scheduling/dispatch pipeline (§ Campaign-Engine.md) — they are never sent as an immediate burst.
- **Retries do not consume additional quota**: one `CampaignExecution` = one usage unit regardless of how many WhatsApp send attempts it takes (e.g. failed → retried → sent still counts as 1).
- **Refunds**: a refund arriving while an execution is `SCHEDULED` or `QUOTA_EXCEEDED` cancels it with zero usage. A refund arriving after quota has been reserved (`SENDING`, `SENT`, or `FAILED` after a send attempt) does not reverse the consumed usage. A scheduled dispatch always re-checks transaction status immediately before quota reservation and send.
- **Duplicate events**: a duplicate webhook/event/transaction (caught by the idempotency constraints in `../04-api/Webhook-Specification.md`) never creates additional usage.
- **Upgrades**: take effect immediately — new quota is available right away.
- **Downgrades**: take effect at the next billing period — never retroactive, and previous usage is never invalidated.
- **Unused quota**: does not roll over between billing periods.
- **Subscription states**: `ACTIVE`, `PAST_DUE`, `CANCELLED`, `EXPIRED`. `PAST_DUE` has a 7-calendar-day grace period; new campaign executions are not dispatched while `PAST_DUE`, but existing `SCHEDULED`/`QUOTA_EXCEEDED` records are retained. If payment is not recovered by the end of the grace period, the subscription becomes `EXPIRED` and sending remains disabled until the merchant resubscribes. Dunning attempts occur on day 0, day 3, and day 6. See Billing-Specification.md.

## 7. Multi-Location Rules

- A merchant with one location is the N=1 case of multi-location — there is no separate single-location mode.
- Location-level campaign/WhatsApp/Google settings override merchant-level defaults where both exist.

## 8. Reporting Honesty

- **Review Requests**, **Google CTA Clicks**, and **Google Reviews** are always shown as three independent counts.
- The platform never implies or scores a causal link between a specific click and a specific review — Google provides no reliable linkage, and no heuristic is used to manufacture one (see `06-automation/Google-Reviews.md`).
- The same rule applies to QR scans: a **QR scan is never claimed to equal a Google review**. Total scans and scans-by-location are reported as their own independent metric, alongside — never merged into — Google CTA Clicks or Google Reviews. See `../06-automation/Google-Reviews.md` and the QR tracking design referenced from `Feature-Scope.md`.

## 9. WhatsApp Compliance

- Only Meta-approved message templates are used; no free-form marketing copy.
- Shared-pool messages must clearly identify the business by name in the message body.
- A campaign cannot be activated until: a Google location is connected (or explicitly skipped), a WhatsApp sender is configured, and at least one approved template exists.
