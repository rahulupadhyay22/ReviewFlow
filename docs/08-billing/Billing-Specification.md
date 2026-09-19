# Billing Specification

This document is the binding V1 billing and quota policy. Payment-provider implementation details may be added later, but the state and usage rules below are fixed for V1.

## Architecture

```
Plan          — tier definition: name, monthly_price, quota_requests, features_json
Subscription  — merchant plan + billing-period/payment state
UsageRecord   — quota reservation/usage for one billing period
```

## A. What Counts as Usage

A review request consumes **exactly one usage unit when its `CampaignExecution` atomically enters `SENDING` and reserves one unit of available quota**. This reservation happens before the WhatsApp provider call. This prevents concurrent workers from oversubscribing quota.

Usage is never consumed by:
- `SCHEDULED` executions
- `QUOTA_EXCEEDED` executions
- `CANCELLED` or `EXPIRED` executions
- Google reviews or Google CTA clicks
- WhatsApp delivery/read events
- retry attempts on an already-reserved execution
- duplicate webhooks/events/transactions

If the provider send subsequently fails, the reserved usage remains consumed: the execution represents one attempted review request.

## B. Atomic Quota Reservation

The transition to `SENDING` and the quota increment must occur in the same database transaction:

```
BEGIN
  lock current UsageRecord for merchant + billing period
  if requests_used >= quota_requests:
      CampaignExecution = QUOTA_EXCEEDED
      quota_exceeded_at = now()
      expires_at = now() + 7 days
      COMMIT
  else:
      requests_used += 1
      CampaignExecution = SENDING
      COMMIT
  call WhatsApp provider outside the DB transaction
```

A retry of an execution that already reached `SENDING` never increments usage again. If a worker crashes after reservation and before the provider response, recovery operates on the existing execution/message state; it does not create a second usage unit.

## C. Quota Exhausted — 7-Day Retention

When no quota is available at dispatch/resume time:

```
CampaignExecution = QUOTA_EXCEEDED
quota_exceeded_at = now()
expires_at = quota_exceeded_at + 7 days
```

A scheduled task transitions expired `QUOTA_EXCEEDED` executions to `EXPIRED`. An `EXPIRED` execution must never be sent or resumed.

## D. Automatic Resume

Triggers:
- plan upgrade that makes quota available; or
- new billing period with available quota.

Each candidate is re-checked from scratch for phone validity, opt-out, transaction status, one-request-per-transaction rule, merchant-wide frequency cap, campaign/location status, WhatsApp configuration/template, subscription state, and quota.

Resume is gradual: the execution returns to `SCHEDULED` with a normal scheduling time and is processed by the normal dispatch loop. There is no burst send. Quota is reserved only when the execution enters `SENDING`.

If a refund arrives while the execution is `QUOTA_EXCEEDED`, it is cancelled with zero usage.

## E. Plan Upgrade

Effective immediately. The new quota entitlement is available immediately, including for eligible `QUOTA_EXCEEDED` executions still inside their 7-day window.

## F. Plan Downgrade

Effective at the next billing period. It is never retroactive and does not invalidate usage already consumed in the current period.

## G. Unused Quota

No rollover. Each new billing period starts a new `UsageRecord` with `requests_used = 0`.

## H. Retries

Retries never consume additional quota. One execution can have multiple provider attempts but still represents one usage unit because the quota was reserved before its first send attempt.

## I. Refunds

| Refund timing | Execution behavior | Usage |
|---|---|---|
| `SCHEDULED` | `CANCELLED` | 0 |
| `QUOTA_EXCEEDED` | `CANCELLED` | 0 |
| `SENDING` / provider call in progress | do not attempt a second send; preserve already-reserved usage | 1 |
| `SENT` / `FAILED` after provider attempt | no usage reversal | 1 |

The dispatch loop re-checks `Transaction.status` immediately before quota reservation, so a refund received before that point prevents quota consumption.

## J. Duplicate Events

Duplicate inbound events never create additional usage. `IntegrationEvent` is idempotent per `(integration_id, external_event_id)`, and campaign execution creation is protected by the transaction row lock plus `(campaign_id, transaction_id)` unique constraint.

## K. Payment Gateway and Subscription Failure Policy — V1 Locked

- **Payment gateway:** Razorpay.
- **Grace period:** 7 calendar days after a failed renewal/payment transition to `PAST_DUE`.
- **Sending during `PAST_DUE`:** disabled immediately. No new execution may enter `SENDING`; existing `SCHEDULED` executions remain stored and are re-checked after payment recovery.
- **Dunning:** payment retry/reminder attempts on day 0, day 3, and day 6; each attempt is idempotent and recorded against the subscription/payment record.
- **Recovery:** successful payment returns the subscription to `ACTIVE` and restores normal quota/sending behavior.
- **Grace-period expiry:** if payment is not recovered by the end of day 7, transition to `EXPIRED`; sending remains disabled until a new/renewed subscription becomes `ACTIVE`.
- **Downgrade after failed payment:** no automatic downgrade during the grace period; the subscription remains on its current plan until recovery or expiry.

These are product defaults for V1 implementation; legal/tax/invoice requirements are implementation/compliance work, not undefined architecture.

## L. Billing Period and Usage Integrity

`UsageRecord` is unique for `(merchant_id, period_start, period_end)`. The quota reservation and `requests_used` increment are atomic. A successful reservation is the single source of truth for billable request usage.

## Related Documents

- `../01-product/Business-Rules.md`
- `../03-database/Data-Dictionary.md`
- `../03-database/Database-Design.md`
- `../06-automation/Campaign-Engine.md`
- `../04-api/Webhook-Specification.md`
