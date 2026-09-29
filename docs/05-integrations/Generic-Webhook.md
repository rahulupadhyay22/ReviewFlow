# Generic Webhook

Implements `integrations.webhook.adapter.GenericWebhookAdapter`, the "connect
almost any business system" path described in `Integration-Architecture.md`.
The merchant defines a JSON field mapping instead of ReviewFlow writing a
provider-specific adapter.

## Connect

`POST /integrations/webhook/connect` — session, OWNER/ADMIN.

- **Never accepts client-supplied `credentials`.** `422` if the request body
  includes any.
- **Requires `config_json.field_map`**, an object mapping ReviewFlow's
  `SaleCreated` fields to dot-separated paths into the raw payload:

  ```json
  {
    "field_map": {
      "external_transaction_id": "order.id",
      "amount": "order.total",
      "currency": "order.currency",
      "occurred_at": "order.completed_at",
      "customer_phone": "customer.phone",
      "customer_name": "customer.name",
      "payment_method": "payment.method",
      "external_location_id": "store.id"
    },
    "default_currency": "INR"
  }
  ```

  `external_transaction_id`, `amount` and `occurred_at` are required paths.
  `currency` is required either as a mapped path or via `default_currency`.
  A numeric path segment indexes a list (`order.items.0.sku`); a missing
  segment resolves to `None`.
- On success, the server **generates** a signing secret
  (`whsec_<32 url-safe chars>`) and returns it in the `201` response body as
  `webhook_secret`, **once**. It is Fernet-encrypted at rest and is never
  returned again by any endpoint.

## Signing

Every delivery must include:

```
X-ReviewFlow-Signature: sha256=<hex HMAC-SHA256(secret, raw_body)>
```

computed over the **raw request body** (before any JSON parsing), keyed
with the secret from connect. Verification uses a constant-time comparison
(`hmac.compare_digest`) and fails closed: a missing header, wrong prefix, or
mismatched digest all return the identical `401`.

## Receiving events

`POST /webhooks/generic/{integration_id}`

1. The `{integration_id}` identifies the `Integration` via the pre-tenant
   `integration_lookup` RLS policy (`Multi-Tenancy.md` §"Integration
   lookup") — before any merchant context exists.
2. The signature is verified over the raw body.
3. A `DISCONNECTED` integration is rejected (`401`) — its secret was
   cleared on disconnect.
4. The body must be a JSON object (`400` otherwise).
5. `external_transaction_id` and `external_location_id` are resolved from
   the raw payload via the field map, and combined into the inbox
   idempotency key
   (`sha256(external_location_id + "\x1f" + external_transaction_id)`); a
   missing `external_transaction_id` returns `422` and nothing is stored.
6. The event enters the unchanged Phase 04 pipeline
   (`IntegrationEvent` → `process_event` → `Transaction`).

Every response before signature verification is the identical `401
invalid_signature` — an unknown id, a malformed id, a wrong-provider id and
a bad signature are indistinguishable to the caller.
