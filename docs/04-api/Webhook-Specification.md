# Webhook & Event Processing Specification

This is the contract for the pipeline the entire product starts from:

```
SALE (at the POS/e-commerce system)
   ↓
INBOUND WEBHOOK  (or generic API call / CSV row)
   ↓
Identify Integration (from URL/credentials)
   ↓
Identify Merchant from Integration.merchant_id
   ↓
Authentication / signature verification
   ↓
IntegrationEvent  (event inbox row, status = RECEIVED, merchant_id already set — never null)
   ↓
Idempotency check — (integration_id, external_event_id) unique
   ↓
Normalization — provider-specific parse() → common SaleCreated shape
   ↓
Customer get-or-create — only when the sale supplies a customer phone
   ↓
Transaction create — customer may be null — (location_id, external_transaction_id) unique
   ↓
Eligibility check (see ../01-product/Business-Rules.md §2), including a row lock on the
Transaction to guard against a concurrent execution being created for it (see
../06-automation/Campaign-Engine.md §"Concurrency & Locking")
   ↓
CampaignExecution create — (campaign_id, transaction_id) unique
```

**Tenant-safety note**: the merchant is identified from the `Integration`, not from the event payload, and *before* the `IntegrationEvent` row is created — `merchant_id` is never written as null and resolved during normalization. See `../02-architecture/Multi-Tenancy.md`.

## Inbound Webhook Endpoints

| Endpoint | Provider | Auth |
|---|---|---|
| `POST /webhooks/shopify/{integration_id}` | Shopify | `X-Shopify-Hmac-Sha256` (base64 HMAC-SHA256 over the raw body, keyed with the platform app client secret — `Authentication.md` §3), plus `X-Shopify-Shop-Domain` bound to the integration's stored `shop_domain`. Returns `200` for a valid `orders/paid` (stored) or `app/uninstalled` (disconnects, or a no-op duplicate) delivery, and for any other verified topic (not stored). `401 invalid_signature` for an unknown/malformed id, a missing/invalid HMAC, a shop-domain mismatch, a missing `X-Shopify-Webhook-Id`, an `app/uninstalled` body that fails the current-topic discriminator, or a `DISCONNECTED` integration for any topic other than a verified, bound `app/uninstalled` — see `05-integrations/Shopify.md`. |
| `POST /webhooks/woocommerce` | WooCommerce | WooCommerce webhook secret |
| `POST /webhooks/petpooja` | Petpooja | Petpooja's own signature scheme |
| `POST /webhooks/gofrugal` | GoFrugal | GoFrugal's own signature scheme |
| `POST /webhooks/generic/{integration_id}` | Any (merchant-configured mapping) | `X-ReviewFlow-Signature: sha256=<hex HMAC-SHA256>` over the raw body, keyed with a server-generated secret (never merchant-supplied) shown once at connect |
| `POST /webhooks/whatsapp/status` | Meta | Meta's webhook signature |
| `POST /webhooks/whatsapp/inbound` | Meta (customer replies, incl. opt-out keywords) | Meta's webhook signature |
| `POST /billing/webhooks/razorpay` | Razorpay (payment gateway) | `X-Razorpay-Signature`: hex HMAC-SHA256 over the raw request body, keyed with the platform `RAZORPAY_WEBHOOK_SECRET`. Not a sale-capture webhook: it has no `Integration` and writes no `IntegrationEvent` — see "Billing Webhook (Razorpay)" below. |

**Fail-closed rule**: a missing or invalid signature is always rejected (`401`) and never stored or processed — this applies to every endpoint above without exception.

## Idempotency

