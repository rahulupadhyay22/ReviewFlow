# Billing Specification

This document is the binding V1 billing and quota policy. Payment-provider implementation details may be added later, but the state and usage rules below are fixed for V1.

## Architecture

```
Plan          — tier definition: name, monthly_price, quota_requests, features_json
Subscription  — merchant plan + billing-period/payment state (the single source of truth for a merchant's plan)
UsageRecord   — quota reservation/usage for one billing period
PaymentAttempt — one row per real Razorpay payment
BillingEvent  — payment-webhook inbox and dedup row (identifiers only, no payload)
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

How a plan change is carried out at Razorpay, and the payment methods for which V1 cannot yet do it, are in §N "Plan change at the provider".

## F. Plan Downgrade

Effective at the next billing period. It is never retroactive and does not invalidate usage already consumed in the current period.

See §N "Plan change at the provider" for the same provider limitation.

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
- **Dunning:** recovery checkpoints on day 0, day 3, and day 6 of the grace period. A checkpoint is a ReviewFlow step, not a payment retry and not a reminder: Razorpay alone retries the payment and notifies the payer, and ReviewFlow does neither. Each checkpoint is idempotent and recorded on the subscription (`Subscription.dunning_stage`) with an audit event. Detail in §N "Dunning checkpoints". *(Amended 2026-09-30; previously "payment retry/reminder attempts".)*
- **Recovery:** a successful payment returns the subscription to `ACTIVE` and restores normal quota/sending behavior. The payment must be proven by a paid invoice for the current billing cycle; Razorpay reporting the subscription `active` is not enough (§N "Paid-entitlement rule").
- **Grace-period expiry:** if payment is not recovered by the end of day 7, transition to `EXPIRED`; sending remains disabled until a new/renewed subscription becomes `ACTIVE`.
- **Downgrade after failed payment:** no automatic downgrade during the grace period; the subscription remains on its current plan until recovery or expiry.

These are product defaults for V1 implementation; legal/tax/invoice requirements are implementation/compliance work, not undefined architecture.

## L. Billing Period and Usage Integrity

`UsageRecord` is unique for `(merchant_id, period_start, period_end)`. `period_start` and `period_end` are datetimes and hold exactly the provider's billing-cycle instants, the same values as `Subscription.current_period_start`/`current_period_end` for that cycle. The quota reservation and `requests_used` increment are atomic. A successful reservation is the single source of truth for billable request usage.

The quota is not copied onto the `UsageRecord`. It is read from `Subscription.plan.quota_requests` inside the reservation lock, which is what makes an upgrade effective immediately.

## M. Razorpay Provider Facts (verified 2026-09-30)

This section records what Razorpay's public documentation says. It is reference material for the Phase 07 spec (`.claude/specs/07-billing-quota-foundation.md`, "Razorpay verification gate"). It changes no policy in §A–§L.

Every answer below was read from the cited page on **2026-09-30**. "Quoted" means the page states it. "Not documented" means the page is silent, which is not proof that something is impossible; those items are listed under "Needs test-mode confirmation".

| # | Question | Answer | Source |
|---|---|---|---|
| V1 | Subscription endpoints | Create `POST /v1/subscriptions`; fetch `GET /v1/subscriptions/{id}`; update `PATCH /v1/subscriptions/{id}`; cancel `POST /v1/subscriptions/{id}/cancel`; pending update `GET /v1/subscriptions/{id}/retrieve_scheduled_changes` and `POST /v1/subscriptions/{id}/cancel_scheduled_changes`; invoices `GET /v1/invoices?subscription_id={id}`. HTTP Basic auth with key id and key secret. | https://razorpay.com/docs/api/payments/subscriptions/ and the per-endpoint pages under it |
| V1 | Open-ended monthly subscription | Not available. `plan_id` is required, and `total_count` (minimum 1) or `end_at` is required. Quoted: "We support Subscriptions for a maximum duration of 100 years. The number of billing cycles depends if the subscription is billed daily, weekly, monthly or yearly." A monthly plan can therefore run for at most 1200 cycles. When the count is used up the subscription becomes `completed`. | https://razorpay.com/docs/api/payments/subscriptions/create-subscription/ ; https://razorpay.com/docs/payments/subscriptions/faqs/ |
| V1 | Other create fields | `customer_notify` (boolean, default `true`: Razorpay handles customer communication), `expire_by` (deadline for the authorization payment), `start_at`, `notes` (max 15 pairs). The response carries `id`, `status`, `short_url` ("authorization payment URL"), `current_start`, `current_end`, `charge_at`, `paid_count`, `remaining_count`, `auth_attempts`, `has_scheduled_changes`, `change_scheduled_at`. | https://razorpay.com/docs/api/payments/subscriptions/create-subscription/ ; https://razorpay.com/docs/api/payments/subscriptions/fetch-subscription-id/ |
| V2 | Webhook signature | Header `X-Razorpay-Signature`. Quoted: "The hash signature is calculated using HMAC with SHA256 algorithm; with your webhook secret set as the key and the webhook request body as the message." It must be computed over the raw body as received (the page's Node sample re-serializes the body; do not copy that). The page does not print the digest encoding; Razorpay's SDKs compare a hex digest. | https://razorpay.com/docs/webhooks/validate-test/ |
| V2 | Event id, duplicates, ordering | Quoted: "You can identify the duplicate webhooks using the `x-razorpay-event-id` header. The value for this header is unique per event." Duplicates are expected, and "you may not always receive the webhooks in order." | https://razorpay.com/docs/webhooks/validate-test/ ; https://razorpay.com/docs/webhooks/best-practices/ |
| V3 | Cancel | `cancel_at_cycle_end` is a boolean, default `false` (immediate). With `true` the status becomes `cancelled` only at the end of the current billing cycle. Documented as cancellable: `active` and `authenticated`. Documented errors: "Subscription is not cancellable in expired status.", "Subscription is not cancellable in cancelled status.", "Subscription cannot be cancelled since no billing cycle is going on." (cycle-end cancel requested on a `created`/`authenticated` subscription; the stated fix is to cancel immediately), "The subscription is in its final cycle and cannot be cancelled now.", "Request failed because another subscription operation is in progress." Cancelling a `pending` or `halted` subscription is **not documented**. | https://razorpay.com/docs/api/payments/subscriptions/cancel-subscription/ |
| V3 | Who else can cancel | Quoted: "Your customer can cancel only those subscriptions authorised via UPI from their UPI app." A customer cannot pause or cancel an eMandate subscription. | https://razorpay.com/docs/payments/subscriptions/faqs/ |
| V4 | Provider retry schedule | Cards, quoted: "Let T=0 be the charge day. On T=0, we attempt to charge the card. If the charge fails, the Subscription moves to the `pending` state, and we automatically reattempt the charge on T+1 day. If the charge fails again, we automatically reattempt the charge two more times on T+2 and T+3 days, respectively." UPI follows the same T+1, T+2, T+3 pattern. eMandate retries "only when we get the confirmation or rejection of the last payment, as it may take more than 24 hours". After the retries are exhausted the subscription is `halted`: invoices keep being generated, but no auto-charge is attempted. | https://razorpay.com/docs/payments/subscriptions/payment-retries/ ; https://razorpay.com/docs/payments/subscriptions/states/ |
| V4 | Provider notices to the payer | With `customer_notify` true: "An email and SMS is sent when a charge on a card fails", and again each time the subscription moves to `pending` and to `halted`. Each notice contains an "Update Card" option. | https://razorpay.com/docs/subscriptions/notifications/ |
| V4 | Recovery | From `pending`: if the customer changes the card, Razorpay charges the last invoice and, on success, the subscription is `active`. From `halted`, quoted: "Once the Subscription moves to the `active` state from the `halted` state, the previous charges are not re-attempted. Only future payments are charged automatically." The missed invoice then needs a manual charge. | https://razorpay.com/docs/payments/subscriptions/states/ ; https://razorpay.com/docs/payments/subscriptions/payment-retries/ |
| V5 | `schedule_change_at` | `now` (default) updates immediately; `cycle_end` updates at the end of the current billing cycle. Immediate upgrade, quoted: "Razorpay creates an invoice and charges the customer the difference amount." Immediate downgrade: Razorpay refunds the difference with a credit note. The prorated difference must be at least 50 currency subunits. At cycle end "there is no need for any amount adjustment with the customer". Quoted: "If the plans have different billing cycles, the new plan is billed at the new interval, starting on the day of the change." Whether an immediate change between two plans of the same interval moves `current_start`/`current_end` is not documented. | https://razorpay.com/docs/payments/subscriptions/update/ ; https://razorpay.com/docs/api/payments/subscriptions/update-subscription/ |
| V5 | Who can be updated | Only `authenticated` and `active` subscriptions; "Subscriptions in the `created`, `pending` or `halted` state cannot be updated." Quoted: "Subscriptions cannot be updated when payment mode is UPI", "Subscriptions cannot be updated when payment mode is emandate", and "For Subscriptions created using domestic cards, you can update only the offer that is linked to them." The FAQ says only "You can only update a Subscription authorised using cards and not via UPI and Emandate." Read together, a plan change by update is documented only for international cards. | https://razorpay.com/docs/payments/subscriptions/update/ ; https://razorpay.com/docs/api/payments/subscriptions/update-subscription/ ; https://razorpay.com/docs/payments/subscriptions/faqs/ |
| V6 | Webhook delivery | A delivery succeeds only with a `2xx` response within 5 seconds. Quoted: "If there is a delivery failure, we retry the delivery in exponential backoff policy for 24 hours after event creation timestamp." After 24 hours of continuous failure the webhook is disabled, the alert email is notified, and "You need to enable the webhook from the Dashboard after fixing the errors at your end." | https://razorpay.com/docs/webhooks/best-practices/ |
| V7 | Page for fixing a failed payment | `short_url` on the subscription is documented as the **authorization** payment URL, not as a payment-method update page. The documented routes are (a) the link in Razorpay's failure email ("a link that the customer can use to change the card details associated with the Subscription"; the URL is not exposed by the API) and (b) the merchant's own Razorpay Checkout opened with the subscription id and `subscription_card_change` set to true. Supported switches on the hosted page: card → card, UPI or eMandate; UPI → card only; eMandate → card only. | https://razorpay.com/docs/payments/subscriptions/payment-retries/ ; https://razorpay.com/docs/api/payments/subscriptions/create-subscription/ |
| V8 | Sample payloads | Published for every subscription event, with the top-level keys `entity`, `account_id`, `event`, `contains`, `payload`, `created_at`. `subscription.charged` and `subscription.completed` contain `subscription` and `payment`; `subscription.pending`, `.halted`, `.cancelled`, `.updated` and `.authenticated` contain only `subscription`. | https://razorpay.com/docs/webhooks/payloads/subscriptions/ |
| V9 | API retry of a failed charge | **Not available.** The Subscriptions API and the Invoices API list no endpoint that charges an invoice or re-attempts a payment. The manual charge is a Dashboard action: open the subscription in the `pending` state, open the invoice in the `issued` state, click "Attempt Charge". Quoted: "Manual charging of a domestic card is not supported." | https://razorpay.com/docs/payments/subscriptions/manually-charge-card/ ; https://razorpay.com/docs/api/payments/subscriptions/ ; https://razorpay.com/docs/api/payments/invoices/ |

Related facts found during the same check:

- **Invoice reminder endpoint.** `POST /v1/invoices/{id}/notify_by/{sms|email}` sends the invoice's short URL to the customer. Quoted: "Notifications can only be sent for invoices in the `issued` or `partially_paid` state." The page does not say whether it applies to subscription-generated invoices. Source: https://razorpay.com/docs/api/payments/invoices/resend/
- **Subscription invoices** are listable (`GET /v1/invoices?subscription_id=`) and each carries `status`, `payment_id`, `short_url`, `billing_start`, `billing_end` and `amount_due`. Source: https://razorpay.com/docs/api/payments/subscriptions/fetch-invoices/
- **UPI amount cap.** Quoted: "The maximum transaction amount allowed is ₹15,000 when using UPI as a payment method for Subscriptions." Any tier priced above that cannot be paid by UPI AutoPay. Source: https://razorpay.com/docs/payments/subscriptions/faqs/

- **Invoice entity.** The sample invoice carries `subscription_id`, `payment_id`, `status` (`paid` in the sample), `issued_at`, `paid_at`, `amount`, `amount_paid`, `amount_due`, `billing_start`, `billing_end`, `short_url` and `created_at`. Source: https://razorpay.com/docs/api/payments/subscriptions/fetch-invoices/
- **Authorization amount.** Quoted: "The authentication transaction amount is the first amount you charge on the customer's card." For an immediate-start subscription it equals the plan amount and is not refunded; for a future-start subscription it is "₹5 (auto refunded)". Quoted: "An invoice is generated at the beginning of each billing cycle." Source: https://razorpay.com/docs/payments/subscriptions/workflow/
- **Signature encoding (primary source, not test mode).** Razorpay's official Python SDK computes `hmac.new(key=key, msg=body, digestmod=hashlib.sha256).hexdigest()` and compares it with `hmac.compare_digest`. Source: https://raw.githubusercontent.com/razorpay/razorpay-python/master/razorpay/utility/utility.py (read 2026-09-30).

### Test-mode confirmations

No Razorpay test-mode call has been made yet (no test credentials were available on 2026-09-30). Nothing below is recorded as passed on test-mode evidence. When a check is run, add the date, the request and the observed response here.

| # | Question | Status on 2026-09-30 |
|---|---|---|
| T1 | Does `POST /subscriptions/{id}/cancel` succeed on a `pending` or `halted` subscription? | Unresolved. The documentation lists only `active` and `authenticated`. |
| T2 | Is `total_count = 1200` on a monthly plan accepted for each payment method's mandate? | Unresolved. |
| T3 | Is the `X-Razorpay-Signature` value a lowercase hex digest? | Resolved from the SDK source above. Not yet observed on a delivery. |
| T4 | Does an immediate update between two monthly plans change `current_start`/`current_end`? | Unresolved. |
| T5 | Does `notify_by` work on a subscription-generated invoice, and does paying that invoice's `short_url` return the subscription to `active`? | Unresolved, and not relied on by the Phase 07 spec. |
| T6 | Does a domestic-card subscription reject a `plan_id` update (the update guide and the FAQ differ)? | Unresolved. |
| T7 | Do a subscription invoice's `billing_start`/`billing_end` bracket the subscription's `current_start` for the cycle it pays, and does a `halted` → `active` recovery leave the missed invoice `issued`? | Unresolved. |

### How these facts were resolved

Signed off on 2026-09-30 and written into this document:

- **Dunning (§K).** Razorpay offers no API retry (V9), its own retries run on T+1, T+2 and T+3, and nothing happens on the provider side on day 6. Day 0, 3 and 6 are therefore ReviewFlow recovery checkpoints; Razorpay owns the retries and the payer notices. See §K and §N.
- **Recovery (§K).** A `halted` subscription can return to `active` without the missed cycle being charged (V4), so recovery requires a paid invoice for the current cycle. See §N "Paid-entitlement rule".
- **Upgrade and downgrade (§E, §F).** A plan change by updating the subscription is documented only for international cards (V5). §E and §F are unchanged; §N "Plan change at the provider" records what V1 does meanwhile.

## N. Provider Integration Rules (signed off 2026-09-30)

These rules bind the Razorpay integration. They implement §A–§L; where they add precision, this section governs.

### Subscription states

`Subscription.status` is one of `INCOMPLETE`, `ACTIVE`, `PAST_DUE`, `CANCELLED`, `EXPIRED`. There is one `Subscription` row per merchant, reused when the merchant resubscribes.

| Status | Sending entitled | Meaning |
|---|---|---|
| (no row) | No | the merchant never started a checkout |
| `INCOMPLETE` | No | checkout started, first payment not confirmed |
| `ACTIVE` | Yes, while `now < current_period_end` and quota remains | the current period is proven paid |
| `PAST_DUE` | No, immediately | a renewal failed and no paid invoice for the current period is proven; the 7-day grace is running |
| `CANCELLED` | No | ended by cancellation |
| `EXPIRED` | No | the grace ran out unpaid |

`INCOMPLETE` exists because the provider subscription id must be stored server-side at checkout, before any payment, so that a later webhook can be resolved to the merchant. Activation happens only through provider state reconciliation (a webhook signal or the maintenance sweep, followed by a fetch from Razorpay). It never happens from a browser callback. There is no trial: a merchant without an `ACTIVE` subscription cannot send.

### Provider state is fetched, not replayed

A Razorpay webhook is only a signal. ReviewFlow records it, then fetches the subscription's current state from the Razorpay API and converges to it. Transitions never come from the webhook's event type or from the order in which events arrive. This one mechanism handles duplicate delivery, out-of-order delivery, lost webhooks and reconciliation.

Every status, plan or period change is applied under a row lock on the `Subscription`. A fetch is applied only if it started after the last applied fetch (`provider_synced_at`) and the row still holds the same provider reference; a stale fetch is dropped.

### Paid-entitlement rule

Razorpay's `active` status does **not** prove payment, so **`provider_status = active` alone never grants entitlement**.

An invoice qualifies as proof for the current billing cycle only if it meets all of:

- it is returned by `GET /v1/invoices?subscription_id=<ref>` and its `subscription_id` equals the stored provider reference;
- `status` is `paid`, `amount_due` is `0`, and `payment_id` is present;
- `billing_start <= current_start < billing_end`, where `current_start` is the fetched subscription's current cycle start.

A qualifying paid invoice is required for:

- every transition into `ACTIVE` (from `INCOMPLETE`, `PAST_DUE`, `CANCELLED` or `EXPIRED`);
- every period advance on an `ACTIVE` subscription;
- and therefore every `UsageRecord` creation.

It is not required for a plan refresh inside an unchanged period, or for any transition out of `ACTIVE`.

The rule fails closed. No qualifying invoice, or an invoice fetch that fails, means no transition: the status stays as it is, the period does not advance and no `UsageRecord` is created. A `PAST_DUE` subscription stays `PAST_DUE` and its 7-day clock keeps running.

When the rule is satisfied, the invoice's `payment_id` is recorded as a `PaymentAttempt` in the same transaction, with the invoice's `paid_at` as its time. This is the only writer of the payment ledger: the webhook stores the event and triggers this sync, but writes no payment itself.

### Razorpay status → ReviewFlow status

| Razorpay `status` | Local `INCOMPLETE` | Local `ACTIVE` | Local `PAST_DUE` | Local `CANCELLED` / `EXPIRED` |
|---|---|---|---|---|
| `created`, `authenticated` | no change | no change | no change | no change |
| `active`, current period **paid** | → `ACTIVE` | same period: refresh plan. New period: advance it | → `ACTIVE` | → `ACTIVE` |
| `active`, current period **not proven paid** | no change | same period: refresh plan. New period: no change (lapsed, not entitled) | no change (stays `PAST_DUE`) | no change |
| `pending`, `halted` | no change | → `PAST_DUE` | no change | no change |
| `cancelled`, `completed`, `expired` | no change | → `CANCELLED` | → `CANCELLED`, or `EXPIRED` if the grace has ended | no change |
| `paused` or unknown | no change, logged | no change, logged | no change, logged | no change, logged |

- Razorpay `halted` never produces `EXPIRED`. ReviewFlow's own 7-day clock does (§K).
- The last Razorpay status applied is stored in `Subscription.provider_status`, including for "no change" cells.
- A fetched subscription whose plan matches no `Plan.provider_plan_id` changes nothing and is logged. A plan is never guessed.

### Billing period and the maintenance sweep

- The billing period is the provider's charge cycle. It is per merchant (anniversary based), not a calendar month.
- A new period begins only when Razorpay reports `active` with a new cycle **and** the paid-entitlement rule holds for it. The same transaction creates the new `UsageRecord` with `requests_used = 0`; the old record is left as it is (§G, no rollover). This step is the `reset_usage_period` service. It is idempotent on the `UsageRecord` unique constraint.
- Reactivation inside the stored period does not reset quota: when a cancelled or expired subscription returns to `ACTIVE` with a proven paid period equal to the one already stored, that period's existing `UsageRecord` is reused with its usage unchanged (insert-or-ignore on the unique period constraint, plus no rollover). A fresh `UsageRecord` is opened only for a new, qualifying billing period; there is no other quota-reset path.
- There is no monthly reset task. A Celery Beat sweep (`run_billing_maintenance`, every 15 minutes) finds the merchants whose subscription needs attention and enqueues, per merchant, a provider-sync task and an independent dunning task, each with an explicit `merchant_id`. Dunning (the day-3/day-6 checkpoints and the day-7 expiry) never waits on the sync, and a failure for one merchant does not stop the others. A subscription needs attention when it has an unprocessed `BillingEvent`, is `INCOMPLETE` with a provider reference, is `ACTIVE` with an elapsed period, is `PAST_DUE`, or is `CANCELLED`/`EXPIRED` while the provider subscription is still open.
- An `ACTIVE` subscription whose period has elapsed without a webhook is **not** moved to `PAST_DUE`. Only a provider failure signal does that. It is simply not entitled until a sync advances the period.
- Billing state keeps synchronizing for a `SUSPENDED` merchant.

### Checkout reconciliation

`POST /billing/checkout` never cancels or replaces a provider subscription on the strength of local state alone. If the row already holds a provider reference, the request first fetches that subscription (and, when it is `active`, its invoices), applies the mapping above, and only then routes on the resulting state.

- **Paid but not yet webhooked.** The fetch shows `active` with a qualifying paid invoice: the row becomes `ACTIVE`, and the paid subscription is not cancelled. What the request then returns depends on what the row was before the request: if this request's own reconcile moved it into `ACTIVE` and the requested plan is the plan now in force, it is a `200` no-op (no further provider call, no further audit row); any other plan is handled as a plan change. A row that was already `ACTIVE` before the request is not in this case: the same plan is `422`.
- **Active but not proven paid, or authorized.** The fetch shows `authenticated`, or `active`/`pending`/`halted` without a qualifying invoice, on an `INCOMPLETE` row: the payer has authorized and may have paid, so that provider subscription is never cancelled or replaced. A same-plan checkout returns the existing checkout; a different-plan checkout is refused (`409 subscription_activating`).
- **Late successful payment.** A `CANCELLED` or `EXPIRED` row whose old provider subscription is `active` with a qualifying paid invoice converges to `ACTIVE`; no second provider subscription is created.
- **Replacing a reference.** The stored reference is replaced only when the old provider subscription is terminal (`cancelled`, `completed`, `expired`) or has just been cancelled by the same request. An old subscription that is still open (`created`; or `pending`, `halted`, or `active` without a paid current period, on a `CANCELLED`/`EXPIRED` row) is cancelled first. If that cancel fails or is refused, the request fails and no new provider subscription is created, so two live provider subscriptions never exist for one merchant. (The one exception is the plan-change replacement, §N "Plan-change replacement": at most two, the entitled one plus one replacement.)
- **What a replacement resets.** When a new provider subscription is stored on an existing row (a `CANCELLED` or `EXPIRED` row, or an `INCOMPLETE` row being replaced), the row starts a new lifecycle: `plan` is the requested plan, `status` is `INCOMPLETE`, the new validated reference and the create response's provider status are stored, and `pending_plan`, `provider_synced_at`, `current_period_start` and `current_period_end` are cleared and `cancel_at_period_end` is set to false. `INCOMPLETE` means the new subscription's first payment is not yet confirmed, so the old period and usage are not carried over. `UsageRecord`, `PaymentAttempt`, `BillingEvent` and `AuditLog` rows are never deleted: the previous lifecycle stays in the history.
- **Which provider states may be touched.** One shared rule, used by checkout and by the maintenance sweep's owed provider cancel. A provider subscription may be cancelled only while it is `created`, `pending`, `halted` or `active` (without a paid current period), and only on a `CANCELLED`/`EXPIRED` row. An `authenticated`, `paused` or unrecognized provider subscription is never cancelled, replaced or otherwise touched, by checkout or by the sweep. Checkout answers `409 subscription_provider_state_unsupported` for `authenticated` on a `CANCELLED`/`EXPIRED` row, for `paused`, and for an unrecognized status; and `409 subscription_plan_unsupported` when a known status carries a plan that maps to no ReviewFlow plan. Nothing is changed locally or at the provider.
- **Provider failures.** A failed fetch of the stored subscription, including a permanent `4xx`, fails closed with `502 billing_provider_unavailable`: nothing is created, replaced or cancelled, and no local state changes. ReviewFlow has no automatic replacement for it; the merchant retries later or contacts support. The subscription id in a create response is validated against `^[A-Za-z0-9_]{1,64}$` before it is stored; if it is unusable, the request is a `502` and nothing is stored.
- **Reusing an `INCOMPLETE` subscription.** A same-plan checkout reuses it only when the fetched plan equals the requested plan's Razorpay plan id; otherwise the plan-change and cancel rules above apply.
- A double submit is serialized by the row lock, or by the one-row-per-merchant unique constraint on a first checkout, and returns the same provider subscription.

### Plan change at the provider

- Where Razorpay allows a subscription update, an upgrade is applied immediately (`schedule_change_at = now`) and a downgrade is scheduled for the cycle end (`schedule_change_at = cycle_end`), as §E and §F require. Razorpay invoices the prorated difference on an immediate upgrade; ReviewFlow computes no proration and follows the period Razorpay reports.
- **Upgrade period creation.** If an immediate upgrade makes Razorpay report a new cycle, the new period and its `UsageRecord` are created only with a qualifying paid invoice for that cycle. If the cycle is unchanged, consumed usage is kept and only the plan, and therefore the quota, changes.
- "Upgrade" means the target plan's `monthly_price` is higher than the current plan's; "downgrade" means lower. No plan change is accepted during `PAST_DUE` (§K).
- **V1 limitation.** Razorpay documents a plan update only for international cards (§M, V5). For UPI, eMandate and domestic-card subscriptions the update is refused, and ReviewFlow returns `409 plan_change_unsupported` and changes nothing. Such a merchant can cancel (effective at the period end) and check out the other plan once the period has ended.
- §E and §F are not amended by this limitation. Delivering them for those payment methods is the plan-change replacement below. It is **off by default** and cannot grant entitlement until D1 is resolved.

### Plan-change replacement

*Spec: `.claude/specs/07-plan-change-replacement.md`. Implemented behind two flags that default off; every provider-specific assumption is a pre-production gate (G-0 to G-6, D1, T7). Nothing here is verified against Razorpay.*

- **C1: unused paid time is forfeited.** No automatic refund or credit and no proration. `acknowledge_no_credit: true` is required only for an upgrade that falls back to a replacement. A goodwill refund is a staff action in the Razorpay Dashboard.
- **C2: a downgrade starts at the next period.** The replacement is created to start at the old `current_period_end`; the old plan, quota and usage stay in force until the switch. A downgrade replacement is not created until its verification fields are evidenced (G-2) or once the commit cutoff has passed.
- **C3: the provider update is always tried first.** A replacement is created only after a refusal classified as "update not supported" (G-0; the classifier set ships empty, so nothing classifies yet) and only with that kind's flag on and its duration set.
- **A second Razorpay subscription** is created for the target plan; the merchant's row keeps its plan and entitlement until the replacement is **proven paid** by the paid-entitlement rule (a qualifying paid invoice, so D1 gates it). At most two provider subscriptions exist: the entitled one plus one replacement, or one awaiting confirmed termination.
- **The switch** (one transaction): plan and period become the replacement's, the old reference moves to the retired slot (`SWITCHED_OLD`), the replacement columns, `pending_plan` and the cancellation flag clear, and a fresh `UsageRecord` opens at 0 (no rollover). A `PAST_DUE` row recovers to `ACTIVE`. An upgrade's old subscription is cancelled right after the switch.
- **Abandon** (local state move, the reference goes to the retired slot as `ABANDONED_REPLACEMENT`): a failed or unpaid replacement (`pending`, `halted`, `cancelled`, `expired`), a `created` one past its deadline, a `PAST_DUE` transition, the row ending, or the merchant. A replacement the provider reports `active` is never cancelled by ReviewFlow.
- **Downgrade commit point:** once an `authenticated` replacement is verified (status, plan, start equal to the old period end, expiry equal to the stored deadline, before the commit cutoff = old period end minus `BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN`; the deadline sent to Razorpay is the cutoff minus 1 second), the intent is committed in one transaction, the old subscription is cancelled at its cycle end outside any transaction and without the row lock, and a second transaction records the confirmation. An unknown outcome is treated as committed and the sweep re-issues the cancel. After commit the downgrade cannot be abandoned to keep the old plan; if its first charge then fails, the old subscription's `cancelled` applies and the merchant checks out again.
- **Retired subscription:** swept until Razorpay reports it terminal; a switched-out subscription is cancelled in any non-terminal state, an abandoned replacement only while `created` or `authenticated`.
- **Merchant cancel:** a pending non-active replacement is cancelled first (`502` and nothing changed on failure), an `active` one is `409 replacement_activating`, a committed downgrade replacement is cancelled too, then the normal cancel runs. `POST /billing/replacement/cancel` abandons a pending replacement (`409 replacement_committed` after a commit).
- Sync order per merchant: replacement, then main subscription, then retired subscription; a failure or the D1 stop in one step does not stop the later ones.

### Dunning checkpoints

Razorpay owns every payment retry (T+1, T+2, T+3, then none) and every notice to the payer. ReviewFlow cannot retry a charge, and it sends no payment reminder.

A checkpoint does exactly this:

1. it forces a reconcile with Razorpay (subscription and, if `active`, invoices), which picks up the result of Razorpay's retries or of a payer's fix;
2. if the subscription is still `PAST_DUE`, it advances `Subscription.dunning_stage` to the checkpoint's day;
3. it writes one `billing.dunning_checkpoint` audit event, only when the stage advanced.

- Stage `0` is set in the transaction that sets `PAST_DUE`. Stages `3` and `6` are reached at `past_due_at + 3 days` and `past_due_at + 6 days`.
- The advance is a conditional update (`... WHERE dunning_stage < N`), so a repeated run changes nothing and writes nothing. A sweep that was down across a boundary goes straight to the highest stage that is due.
- `dunning_stage` is cleared together with `past_due_at` when the grace episode ends.
- A checkpoint writes no `PaymentAttempt`.

### Cancellation and expiry

- An `ACTIVE` subscription is cancelled at the period end: Razorpay is told to cancel at cycle end, `cancel_at_period_end` is set, and the subscription stays `ACTIVE` and entitled until Razorpay reports it cancelled. If that provider call fails, nothing changes. Before it decides, an `ACTIVE` subscription that is not already cancelling is reconciled with Razorpay, as checkout is; if the provider subscription is then found `paused`, the request is refused with `409 subscription_provider_state_unsupported` and nothing is cancelled or changed. An already-cancelling subscription is a `200` no-op with no provider call.
- A `PAST_DUE` subscription is cancelled immediately and locally: the row becomes `CANCELLED` whatever Razorpay then allows. The provider cancel is attempted and, if it fails or is refused, retried by the sweep.
- Razorpay reporting a status never by itself moves a `CANCELLED` subscription, not even `active`. Only the late-charge rule reactivates it: Razorpay `active` **and** a qualifying paid invoice for that period (the full paid-entitlement evidence). Because the merchant's cancel is local, a snapshot fetched just before it can still be applied after it under these rules: a payment collected before the cancel can reactivate the subscription after the merchant cancelled, and the next sync, seeing Razorpay `cancelled`, returns it to `CANCELLED`. The cancel is not sticky. If a recovery is applied first, the cancel then takes effect at the end of the period.
- Grace expiry is unconditional and measured in absolute time. At exactly `past_due_at + 7 days` the subscription becomes `EXPIRED` in its own task, whether or not a sync has run or failed; the independently enqueued sync reconciles it, and a payment that lands around the cutoff either recovers the row first (the expiry then finds it no longer `PAST_DUE` and does nothing) or reactivates it afterwards through the paid-invoice rule. The provider cancel that follows is best-effort and retried; the day-7 rule never waits on it.
- A payer who fixes the payment method after Razorpay has `halted` the subscription leaves the missed invoice unpaid, and no API can charge it. The subscription stays `PAST_DUE` and expires on day 7. The way back is to cancel and check out again.

### Payment ledger

`PaymentAttempt` holds real Razorpay payments only. One row is one payment, keyed by its Razorpay payment id (`pay_...`), and the database refuses any other key. `attempt_type` is `RETRY` for a payment recorded while the subscription was `PAST_DUE`, and `RENEWAL` for any other payment, the first charge included. Dunning checkpoints are never stored there.

## Related Documents## Related Documents

- `../01-product/Business-Rules.md`
- `../03-database/Data-Dictionary.md`
- `../03-database/Database-Design.md`
- `../06-automation/Campaign-Engine.md`
- `../04-api/Webhook-Specification.md`
