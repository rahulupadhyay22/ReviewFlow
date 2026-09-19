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
Customer get-or-create
   ↓
Transaction create — (location_id, external_transaction_id) unique
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
| `POST /webhooks/shopify` | Shopify | Shopify HMAC-SHA256 header |
| `POST /webhooks/woocommerce` | WooCommerce | WooCommerce webhook secret |
| `POST /webhooks/petpooja` | Petpooja | Petpooja's own signature scheme |
| `POST /webhooks/gofrugal` | GoFrugal | GoFrugal's own signature scheme |
| `POST /webhooks/generic/{integration_id}` | Any (merchant-configured mapping) | shared secret configured per `Integration` |
| `POST /webhooks/whatsapp/status` | Meta | Meta's webhook signature |
| `POST /webhooks/whatsapp/inbound` | Meta (customer replies, incl. opt-out keywords) | Meta's webhook signature |
| `POST /webhooks/billing/{provider}` | Payment gateway | provider's signature scheme |

**Fail-closed rule**: a missing or invalid signature is always rejected (`401`) and never stored or processed — this applies to every endpoint above without exception.

## Idempotency

- Every inbound call writes an `IntegrationEvent` row **before** any processing, with `status = RECEIVED` and `merchant_id` already resolved from the `Integration`.
- `UNIQUE(integration_id, external_event_id)` — **revised from `(source, external_event_id)`**. Scoping to the bare provider name was not tenant-safe: the same `external_event_id` (e.g. an invoice number) can legitimately recur across two different merchants both using the same POS provider. Scoping to `integration_id` (which is itself merchant-specific) closes that gap. A retried delivery of the same event to the same integration is caught at the database and returns `200` without reprocessing.
- Processing itself (in a Celery task) is additionally idempotent: it checks `IntegrationEvent.status != PROCESSED` before doing anything, so even a duplicate task enqueue is safe.
- Downstream, `CampaignExecution(campaign_id, transaction_id)` is the next backstop, now paired with a `SELECT ... FOR UPDATE` lock on the `Transaction` row at creation time to close a race between two campaigns (or two concurrent workers) both attempting to create an execution for the same transaction at once — see `../06-automation/Campaign-Engine.md` §"Concurrency & Locking". The unique constraint remains as an additional database-level safety net on top of the lock, not a replacement for it.

## Event Statuses

`RECEIVED → PROCESSED` (happy path), or `RECEIVED → FAILED → (retry) → PROCESSED`, or `FAILED → DEAD_LETTER` after N attempts.

## Retries & Failure Handling

- Processing failures (bad payload, transient DB error) set `status = FAILED` with an error message.
- A scheduled task (`retry_failed_events`, every 5 minutes) retries `FAILED` events with exponential backoff.
- After a capped number of attempts, the event moves to `DEAD_LETTER` for manual review in the admin panel — it is never silently dropped.

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

This is the only shape any downstream consumer (eligibility, campaign scheduler) ever sees — no consumer knows or cares which provider produced it. Every adapter's `normalize()` method is responsible for producing exactly this shape; see `../05-integrations/Integration-Architecture.md`.

## Security Notes

- Raw inbound payloads are stored in `IntegrationEvent.payload` for auditability, subject to the retention policy in `../09-security/Privacy-Data-Retention.md`.
- A spike in invalid-signature attempts on any endpoint is worth alerting on — it may indicate a misconfigured merchant integration or an attack.