- Every inbound sale-capture call writes an `IntegrationEvent` row **before** any processing, with `status = RECEIVED` and `merchant_id` already resolved from the `Integration`.
- `UNIQUE(integration_id, external_event_id)` — **revised from `(source, external_event_id)`**. Scoping to the bare provider name was not tenant-safe: the same `external_event_id` (e.g. an invoice number) can legitimately recur across two different merchants both using the same POS provider. Scoping to `integration_id` (which is itself merchant-specific) closes that gap. A retried delivery of the same event to the same integration is caught at the database and returns `200` without reprocessing.
- Processing itself (in a Celery task) is additionally idempotent: it processes only `RECEIVED`/`FAILED` events (`PROCESSED`, `DEAD_LETTER` and `CANCELLED` are no-ops), so even a duplicate task enqueue is safe.
- Downstream, `CampaignExecution(campaign_id, transaction_id)` is the next backstop, now paired with a `SELECT ... FOR UPDATE` lock on the `Transaction` row at creation time to close a race between two campaigns (or two concurrent workers) both attempting to create an execution for the same transaction at once — see `../06-automation/Campaign-Engine.md` §"Concurrency & Locking". The unique constraint remains as an additional database-level safety net on top of the lock, not a replacement for it.

## Event Statuses

`RECEIVED → PROCESSED` (happy path), or `RECEIVED → FAILED → (retry) → PROCESSED`, or `FAILED → DEAD_LETTER` after N attempts, or `RECEIVED`/`FAILED → CANCELLED` when the integration is disconnected. `PROCESSED`, `DEAD_LETTER` and `CANCELLED` are terminal; disconnecting an integration stops processing of its pending events.

## Retries & Failure Handling

- Processing failures (bad payload, transient DB error) set `status = FAILED` with a stable `error_code` and a controlled, PII-safe `error_message` — never exception text, payload values, phone numbers, credentials or tokens. A sale with no customer phone is not a failure; it is recorded as a `Transaction` without a `Customer`.
- A scheduled task (`retry_failed_events`, every 5 minutes) retries `FAILED` events with exponential backoff.
- After a capped number of attempts, the event moves to `DEAD_LETTER` for manual review in the admin panel — it is never silently dropped.

## Billing Webhook (Razorpay)

Canonical path: **`POST /api/v1/billing/webhooks/razorpay`**. It is the only billing webhook endpoint. It is not part of the sale pipeline above: it has no `Integration`, writes no `IntegrationEvent`, and is handled by the `billing` app. Full rules: `../08-billing/Billing-Specification.md` §N.

**Authentication.** `X-Razorpay-Signature` is the hex HMAC-SHA256 of the **raw request body**, keyed with the platform `RAZORPAY_WEBHOOK_SECRET` and compared in constant time. No session, no API key, no CSRF. If the secret is unset, every request is rejected. The signature is verified **before any database read**.

**Receive order.**

1. Per-IP throttle (`429` with `Retry-After`).
2. Verify the signature over the raw body. Missing or invalid → `401 invalid_signature`; nothing is read or stored.
3. Require a valid `x-razorpay-event-id` header: it must match `^[\x21-\x7E]{1,64}$`. Missing, empty, non-string, longer than 64 characters, or containing whitespace, a control character or a non-ASCII character → the same `401 invalid_signature`, identical for every failure; no `BillingEvent` is stored, no billing lookup runs, and nothing is marked processed. The failure is logged by category only (`missing`, `empty`, `not_string`, `too_long`, `invalid_characters`), never the raw id, its length, the signature or the body. A valid id is compared exactly, case-sensitively and without trimming; a duplicate valid id is handled in step 7 by `UNIQUE(provider, provider_event_id)`.
4. Parse the JSON. Not an object, no `event` string, or an `event` that is empty or longer than 64 characters → `400 invalid_payload`; nothing is stored, the value is never truncated and is not logged.
5. Take the provider subscription id from `payload.subscription.entity.id`. An event with no subscription entity → `200`, nothing stored. An id that is present but not a string, or does not match `^[A-Za-z0-9_]{1,64}$`, is treated like step 6's no-row outcome: `200 {}`, nothing stored, no lookup, no reconciliation, and the raw value is not logged.
6. Resolve the merchant through the pre-tenant `billing_ref_lookup` (`../02-architecture/Multi-Tenancy.md`): the `Subscription` whose stored provider reference equals that id. No such row → `200`, nothing stored.
7. Inside that merchant's tenant context: insert a `BillingEvent` (`UNIQUE(provider, provider_event_id)`; a duplicate is a `200` no-op); no `PaymentAttempt` is written from the payload (it carries no invoice, so no `paid_at`); enqueue the subscription sync on commit. The sync records the payment from the qualifying paid invoice. Every committed event enqueues its own sync; nothing coalesces enqueues. The sync fetches from Razorpay only while the merchant has an unprocessed `BillingEvent`: an event is marked processed only by a settled sync whose fetch started more than 5 seconds after the event was received (a margin for clock skew between the web and worker hosts: it absorbs ordinary skew but is not a guarantee under arbitrary drift), so a sync that finds none has nothing left to observe. An event received within that margin of a fetch stays unprocessed and is observed by the next sync. A replayed signed body under fresh event ids therefore costs one provider fetch per burst of events older than the margin, not one per delivery. Only a unique violation of `(provider, provider_event_id)` is a duplicate; any other database integrity error is not answered as one. An event whose enqueue was lost (broker failure, or a process exit between commit and enqueue) stays unprocessed and is observed by the next event's sync or by the sweep below.
8. `200`.

**Tenant safety.** The merchant comes only from the `Subscription` row found by the provider reference that ReviewFlow itself stored at checkout. `notes`, customer fields and every other payload value are ignored for tenancy.

**Idempotency and ordering.** `x-razorpay-event-id` is unique per event and is the dedup key. Razorpay may deliver an event more than once and out of order. Neither matters: the event is only a signal, and processing fetches the subscription's current state from the Razorpay API and converges to it. The payload is not stored.

**Entitlement.** A webhook never grants entitlement by itself, and Razorpay reporting the subscription `active` is not enough either: activation and period advance require a qualifying paid invoice (`Billing-Specification.md` §N, "Paid-entitlement rule").

**Delivery.** Razorpay counts a delivery as failed unless it receives a `2xx` within 5 seconds, retries with exponential backoff for 24 hours, and then disables the webhook until it is re-enabled by hand in the Razorpay Dashboard. The receiver therefore makes no provider call. A Beat sweep every 15 minutes re-syncs every subscription that is not in a settled state, so a lost webhook is recovered without Razorpay's retry.

## Outbound Webhooks *(future — `WebhookEndpoint` model exists in the schema for this)*

- HMAC-SHA256 signed with the endpoint's own secret.
- Payload includes a timestamp to prevent replay.
- Retried with exponential backoff on any non-2xx response.

## Normalized Event Schema (`SaleCreated`)

```json
{
  "event": "sale.completed",
  "merchant_id": "mer_123",
  "location_id": "loc_001",
  "source": "petpooja",
  "external_transaction_id": "INV-12345",
  "customer": { "name": "Rahul", "phone": "+919999999999" },
  "amount": 850,
  "currency": "INR",
  "payment_method": "UPI",
  "occurred_at": "2026-09-18T13:00:00+05:30"
}
```

`customer` (and `customer.phone`) may be absent: a sale without a customer phone is still recorded as a `Transaction`, with no `Customer`. A phone that is supplied must be valid E.164.

This is the only shape any downstream consumer (eligibility, campaign scheduler) ever sees — no consumer knows or cares which provider produced it. Every adapter's `normalize()` method is responsible for producing exactly this shape; see `../05-integrations/Integration-Architecture.md`.

## Security Notes

- Raw inbound payloads are stored in `IntegrationEvent.payload` for auditability, subject to the retention policy in `../09-security/Privacy-Data-Retention.md`.
- A spike in invalid-signature attempts on any endpoint is worth alerting on — it may indicate a misconfigured merchant integration or an attack.
