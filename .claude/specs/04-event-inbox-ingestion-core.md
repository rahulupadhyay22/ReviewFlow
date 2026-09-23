# Spec: Event Inbox & Ingestion Core

## Overview
Builds the **ingestion plane** core that every sale-capture integration
ends at. It adds:
- the merchant-level `Integration` and its explicit
  `IntegrationLocationMapping`;
- the `IntegrationEvent` inbox, with tenant-safe idempotency
  (`UNIQUE(integration_id, external_event_id)`, `merchant_id` resolved
  from the `Integration` before the row exists);
- the provider-agnostic adapter layer (`BaseAdapter`, the `SaleCreated`
  normalized event, shared payload validation, the provider registry);
- `Customer` and `Transaction`, plus the processing pipeline that turns
  a stored event into a `Transaction` with the right `Location`. It
  also creates or reuses a `Customer`, but only when the sale supplies
  a phone (Decision 17);
- the `retry_failed_events` Beat sweeper (`FAILED` → retry with backoff
  → `DEAD_LETTER`). Disconnecting an integration cancels its pending
  events (`CANCELLED`, Decision 18);
- the `/integrations` and `/transactions` dashboard endpoints.

It exists now because Phase 06 (Shopify, Generic Webhook, CSV, REST
API), Phase 10 (campaign eligibility) and Phase 17 (phased adapters)
all build on it, and Phase 03 (locations) is Done. Phase 04 ships **no
concrete adapter and no inbound webhook endpoint**. The production
registry is empty. Tests drive the pipeline with a fake adapter that is
registered only in tests.

### Where Phase 04 stops
`Event-Processing.md` describes the pipeline all the way to
`CampaignExecution`. Phase 04 implements it up to and including
**`Transaction` created (or found as a duplicate) →
`IntegrationEvent.status = PROCESSED`**. Everything after that point
belongs to a later phase:

| Not in Phase 04 | Owning phase |
|---|---|
| `SELECT … FOR UPDATE` on `Transaction`, eligibility, `CampaignExecution` | 10 |
| Concrete adapters (Shopify, Generic Webhook, CSV), `POST /webhooks/*` receivers, signature verification in practice, provider-specific `connect` handshakes | 06 (WooCommerce/Petpooja/GoFrugal/Zapier/Make: 17) |
| `POST /sales`, `GET /sales`, API-key auth and per-key rate limits on `/transactions` | 05 / 06 |
| Setting `Customer.opted_out` (inbound WhatsApp opt-out, dashboard opt-out) | 08 |
| `CampaignExecution` summary on `GET /transactions/{id}` | 10 |
| 90-day purge of `IntegrationEvent.payload` | 15 |
| Dead-letter review in Django Admin (audited privileged path) | 16 |
| Invalid-signature spike alerting | 18 |

### Decisions this spec makes where the docs are silent
Each one is written into the named doc during implementation (see
Files to change), so nothing is left ambiguous. None of them changes a
locked decision.
1. **App layout.** `integrations` is the Django app. It owns
   `Integration` and `IntegrationLocationMapping`. `integrations/core/`
   is a plain Python subpackage (`adapters.py`, `events.py`,
   `schemas.py`, `registry.py`) with **no `apps.py` or models**.
   Registering it as a Django app would give it the label `core`, which
   clashes with the existing `core` app. `IntegrationEvent` lives in
   `events`, `Customer` in `customers` and `Transaction` in
   `transactions` (`SAD.md` §3).
2. **Adapter contract signature.** `parse()` receives the stored
   `IntegrationEvent.payload` (a JSON-decoded `dict`), not `bytes`.
   `Event-Processing.md` requires `parse()` to run inside the Celery
   task, and requires parse failures to be stored as `FAILED`. The task
   only has the stored JSON payload, so the receiver JSON-decodes the
   body once, at receipt, and stores it. The `verify` → `parse` →
   `normalize` contract and its responsibilities are otherwise exactly
   as documented. `Integration-Architecture.md` §"The Adapter Contract"
   and `SAD.md` §4 are updated to `parse(self, payload: dict) -> dict`.
3. **Task argument order.** The task is
   `process_integration_event(merchant_id, event_id)`, not
   `(event_id, merchant_id)` as `Event-Processing.md` writes it.
   `core.tenancy.tenant_task` is the implemented contract, and it
   requires `merchant_id` first. The doc's ordering is illustrative.
   `Event-Processing.md` is updated so nobody "fixes" this later.
4. **`SaleCreated` never carries ReviewFlow tenant IDs.** The
   `merchant_id`/`location_id` in the `Webhook-Specification.md` JSON
   example are the *post-resolution* shape. An adapter's `normalize()`
   returns the **provider's** location identifier,
   `external_location_id: str | None`. `merchant_id` always comes from
   `Integration.merchant_id`. `location_id` always comes from
   `IntegrationLocationMapping`. Neither is ever read from a payload.
5. **Location-resolution key.** A mapping's provider location
   identifier is stored at `IntegrationLocationMapping.config_json
   ["external_location_id"]` (string). Resolution rules are under
   Services. An unresolvable location leaves the event `FAILED`
   (retryable). It never creates a `Transaction` without a location.
6. **Retry/backoff and error fields.** `Event-Processing.md` requires
   "error stored", "retried with exponential backoff" and "capped
   number of attempts → DEAD_LETTER". The original `Data-Dictionary.md`
   `IntegrationEvent` table had no field for these. This spec adds
   `attempt_count`, `error_code` and `error_message` (see Decision 19
   for the error fields). They are additions for documented behavior,
   not a locked-decision change, and are already written into
   `Data-Dictionary.md` and `Database-Design.md`. Backoff is keyed off
   the existing `updated_at` (there is no `last_attempt_at`).
7. **Retry policy.** Every processing failure goes to `FAILED`,
   including deterministic ones. The failures are:
   - an adapter-parse/normalize error;
   - a phone that is present but not valid E.164;
   - an unresolvable location;
   - no registered adapter.

   A **missing** phone is not a failure (Decision 17).
   `retry_failed_events` retries failures with exponential backoff: an
   event is due when `updated_at <= now - 5 min × 2^(attempt_count − 1)`.
   After `MAX_EVENT_ATTEMPTS = 5` failed attempts the event becomes
   `DEAD_LETTER`. Retrying deterministic errors is doc-faithful: the
   docs do not distinguish between failure types. It also recovers
   cases the merchant can fix, such as adding a missing location
   mapping.
8. **Stale `RECEIVED` sweep.** `retry_failed_events` also re-enqueues
   `RECEIVED` events whose `received_at` is more than 10 minutes old.
   This covers the case where the original on-commit enqueue was lost
   (for example, the broker was down), so an event is never silently
   stranded (`Webhook-Specification.md` "never silently dropped").
9. **Cross-tenant sweep without bypassing RLS.** `retry_failed_events`
   takes no `merchant_id`. It reads only `Merchant`, which is global
   and not RLS-protected. For each merchant it enters
   `tenant_context` + `tenant_atomic` to find due events, and it
   enqueues `process_integration_event(merchant_id, event_id)` with an
   explicit `merchant_id`. No `BYPASSRLS` and no privileged
   connection. Marked `# ponytail:` (O(merchants) scan per tick;
   upgrade path: a narrow, audited SQL function returning due
   `(merchant_id, event_id)` pairs).
10. **`/integrations` roles.** The API spec states no roles for these
    endpoints. Every `/integrations` endpoint is **OWNER/ADMIN only**:
    integrations are merchant-level settings, as with `/team-members`.
    MANAGER and VIEWER get `403`.
11. **`POST /integrations/{provider}/connect` in Phase 04** is the
    generic credential-based path. It validates the provider, stores
    Fernet-encrypted `credentials` and `config_json`, and sets
    `status = CONNECTED`. There is no provider handshake. Phase 06
    adds the provider-specific connection logic. The provider must be
    a `Data-Dictionary.md` enum value **and** have a registered
    adapter, because an integration whose events can never be
    processed must not be connectable. In production this returns
    `422` for every provider until Phase 06 registers one.
12. **Disconnect** (`DELETE /integrations/{id}`), in one transaction
    under the `Integration` row lock:
    - sets `status = DISCONNECTED`;
    - sets every mapping `is_active = False` (mappings are kept);
    - clears `credentials_encrypted`, so an unneeded secret is not
      kept;
    - cancels the integration's pending events (Decision 18).

    **Disconnecting an integration stops processing of its pending
    events.** Reconnecting is a new `connect`. `record_event` rejects a
    `DISCONNECTED` integration.
13. **Audit.** Only `integration.connected` and
    `integration.disconnected` are audited, which is exactly
    `Audit-Logging.md` §"What Gets Logged". Mapping and `config_json`
    changes are not audited. Unlike team location assignment, they
    grant no user any data access.
14. **`Transaction` ownership.** `Database-Design.md` labels
    `Transaction` "LOCATION", but `Data-Dictionary.md` (the field-level
    authority) gives it a non-null `merchant_id`. It uses a direct
    `merchant_id` with `rls_direct`, which CLAUDE.md permits. The
    service asserts
    `Transaction.merchant_id == Location.merchant_id ==
    Customer.merchant_id`.
15. **Provider enum values stay lowercase** (`shopify`, `webhook`,
    `csv`, …), exactly as `Data-Dictionary.md` §Integration lists them.
    They are provider identifiers, not workflow statuses. `status`
    enums are `UPPER_SNAKE_CASE`.
16. **Refund/void ingestion is out of scope.** No normalized refund
    event is documented. Phase 04 creates only `COMPLETED`
    transactions. `REFUNDED`/`VOIDED` ingestion must be designed with
    the first adapter that emits refunds (Phase 06), before Phase 11
    depends on it.
17. **A sale without a customer phone is still a sale.** A
    `Transaction` represents the sale. Customer and review eligibility
    are a separate concern.
    - `Transaction.customer` is **nullable**.
    - `SaleCreated.customer_phone` is optional (`str | None`).
    - A sale with no phone is normalized and recorded as a `COMPLETED`
      `Transaction` with `customer = NULL`, and the event ends
      `PROCESSED`. It is never retried or dead-lettered for that reason.
    - No `Customer` is created without a phone. `Customer.phone` stays
      non-null, and `UNIQUE(merchant_id, phone)` stays.
    - A phone that **is** supplied must be valid E.164, exactly as
      before. An invalid supplied phone is a validation failure
      (`FAILED`, `INVALID_PHONE`), and it is never dropped silently to
      turn the sale into a phoneless one.
    - A blank or whitespace-only phone is treated as missing. That is
      the only normalization applied.
    - Phase 10 eligibility excludes transactions without a customer.
      `Business-Rules.md` §2 rule 1 and `Campaign-Engine.md` rule 1 are
      updated to say so.
18. **`CANCELLED` event status.** `IntegrationEvent.status` is
    `RECEIVED, PROCESSED, FAILED, DEAD_LETTER, CANCELLED`.
    - `disconnect_integration` transitions that integration's
      `RECEIVED` and `FAILED` events to `CANCELLED`, in the same
      transaction as the status change. `PROCESSED`, `DEAD_LETTER` and
      already-`CANCELLED` events are unchanged. Rows are never deleted.
    - `CANCELLED` is terminal. `process_event` treats it as a no-op.
      `retry_failed_events`, including the stale-`RECEIVED` sweep, never
      selects it.
    - Race guard: an event inserted concurrently with a disconnect can
      commit as `RECEIVED` after the cancellation `UPDATE` ran. A
      plain FK insert does not conflict with the integration's
      status-update lock. To cover that, `process_event` re-checks the
      integration under the event lock. If the integration is
      `DISCONNECTED`, it sets the event to `CANCELLED` and processes
      nothing. Events are never processed after a disconnect, and
      event inserts are not serialized on the `Integration` row.
    - Cancellation leaves `error_code`, `error_message` and
      `attempt_count` untouched, so a prior failure's diagnosis is kept.
19. **Safe error storage.** `IntegrationEvent` stores
    `error_code` (a stable machine-readable value) and `error_message`
    (a controlled human-readable message). Both come **only** from a
    fixed code→message table in `events/services.py` (`SAFE_ERRORS`).
    - Neither field is ever built from `str(exc)`, payload values,
      phone numbers, credentials, tokens or request bodies.
    - Known failures map to explicit codes (see Services).
    - Any other exception becomes `PROCESSING_ERROR` /
      `"Event processing failed"`.
    - Unexpected exceptions are logged with `event_id`, `merchant_id`,
      the exception class and the stack frames only
      (`traceback.format_tb`). The exception **message** is omitted,
      because driver messages can embed row values; for example, a
      unique-violation `DETAIL` shows the key values. The log never
      contains the payload, a phone, credentials or tokens.
20. **`Integration` row locking, and why it is `FOR NO KEY UPDATE`.**
    `replace_location_mappings`, `add_location_mapping` (for the same
    `external_location_id` uniqueness invariant) and
    `disconnect_integration` each begin with
    `Integration.objects.select_for_update(no_key=True).get(pk=...)`
    inside `tenant_atomic()`. Only after that do they read, validate,
    upsert, delete or cancel anything.
    - Concurrent replacements (or an add racing a replace, or either
      racing a disconnect) for the same integration are serialized by
      the row lock.
    - Validation of the complete requested set happens before any
      write. A validation failure leaves the previous set unchanged.
    - **`no_key=True` (`FOR NO KEY UPDATE`) rather than a plain
      `select_for_update()` (`FOR UPDATE`)**: `FOR UPDATE` conflicts
      with the `FOR KEY SHARE` locks PostgreSQL takes on the
      `Integration` row during FK inserts into `IntegrationEvent` and
      `Transaction`, which can deadlock a disconnect against a
      concurrent `process_event`: disconnect holds the `Integration`
      lock and waits for an event row, while `process_event` holds
      that event row and needs `FOR KEY SHARE` on the same
      `Integration` to insert the `Transaction`. `FOR NO KEY UPDATE`
      still serializes the `Integration` mutations that need
      coordination (it conflicts with itself and with `FOR UPDATE`),
      while not conflicting with `FOR KEY SHARE`. It is sufficient
      because none of these operations changes the `Integration`
      primary key.
    - The full lock inventory and lock order are documented in the
      Phase 04 implementation plan, and a concurrency test covers the
      serialization.

### Open item carried to Phase 06 (not built here)
An inbound webhook must identify its `Integration` **before** any
tenant context exists (for example, from `/webhooks/generic/{integration_id}`
or a Shopify shop-domain header). `integrations_integration` is
RLS-protected, so that lookup needs an RLS-compatible design, most
likely a narrow SELECT policy or function in the style of the
`self_membership` precedent. That design needs the same kind of user
sign-off. Phase 04's `record_event` therefore requires the caller to
have already entered `tenant_context(integration.merchant_id)`.

## Source docs
- `docs/ROADMAP.md`: §"04 — Event inbox & ingestion core", §"06 —
  Priority integrations" (boundary), §"16", §"15"
- `docs/FINAL-ARCHITECTURE-REVIEW.md`: §1 (integration scope and
  ownership), §8 (RLS)
- `docs/02-architecture/Architecture.md`: §"Three Logical Planes",
  §"Foundational Decisions" 1, 6
- `docs/02-architecture/SAD.md`: §2, §3, §4, §5 (queues, Beat
  `retry_failed_events` every 5 min), §8 (failure handling)
- `docs/02-architecture/Multi-Tenancy.md`: §Layer 1, §Layer 2, §"No
  Standing Privileged Role", §"`IntegrationEvent` — merchant identified
  before creation", §"Required Tests" 1–5
- `docs/02-architecture/Security-Architecture.md`: §Threat Model
  (cross-tenant ID collision on ingestion, duplicate/replayed webhooks)
- `docs/03-database/Data-Dictionary.md`: §Integration,
  §IntegrationLocationMapping, §IntegrationEvent, §Customer,
  §Transaction
- `docs/03-database/Database-Design.md`: §Integration,
  §IntegrationLocationMapping, §IntegrationEvent (indexes, invariant),
  §Customer, §Transaction (unique, index, `RESTRICT`), §"Design
  Principles"
- `docs/03-database/ERD.md`: Integration/Event/Customer/Transaction
  lines
- `docs/04-api/API-Specification.md`: preamble, §Transactions,
  §Integrations, §"General Conventions"
- `docs/04-api/Webhook-Specification.md`: pipeline, §Idempotency,
  §"Event Statuses", §"Retries & Failure Handling", §"Normalized Event
  Schema (`SaleCreated`)", §"Security Notes"
- `docs/04-api/Authentication.md`: §1 (session + CSRF), §3 (webhook
  signature in the adapter, fail closed)
- `docs/05-integrations/Integration-Architecture.md`: all sections
- `docs/06-automation/Event-Processing.md`: §Pipeline, §"Failure
  Handling", §"Idempotency Guarantees", §"Concurrency Considerations"
- `docs/01-product/Feature-Scope.md`: V1 integrations scope
- `docs/01-product/Business-Rules.md`: §2 rule 1 (E.164), §4 (refunds,
  boundary only)
- `docs/09-security/Audit-Logging.md`: "Integration connected /
  disconnected"
- `docs/09-security/Security-Controls.md`: §Encryption & Secrets
  (`credentials_encrypted`), §"Rate Limiting & Abuse Prevention"
  (`schemas.py` input validation), §Row-Level Security
- `docs/09-security/Privacy-Data-Retention.md`: §"What Is Collected"
  (raw payloads), §"Logging Rules"
- `docs/10-development/Coding-Standards.md`: §1–§7
- `docs/10-development/Testing-Strategy.md`: §"Tenant Isolation",
  §"Duplicate Webhook Event", §"Cross-Merchant Event Isolation",
  §"Duplicate External Event ID Across Integrations", §"Contract Tests
  for Adapters", §"Final Consistency Tests Added" (Multi-location
  integrations, RLS)
- `.claude/specs/03-locations-manager-assignment.md`:
  `accessible_locations` reuse rule, the `_assert_same_merchant`
  pattern, the `CursorPagination` pattern
- `.claude/specs/01-tenant-core.md`: `tenant_task`, `tenant_atomic`,
  `rls_direct`

## Depends on
- Phase 00, Project scaffold (Done): Celery app, queues, pytest eager
  mode
- Phase 01, Core tenancy & RLS (Done): `BaseModel`,
  `TenantScopedManager`, `tenant_context`, `tenant_atomic`,
  `tenant_task`, `core.rls.rls_direct`, `core.crypto`,
  `TenantMiddleware`
- Phase 02, Accounts, roles & audit log (Done): `Merchant`,
  `TeamMember`, `IsMerchantMember`, `IsOwnerOrAdmin`, session auth +
  CSRF, `auditlog.services.record`, `core.api.exception_handler`
- Phase 03, Locations (Done): `Location`,
  `locations.services.accessible_locations`, `core.pagination.CursorPagination`

## Roadmap Phase
- Phase: 04 — Event inbox & ingestion core
- Completes entire phase: Yes
- It covers all six ROADMAP §04 bullets: `Integration` +
  `IntegrationLocationMapping`; the `IntegrationEvent` inbox and its
  statuses; `integrations/core` (`BaseAdapter`, `SaleCreated`,
  `schemas.py`, `registry.py`); `Customer`, `Transaction`,
  normalization and location resolution; `retry_failed_events`;
  `/integrations` and `/transactions`. Provider-specific `connect`
  handshakes are explicitly provider-specific (`API-Specification.md`
  §Integrations), so they belong to the adapter phases.
- If this branch proves too large, it splits naturally into two specs:
  (a) models + inbox + pipeline + sweeper, and (b) the `/integrations`
  and `/transactions` API.

## Locked decisions touched
- Shared schema, `merchant_id` scoping (`Multi-Tenancy.md` §Model) —
  DEPENDS ON
- `TenantScopedManager` as primary application-layer scoping
  (`Multi-Tenancy.md` §Layer 1) — DEPENDS ON
- Transaction-local `SET LOCAL app.current_merchant_id`, RLS on every
  tenant table (`FINAL-ARCHITECTURE-REVIEW.md` §8) — DEPENDS ON (5 new
  tables, all `rls_direct`)
- No standing privileged role / no `BYPASSRLS` (`Multi-Tenancy.md` §No
  Standing Privileged Role) — DEPENDS ON (the sweeper iterates
  merchants rather than bypassing RLS)
- `Integration` merchant-level + `IntegrationLocationMapping` as the
  only integration↔location source of truth
  (`FINAL-ARCHITECTURE-REVIEW.md` §1; `Integration-Architecture.md`) —
  DEPENDS ON (built as documented)
- `IntegrationEvent.merchant_id` resolved from `Integration` before
  creation, never null; `UNIQUE(integration_id, external_event_id)`
  (`FINAL-ARCHITECTURE-REVIEW.md` §1; `Multi-Tenancy.md`) — DEPENDS ON
- `UNIQUE(location_id, external_transaction_id)` on `Transaction`
  (`Event-Processing.md` §Idempotency Guarantees) — DEPENDS ON
- Adapter pattern (`BaseAdapter` verify/parse/normalize; registry keyed
  on `Integration.provider`) (`Integration-Architecture.md`, `SAD.md`
  §4) — DEPENDS ON (the `parse` argument type is clarified to the
  stored JSON payload, Decision 2; the contract is unchanged)
- Celery merchant context: explicit `merchant_id`, `SET LOCAL` per
  transaction (`Multi-Tenancy.md` §Layer 1/2) — DEPENDS ON
- Queues `events`/`whatsapp`/`google_sync`/`default`; Beat
  `retry_failed_events` every 5 min (`SAD.md` §5) — DEPENDS ON
- Poll-based dispatch, no broker `eta`/`countdown` (`Architecture.md`
  §4) — NO CHANGE (no sends in this phase; the sweeper is Beat-driven,
  and backoff is computed from `updated_at`, not `countdown`)
- One review request per transaction / `SELECT FOR UPDATE` on
  `Transaction` (`FINAL-ARCHITECTURE-REVIEW.md` §2) — NO CHANGE (Phase
  10)
- Three authentication mechanisms, never mixed; webhook signatures
  verified in the adapter, fail closed (`Authentication.md`) — DEPENDS
  ON (`BaseAdapter.verify` contract; no receivers yet)
- Fernet encryption of integration credentials (`Security-Controls.md`)
  — DEPENDS ON
- Externally exposed IDs are UUIDs (`Security-Architecture.md`) —
  DEPENDS ON
- Retention: raw payloads 90 days (`FINAL-ARCHITECTURE-REVIEW.md` §9) —
  NO CHANGE (purge is Phase 15)
- Django Admin, no bespoke admin app (`Architecture.md` §6) — NO CHANGE

None. No line is a LOCKED DECISION CHANGE.

## Django apps
- `integrations`: **created**. `Integration`,
  `IntegrationLocationMapping`, services, serializers, views, URLs.
  Contains the plain subpackage `integrations/core/` (`adapters.py`,
  `events.py`, `schemas.py`, `registry.py`, which is not a Django app).
  No provider subpackages (`shopify/`, `webhook/`, …) yet.
- `events`: **created**. `IntegrationEvent`, the processing pipeline
  services, and Celery tasks.
- `customers`: **created**. `Customer` and its get-or-create service.
- `transactions`: **created**. `Transaction`, the sale-recording
  service, and `/transactions`.
- `core`, `accounts`, `locations`, `auditlog`: **used** only.
  `accessible_locations`, `IsOwnerOrAdmin`, `record()`, `crypto`,
  `rls_direct`, `CursorPagination`, `tenant_task`. Their code does not
  change.
- `config`: **touched**. `INSTALLED_APPS`, URL includes,
  `CELERY_BEAT_SCHEDULE`.

## Models & database changes
Every model is built on `core.BaseModel` (UUID `id`, `created_at`,
`updated_at`) and uses `objects = TenantScopedManager()` with the
default `tenant_field = "merchant_id"`. Every table gets
`rls_direct(<table>)` in its own migration (`tenant_isolation`,
`ENABLE` + `FORCE`).

### `integrations.Integration` (new; MERCHANT)
| Field | Django type | Null | Notes |
|---|---|---|---|
| `merchant` | `ForeignKey("accounts.Merchant", PROTECT, related_name="integrations")` | No | |
| `provider` | `CharField(max_length=32, choices=Provider)` | No | `shopify, woocommerce, petpooja, gofrugal, webhook, csv, zapier, make` (lowercase, per Data-Dictionary) |
| `status` | `CharField(max_length=16, choices=Status, default=CONNECTED)` | No | `CONNECTED, ERROR, DISCONNECTED`. `ERROR` exists but nothing sets it in Phase 04 |
| `credentials_encrypted` | `TextField(null=True, blank=True)` | Yes | Fernet token of the JSON-serialized credentials (`core.crypto`). Never plaintext, never returned by the API, never logged |
| `config_json` | `JSONField(null=True, blank=True)` | Yes | provider settings / generic field mapping |

No unique constraint: a merchant may connect two stores of the same
provider.

### `integrations.IntegrationLocationMapping` (new; MERCHANT)
| Field | Django type | Null | Notes |
|---|---|---|---|
| `merchant` | `ForeignKey(Merchant, PROTECT, related_name="+")` | No | must equal `integration.merchant_id` and `location.merchant_id` (service invariant) |
| `integration` | `ForeignKey(Integration, PROTECT, related_name="location_mappings")` | No | |
| `location` | `ForeignKey("locations.Location", PROTECT, related_name="integration_mappings")` | No | |
| `config_json` | `JSONField(null=True, blank=True)` | Yes | optional; `external_location_id` (string) is the resolution key (Decision 5) |
| `is_active` | `BooleanField(default=True)` | No | |

- `UniqueConstraint(["integration", "location"],
  name="uniq_integrationlocationmapping_integration_location")`

### `events.IntegrationEvent` (new; MERCHANT, direct, non-null)
| Field | Django type | Null | Notes |
|---|---|---|---|
| `merchant` | `ForeignKey(Merchant, PROTECT, related_name="+")` | **No** | set from `integration.merchant_id` at creation, never null |
| `integration` | `ForeignKey("integrations.Integration", PROTECT, related_name="events")` | **No** | |
| `source` | `CharField(max_length=32)` | No | denormalized from `Integration.provider` |
| `external_event_id` | `CharField(max_length=255)` | No | |
| `location` | `ForeignKey("locations.Location", PROTECT, null=True, related_name="+")` | Yes | set when location resolution succeeds |
| `payload` | `JSONField()` | No | raw inbound payload (JSON-decoded); never logged; purged at 90 days in Phase 15 |
| `status` | `CharField(max_length=16, choices=Status, default=RECEIVED)` | No | `RECEIVED, PROCESSED, FAILED, DEAD_LETTER, CANCELLED` (Decision 18) |
| `received_at` | `DateTimeField(null=True)` | Yes (per dictionary) | always populated at creation with `timezone.now()` |
| `processed_at` | `DateTimeField(null=True, blank=True)` | Yes | set on `PROCESSED` |
| `attempt_count` | `PositiveIntegerField(default=0)` | No | **addition** (Decision 6): failed processing attempts |
| `error_code` | `CharField(max_length=64, null=True, blank=True)` | Yes | **addition** (Decisions 6, 19). Stable code of the last failure, from `SAFE_ERRORS` only |
| `error_message` | `CharField(max_length=255, null=True, blank=True)` | Yes | **addition** (Decisions 6, 19). Controlled message for `error_code`, from `SAFE_ERRORS` only; never exception text, payload, phone, credentials or tokens |

- `UniqueConstraint(["integration", "external_event_id"],
  name="uniq_integrationevent_integration_external_event")`. This is
  the idempotency key, **not** `(source, external_event_id)`.
- `Index(["status", "received_at"], name="integrationevent_status_recv_idx")`
  for the retry/stale sweeps.

### `customers.Customer` (new; MERCHANT)
| Field | Django type | Null | Notes |
|---|---|---|---|
| `merchant` | `ForeignKey(Merchant, PROTECT, related_name="customers")` | No | |
| `phone` | `CharField(max_length=16)` | No | E.164 (validated before insert). A `Customer` exists only when a sale supplied a phone (Decision 17) |
| `name` | `CharField(max_length=255, null=True, blank=True)` | Yes | set on create only |
| `first_seen_at` / `last_seen_at` | `DateTimeField(null=True, blank=True)` | Yes | min/max of recorded `occurred_at` |
| `total_transactions` | `PositiveIntegerField(default=0)` | No | incremented only when a `Transaction` is genuinely created |
| `opted_out` | `BooleanField(default=False)` | No | never set in Phase 04 (Phase 08) |
| `opted_out_at` | `DateTimeField(null=True, blank=True)` | Yes | |

- `UniqueConstraint(["merchant", "phone"], name="uniq_customer_merchant_phone")`

### `transactions.Transaction` (new; direct `merchant_id`, Decision 14)
| Field | Django type | Null | Notes |
|---|---|---|---|
| `merchant` | `ForeignKey(Merchant, RESTRICT, related_name="+")` | No | `RESTRICT` per Database-Design (financial record) |
| `location` | `ForeignKey("locations.Location", PROTECT, related_name="transactions")` | No | |
| `customer` | `ForeignKey("customers.Customer", PROTECT, null=True, blank=True, related_name="transactions")` | **Yes** | `NULL` when the sale supplied no phone (Decision 17); such transactions are never review-eligible (Phase 10) |
| `integration` | `ForeignKey("integrations.Integration", PROTECT, null=True, related_name="transactions")` | Yes | |
| `external_transaction_id` | `CharField(max_length=255)` | No | |
| `amount` | `DecimalField(max_digits=12, decimal_places=2)` | No | |
| `currency` | `CharField(max_length=3)` | No | ISO 4217 code, `^[A-Z]{3}$` |
| `payment_method` | `CharField(max_length=32, null=True, blank=True)` | Yes | attribute only |
| `status` | `CharField(max_length=16, choices=Status, default=COMPLETED)` | No | `COMPLETED, REFUNDED, VOIDED`; only `COMPLETED` is written in Phase 04 |
| `occurred_at` | `DateTimeField()` | No | |

- `UniqueConstraint(["location", "external_transaction_id"],
  name="uniq_transaction_location_external_txn")`
- `Index(["merchant", "occurred_at"], name="transaction_merchant_occurred_idx")`
- There is no FK to `IntegrationEvent`. `Data-Dictionary.md` has none;
  the ERD's `0..1───1` line is informational.

### Migrations
One logical change each. Nothing is dropped:
1. `integrations/0001_initial.py`: `Integration`,
   `IntegrationLocationMapping` + unique constraint
2. `integrations/0002_integrations_rls.py`:
   `rls_direct("integrations_integration")`,
   `rls_direct("integrations_integrationlocationmapping")`
3. `customers/0001_initial.py`: `Customer` + unique constraint
4. `customers/0002_customer_rls.py`: `rls_direct("customers_customer")`
5. `events/0001_initial.py`: `IntegrationEvent` + unique constraint +
   index
6. `events/0002_integrationevent_rls.py`:
   `rls_direct("events_integrationevent")`
7. `transactions/0001_initial.py`: `Transaction` + unique constraint +
   index
8. `transactions/0002_transaction_rls.py`:
   `rls_direct("transactions_transaction")`

### Idempotency and concurrency
- Every unique key is enforced by the database and handled
  **constraint-then-catch-`IntegrityError`** inside a savepoint. There
  is never a check-then-insert. The keys are:
  `(integration_id, external_event_id)`,
  `(location_id, external_transaction_id)`, `(merchant_id, phone)`,
  `(integration_id, location_id)`.
- Processing locks the `IntegrationEvent` row
  (`select_for_update()`) for the whole attempt. A duplicate enqueue
  or a concurrent retry of the same event is serialized. The second
  worker sees `PROCESSED` or `CANCELLED` (no-op), or sees `FAILED` that
  is not yet due (skips).
- Disconnect vs. processing:
  - `disconnect_integration` locks the `Integration` row, then runs
    `UPDATE … SET status = CANCELLED WHERE integration_id = … AND
    status IN (RECEIVED, FAILED)`.
  - That `UPDATE` waits on any event row currently locked by
    `process_event`. PostgreSQL re-evaluates the `WHERE` clause after
    the wait, so an event that just became `PROCESSED` is left alone.
  - `process_event` never locks the `Integration`, so there is no lock
    cycle.
  - The late-insert race is covered by `process_event`'s own
    integration re-check (Decision 18).
- Mapping writes are serialized per integration by the `Integration`
  row lock (Decision 20).
- Counter updates (`total_transactions`, `first/last_seen_at`) run in
  the same transaction as the `Transaction` insert, using `F()` /
  `Least` / `Greatest`, and only when the insert actually created the
  row **and** the sale has a customer.

## API endpoints
All endpoints are under `/api/v1/`, with session auth + CSRF on writes.
`merchant_id` always comes from the session and is never taken from
request data. Any `merchant_id`, `id` or `status` in a request body is
ignored. Errors use `{ "error": { "code", "message", "field_errors"? } }`.
Cross-merchant ids return `404`, never `403`.

Integration body:
`{ id, provider, status, config_json, has_credentials, mappings: [Mapping], created_at, updated_at }`.
Mapping body: `{ id, location_id, config_json, is_active }`. There is
never a `credentials*` field.

- `POST /integrations/{provider}/connect`: `{ credentials?: object,
  config_json?: object }`. Session. OWNER, ADMIN.
  - Returns `201` with the Integration body (`status: CONNECTED`, no
    mappings).
  - `422` if `provider` is not an enum value or has no registered
    adapter (Decision 11). `422` if `credentials` or `config_json` is
    not a JSON object.
  - `403` for MANAGER and VIEWER.
- `GET /integrations`: lists every merchant integration, including
  `DISCONNECTED` ones, with embedded mapping summaries. Session. OWNER,
  ADMIN.
  - Returns `200 { results: [Integration], next_cursor }`, with shared
    cursor pagination ordered by `created_at`.
  - `403` for MANAGER and VIEWER.
- `PATCH /integrations/{id}`: `{ config_json }`. Replaces
  merchant-level `config_json`. Session. OWNER, ADMIN.
  - Returns `200` with the Integration body.
  - `404` for a cross-merchant or missing id. `422` for a non-object.
- `DELETE /integrations/{id}`: disconnect (Decision 12). Session.
  OWNER, ADMIN.
  - Atomically cancels the integration's pending `RECEIVED`/`FAILED`
    events (Decision 18).
  - Returns `204`. It is idempotent: an already-`DISCONNECTED`
    integration returns `204` again, with no second audit row.
  - `404` for a cross-merchant or missing id.
- `POST /integrations/{id}/locations`: `{ location_id, config_json?,
  is_active? }`. Session. OWNER, ADMIN.
  - Returns `201` with the Mapping body.
  - `404` for a cross-merchant or missing integration.
  - `409 mapping_exists` for a duplicate `(integration_id,
    location_id)`.
  - `422` for an unknown or cross-merchant `location_id`, with the same
    body for both. `422` for a malformed UUID.
  - `422` for an `external_location_id` that is not a string, or that
    duplicates another **active** mapping of the same integration.
- `PUT /integrations/{id}/locations`: `{ mappings: [{ location_id,
  config_json?, is_active? }] }`. Session. OWNER, ADMIN.
  - Atomically replaces the integration's mapping set, under a
    `select_for_update(no_key=True)` lock on the `Integration` row
    (Decision 20).
    The whole requested set is validated first. Listed locations are
    then upserted, and unlisted mappings are deleted (nothing
    references a mapping row). Concurrent `PUT`s for the same
    integration are serialized.
  - Returns `200` with the Integration body.
  - Errors are as for `POST`. A duplicate `location_id` within the
    request returns `422`.
- `GET /transactions`: session. Any role.
  - A MANAGER sees only transactions at locations returned by
    `locations.services.accessible_locations(actor)`, which is reused,
    not re-implemented.
  - Filters: `location_id` (uuid), `date_from`/`date_to` (ISO date,
    inclusive, on `occurred_at` in UTC), `status`
    (`COMPLETED|REFUNDED|VOIDED`). A malformed filter returns `422`.
    An inaccessible `location_id` returns empty results.
  - Returns `200 { results: [Transaction], next_cursor }`, with cursor
    pagination ordered by `-created_at`.
  - Transaction body: `{ id, location_id, integration_id,
    external_transaction_id, amount (string), currency,
    payment_method, status, occurred_at, created_at, customer: { id,
    name, phone } | null }`. `customer` is `null` for a sale recorded
    without a phone (Decision 17).
- `GET /transactions/{id}`: session. Any role.
  - Returns `200` with the Transaction body.
  - `404` for a missing or cross-merchant id, or (for a MANAGER) a
    transaction at an unassigned location.
  - There is no `campaign_execution` field until Phase 10.

No inbound webhook endpoint, and no `/sales` (Phase 06).

## Services & background tasks

### `integrations/core/events.py`: `SaleCreated`
A frozen `@dataclass`. It is the only shape downstream code sees.
- `source: str`
- `external_transaction_id: str`: non-blank, ≤ 255
- `customer_phone: str | None`: E.164 when present. Blank or
  whitespace-only becomes `None`. An invalid non-blank value raises
  `PayloadValidationError("INVALID_PHONE")` (Decision 17)
- `customer_name: str | None`: ≤ 255 characters; ignored when
  `customer_phone` is `None`
- `amount: Decimal`: ≥ 0, and must fit `Decimal(12, 2)` (at most 2
  decimal places and 10 integer digits)
- `currency: str`: `^[A-Z]{3}$`
- `payment_method: str | None`: ≤ 32 characters
- `occurred_at: datetime`: timezone-aware
- `external_location_id: str | None`: the provider's location id
  (Decision 4)

`__post_init__` validates through `schemas.py` and raises
`PayloadValidationError`. It has no `merchant_id` or `location_id`
field.

Field bounds (`customer_name`, `payment_method`, and `amount`'s
precision) are validated here, with the existing `PAYLOAD_INVALID`
code, so an over-long or over-precise value never reaches the database
as an error that would become `PROCESSING_ERROR` and burn five
retries. `SAFE_ERRORS` is unchanged.

### `integrations/core/schemas.py`
- `E164_RE = r"^\+[1-9]\d{7,14}$"`; `validate_e164(phone) -> str`;
  `validate_currency(code) -> str`.
- `PayloadValidationError(ReviewFlowError)`: constructed with a
  **code** only, `PayloadValidationError(code)`. The code is one of
  `INVALID_PHONE`, `INVALID_CURRENCY`, `INVALID_AMOUNT`,
  `INVALID_OCCURRED_AT`, `INVALID_EXTERNAL_TRANSACTION_ID`,
  `PAYLOAD_INVALID` (a generic adapter `parse`/`normalize` failure).
  It carries no free-text message and never holds the offending value.
  An unrecognized code is stored as `PAYLOAD_INVALID`.
- Per-provider raw-payload schemas are added here by each adapter phase.

### `integrations/core/adapters.py`: `BaseAdapter(ABC)`
- `__init__(self, integration: Integration)`: the adapter reads its
  own secret via `core.crypto.decrypt` when needed.
- `@abstractmethod verify(self, request) -> bool`: signature/secret
  check. It must fail closed. It is unused in Phase 04.
- `@abstractmethod parse(self, payload: dict) -> dict`: provider
  parsing and validation (Decision 2). Raises `PayloadValidationError`.
- `@abstractmethod normalize(self, parsed: dict) -> SaleCreated`
- `get_webhook_id` and similar hooks are not added. Phase 06 adds
  whatever a receiver needs.

### `integrations/core/registry.py`
- `ADAPTERS: dict[str, type[BaseAdapter]] = {}`. It is empty in
  Phase 04.
- `get_adapter(integration: Integration) -> BaseAdapter`: keyed on
  `integration.provider`, never an event `source` string. Raises
  `AdapterNotFound(ReviewFlowError)`.
- `is_registered(provider: str) -> bool`

### `integrations/exceptions.py`
- `IntegrationNotFound` (`404`, `not_found`)
- `MappingExists` (`409`, `mapping_exists`)
- `IntegrationDisconnected(ReviewFlowError)`: no HTTP mapping; raised
  by `record_event`
- `LocationUnresolved(ReviewFlowError)`: no HTTP mapping; a processing
  failure

### `integrations/services.py`
Every function requires a tenant context and runs inside
`tenant_atomic()`.
- `AUDIT_CONNECTED = "integration.connected"`,
  `AUDIT_DISCONNECTED = "integration.disconnected"`
- `connect_integration(*, actor: TeamMember, provider: str,
  credentials: dict | None, config_json: dict | None) -> Integration`:
  - Raises `ValidationError` (`422`) for an unknown or unregistered
    provider.
  - Stores `encrypt(json.dumps(credentials))` when credentials are
    given.
  - Calls `record(AUDIT_CONNECTED, actor=actor.user,
    target=integration, metadata={"provider": provider})`. Credentials
    are never included in metadata.
- `get_integration(integration_id) -> Integration`: raises
  `IntegrationNotFound`.
- `list_integrations() -> QuerySet[Integration]`: prefetches mappings.
- `update_integration_config(integration, *, config_json: dict) ->
  Integration`
- `disconnect_integration(*, actor: TeamMember, integration) -> None`:
  one `tenant_atomic()` block that runs these steps:
  1. Re-reads the integration with `select_for_update(no_key=True)`
     (Decision 20).
  2. If it is already `DISCONNECTED`, returns without changes. This
     makes the call idempotent.
  3. Otherwise sets `DISCONNECTED`, sets `credentials_encrypted=None`
     and sets every mapping `is_active=False`.
  4. Runs `IntegrationEvent.objects.filter(integration=integration,
     status__in=[RECEIVED, FAILED]).update(status=CANCELLED)`
     (Decision 18).
  5. Writes the `integration.disconnected` audit row.

  All of this commits or rolls back together.
- `add_location_mapping(integration, *, location_id,
  config_json=None, is_active=True) -> IntegrationLocationMapping`:
  - First locks the `Integration` row with
    `select_for_update(no_key=True)` (Decision 20).
  - Loads the location through the tenant-scoped manager. Unknown or
    cross-merchant raises `ValidationError` (`422`).
  - Runs `_assert_same_merchant`, then `_validate_external_location_id`.
  - Inserts inside a savepoint. `IntegrityError` raises
    `MappingExists`.
- `replace_location_mappings(integration, *, mappings: list[dict]) ->
  Integration`: one `tenant_atomic()` block:
  1. `Integration.objects.select_for_update(no_key=True).get(pk=integration.pk)`
     (Decision 20).
  2. Validates the **complete** requested set: every location
     resolvable, `_assert_same_merchant` for each, no duplicate
     `location_id` in the request, and `external_location_id`
     type/uniqueness across the resulting active set. Any failure
     raises before any write, so the previous set is unchanged.
  3. Upserts the listed mappings.
  4. Deletes the unlisted mappings.
- `_assert_same_merchant(integration, location) -> None`: **the
  documented three-way invariant, checked explicitly**:
  `location.merchant_id == integration.merchant_id ==
  get_current_merchant_id()`, before any insert. It must not rely on
  RLS or scoped reads having filtered rows already. It has its own
  unit test using a mismatched unsaved `Location`, the same pattern as
  Phase 03's `_assert_same_merchant`.
- `_validate_external_location_id(integration, config_json,
  is_active, exclude_location_id=None) -> None`:
  - The value must be a string if present.
  - It must be unique among the integration's active mappings.
- `resolve_location(integration, sale: SaleCreated) -> Location`
  (Decision 5), using only the integration's **active** mappings:
  1. If there is exactly one active mapping and it has no
     `external_location_id`, return its location. This is the
     single-store provider case.
  2. Otherwise, if `sale.external_location_id` is set, return the
     location of the one active mapping whose
     `config_json["external_location_id"]` equals it.
  3. Otherwise raise `LocationUnresolved`. It never guesses and never
     falls back to "the first" mapping.

### `customers/services.py`
- `get_or_create_customer(*, phone: str, name: str | None) ->
  tuple[Customer, bool]`:
  - `merchant_id` comes from the tenant context.
  - Validates E.164 (defense in depth).
  - Handles `UNIQUE(merchant_id, phone)` races through a savepoint and
    `IntegrityError`, then re-reads.
  - `name` is set only on create.

### `transactions/exceptions.py`
- `TransactionNotFound` (`404`, `not_found`)

### `transactions/services.py`
- `record_sale(*, integration: Integration, location: Location, sale:
  SaleCreated) -> tuple[Transaction, bool]`:
  1. If `sale.customer_phone` is set, calls `get_or_create_customer`.
     Otherwise `customer = None`, and no `Customer` is created
     (Decision 17).
  2. Asserts `location.merchant_id == integration.merchant_id ==
     get_current_merchant_id()`, plus `customer.merchant_id` to the
     same value when there is a customer.
  3. Inserts the `Transaction` (`COMPLETED`, `customer` possibly
     `NULL`) in a savepoint. `IntegrityError` on
     `(location, external_transaction_id)` returns `(existing, False)`,
     a no-op.
  4. Only when a row was created **and** there is a customer, updates
     the customer: `total_transactions = F()+1`,
     `first_seen_at = Least(Coalesce(first_seen_at, occurred_at),
     occurred_at)`, `last_seen_at = Greatest(...)`.
  - A duplicate re-delivery of an earlier phoneless sale that now
    carries a phone is still a no-op. The first recorded version of a
    transaction wins, and this phase has no transaction-update path.
- `list_transactions(actor: TeamMember, *, location_id=None,
  date_from=None, date_to=None, status=None) -> QuerySet[Transaction]`:
  filters
  `location__in=accessible_locations(actor)`, then applies the filters,
  with `select_related("customer")`.
- `get_transaction(actor: TeamMember, transaction_id) -> Transaction`:
  same scoping. Raises `TransactionNotFound`.

### `events/services.py`
Constants: `MAX_EVENT_ATTEMPTS = 5`,
`RETRY_BASE = timedelta(minutes=5)`,
`STALE_RECEIVED_AFTER = timedelta(minutes=10)`.
- `record_event(*, integration: Integration, external_event_id: str,
  payload: dict) -> tuple[IntegrationEvent, bool]`: the inbox write,
  which Phase 06 receivers call after `verify()`.
  - Requires an active tenant context **equal to**
    `integration.merchant_id`, otherwise it raises
    `TenantContextError`. This makes `IntegrationEvent.merchant_id ==
    Integration.merchant_id` an enforced invariant.
  - Raises `IntegrationDisconnected` for a `DISCONNECTED` integration.
  - Inside `tenant_atomic()`, inserts in a savepoint with
    `merchant_id=integration.merchant_id`, `source=integration.provider`,
    `status=RECEIVED` and `received_at=now()`.
  - On `IntegrityError` it returns `(existing, False)`: no enqueue and
    no reprocessing.
  - On create it registers
    `transaction.on_commit(lambda: process_integration_event.delay(merchant_id,
    event.id))`.
- `SAFE_ERRORS: dict[str, str]` is the **only** source of stored error
  text (Decision 19):

  | `error_code` | `error_message` |
  |---|---|
  | `ADAPTER_NOT_FOUND` | "No adapter is registered for this integration's provider." |
  | `PAYLOAD_INVALID` | "The event payload failed validation." |
  | `INVALID_PHONE` | "The customer phone is not a valid E.164 number." |
  | `INVALID_CURRENCY` | "The currency is not a valid ISO 4217 code." |
  | `INVALID_AMOUNT` | "The amount is missing or negative." |
  | `INVALID_OCCURRED_AT` | "The sale time is missing or has no timezone." |
  | `INVALID_EXTERNAL_TRANSACTION_ID` | "The external transaction ID is missing or too long." |
  | `LOCATION_UNRESOLVED` | "The sale could not be mapped to a location." |
  | `PROCESSING_ERROR` | "Event processing failed" |

- `_safe_error(exc) -> tuple[str, str]`:
  - `AdapterNotFound` maps to `ADAPTER_NOT_FOUND`.
  - `PayloadValidationError` maps to its code if the code is in
    `SAFE_ERRORS`, otherwise to `PAYLOAD_INVALID`.
  - `LocationUnresolved` maps to `LOCATION_UNRESOLVED`.
  - Everything else, including other `ReviewFlowError`s and database
    errors, maps to `PROCESSING_ERROR`.
  - It never reads `str(exc)` or `exc.args`.
- `process_event(event_id) -> IntegrationEvent`: one processing
  attempt, inside `tenant_atomic()`:
  1. Locks the event:
     `event = IntegrationEvent.objects.select_for_update().get(pk=event_id)`.
     - If `status` is `PROCESSED`, `DEAD_LETTER` or `CANCELLED`, return
       without changes.
     - If `FAILED` and not `_is_due(event)`, return without changes.
  2. Re-checks the integration. If `event.integration.status ==
     DISCONNECTED`, set `status=CANCELLED`, leave the error fields and
     `attempt_count` untouched, and return (Decision 18 race guard).
  3. In a savepoint:
     - `adapter = get_adapter(event.integration)`
     - `sale = adapter.normalize(adapter.parse(event.payload))`
     - `location = resolve_location(event.integration, sale)`
     - `record_sale(...)`
  4. On success: `status=PROCESSED`, `location=location`,
     `processed_at=now()`, `error_code=None`, `error_message=None`.
     - A duplicate transaction still ends in `PROCESSED`
       (`Event-Processing.md`).
     - A sale with no phone ends in `PROCESSED` with a customer-less
       `Transaction` (Decision 17).
  5. On any `Exception`:
     - The savepoint rolls back, and `attempt_count += 1`.
     - `error_code, error_message = _safe_error(exc)`.
     - `status` becomes `DEAD_LETTER` if `attempt_count >=
       MAX_EVENT_ATTEMPTS`, otherwise `FAILED`.
     - The log records `event_id`, `merchant_id`,
       `type(exc).__name__`, the stored `error_code` and, for
       `PROCESSING_ERROR`, `traceback.format_tb(exc.__traceback__)`.
       It never records the exception message, the payload, a phone,
       credentials or tokens.
- `_is_due(event) -> bool`:
  `event.updated_at <= now() - RETRY_BASE * 2 ** (attempt_count - 1)`.
- `due_event_ids() -> list[uuid.UUID]`: runs for the current tenant.
  - Selects `FAILED` events that are due by backoff, plus `RECEIVED`
    events with `received_at < now() - STALE_RECEIVED_AFTER`
    (Decision 8). The filter is on explicit status values, so
    `CANCELLED`, `DEAD_LETTER` and `PROCESSED` are never selected.
  - Uses `select_for_update(skip_locked=True)`, so events currently
    being processed are skipped.
  - Backoff is applied in SQL where practical, or filtered in Python
    over the `(status, received_at)`-indexed candidate set.

### `events/tasks.py`
- `process_integration_event(merchant_id, event_id)`:
  `@shared_task(queue="events")` + `@tenant_task`, a thin wrapper
  around `services.process_event(event_id)`. `merchant_id` is explicit
  and never re-derived from the event (Decision 3).
- `retry_failed_events()`: `@shared_task(queue="events")`, and
  merchant-agnostic by design (Decision 9).
  - For each `Merchant` id (global table, no RLS), inside
    `tenant_context(mid)` + `tenant_atomic()`, calls `due_event_ids()`.
  - On commit, enqueues `process_integration_event.delay(mid, eid)`
    for each.
  - Has a `# ponytail:` comment naming the O(merchants) ceiling and
    the upgrade path.
  - Never schedules work with `eta`/`countdown`.

### Celery Beat (`config/settings.py`)
```python
CELERY_BEAT_SCHEDULE = {
    "retry-failed-events": {
        "task": "events.tasks.retry_failed_events",
        "schedule": 300.0,
        "options": {"queue": "events"},
    },
}
```

### Adapter/provider interfaces
- `BaseAdapter` is defined. No implementation ships. A test-only
  `FakeAdapter` in `events/tests/conftest.py` is registered with
  `monkeypatch.setitem(registry.ADAPTERS, "webhook", FakeAdapter)`.

## Admin
No admin changes. This follows the `accounts/admin.py` precedent: no
tenant-scoped model is registered, because `TenantScopedManager` needs
a tenant context. Cross-tenant admin, including dead-letter
`IntegrationEvent` review, is the Phase 16 audited privileged path.
When it is registered, `credentials_encrypted` and `payload` must never
be rendered in list views.

## Files to change
- `config/settings.py`: add `integrations`, `events`, `customers`,
  `transactions` to `INSTALLED_APPS`; add the `retry-failed-events`
  Beat entry
- `config/urls.py`: include `integrations.urls` and
  `transactions.urls` under `api/v1/`
- `docs/04-api/API-Specification.md`: §Integrations (roles, request and
  response bodies, errors, the Phase 04 generic `connect`);
  §Transactions (roles, MANAGER scoping, filters and formats, body
  including nullable `customer`, "CampaignExecution summary arrives
  with Phase 10")
- `docs/03-database/Data-Dictionary.md`: §IntegrationLocationMapping
  document the `config_json.external_location_id` resolution key

**Already updated during spec review** (Decisions 17–19; no further
change needed at implementation unless the implementation diverges):
- `docs/03-database/Data-Dictionary.md`: §IntegrationEvent (`CANCELLED`
  status, `attempt_count`, `error_code`, `error_message`),
  §Customer (exists only when a phone was supplied), §Transaction
  (`customer_id` nullable)
- `docs/03-database/Database-Design.md`: §IntegrationEvent (statuses,
  retry/error fields, safe-error rule), §Customer, §Transaction
- `docs/03-database/ERD.md`: `Customer 1───* Transaction` (optional
  customer)
- `docs/04-api/Webhook-Specification.md`: pipeline (conditional
  customer step), §"Event Statuses" (`CANCELLED`), §"Retries & Failure
  Handling" (safe error code and message), §"Normalized Event Schema"
  (optional customer)
- `docs/04-api/API-Specification.md`: `DELETE /integrations/{id}`
  cancels pending events
- `docs/06-automation/Event-Processing.md`: pipeline (conditional
  customer step), §"Failure Handling" (phoneless sale, disconnect, safe
  error storage)
- `docs/06-automation/Campaign-Engine.md` and
  `docs/01-product/Business-Rules.md`: eligibility rule 1 (the
  transaction has a `Customer`)
- `docs/01-product/User-Flows.md` §4: "Transaction (and Customer, when
  a phone is supplied) created"
- `docs/05-integrations/Integration-Architecture.md`: §"The Adapter
  Contract" `parse(self, payload: dict) -> dict`, plus the
  location-resolution rules under §"Integration-to-Location Mapping"
- `docs/02-architecture/SAD.md`: §4 adapter signature, to match
- `docs/06-automation/Event-Processing.md`: task signature
  `process_integration_event(merchant_id, event_id)`, the stale
  `RECEIVED` re-enqueue, and the retry cap/backoff values

## Files to create
- `integrations/__init__.py`, `apps.py`, `models.py`, `exceptions.py`,
  `services.py`, `serializers.py`, `views.py`, `urls.py`
- `integrations/migrations/__init__.py`, `0001_initial.py`,
  `0002_integrations_rls.py`
- `integrations/core/__init__.py`, `adapters.py`, `events.py`,
  `schemas.py`, `registry.py`
- `integrations/tests/__init__.py`, `conftest.py`, `test_services.py`,
  `test_api.py`, `test_rls.py`, `test_core.py`
- `events/__init__.py`, `apps.py`, `models.py`, `services.py`,
  `tasks.py`
- `events/migrations/__init__.py`, `0001_initial.py`,
  `0002_integrationevent_rls.py`
- `events/tests/__init__.py`, `conftest.py`, `test_services.py`,
  `test_tasks.py`, `test_rls.py`
- `customers/__init__.py`, `apps.py`, `models.py`, `services.py`
- `customers/migrations/__init__.py`, `0001_initial.py`,
  `0002_customer_rls.py`
- `customers/tests/__init__.py`, `conftest.py`, `test_services.py`,
  `test_rls.py`
- `transactions/__init__.py`, `apps.py`, `models.py`, `exceptions.py`,
  `services.py`, `serializers.py`, `views.py`, `urls.py`
- `transactions/migrations/__init__.py`, `0001_initial.py`,
  `0002_transaction_rls.py`
- `transactions/tests/__init__.py`, `conftest.py`, `test_services.py`,
  `test_api.py`, `test_rls.py`

## New dependencies
No new dependencies. E.164 and ISO 4217 checks are stdlib regexes.
Encryption reuses `cryptography` through `core.crypto`.

## Rules for implementation
- Django + DRF monolith. Business logic lives only in `services.py`,
  never in views, serializers or tasks. Tasks and views are thin
  wrappers.
- Every tenant-owned model uses `core.TenantScopedManager`. RLS
  (`rls_direct`) is enabled on all five new tables, each in its own
  migration.
- Never trust client-supplied `merchant_id`/`location_id`. The merchant
  comes from the session or the `Integration`. The location comes from
  `IntegrationLocationMapping`, never from a payload or `SaleCreated`.
- Celery tasks take `merchant_id` explicitly as the **first** argument
  (`tenant_task`) and set tenant context before any query.
  `retry_failed_events` touches only `Merchant` outside a tenant
  context.
- Idempotency is enforced by database unique constraints plus a
  savepoint and `IntegrityError`, never check-then-insert.
- Role checks use DRF permission classes: `IsOwnerOrAdmin` for
  `/integrations`, the default `IsMerchantMember` for `/transactions`.
  MANAGER scoping goes through `accessible_locations` only.
- External IDs are UUIDs, never sequential integers.
- `Integration.credentials_encrypted` is Fernet-encrypted through
  `core.crypto`. It is never returned, logged or put in audit metadata.
- Status enums are `UPPER_SNAKE_CASE`. Timestamps are suffixed `_at`.
  Provider identifiers stay lowercase, per Data-Dictionary.
- No V2 features, no bespoke admin app, no broker-side
  `eta`/`countdown` scheduling (retry backoff is computed from
  `updated_at` by the Beat sweeper).
- Never log phone numbers, credentials, tokens or raw payloads.
  `error_code` and `error_message` come only from `SAFE_ERRORS`, never
  from exception text. Unexpected-exception logs carry the class name
  and stack frames only, never the exception message.
- `Transaction.customer` is nullable. A missing phone never fails an
  event, and a supplied phone is always E.164-validated.
- `CANCELLED` events are terminal. Disconnect cancels pending events
  atomically, and `process_event` re-checks the integration status
  under the event lock.
- Mapping writes lock the `Integration` row first
  (`select_for_update()`).
- Do not implement eligibility, `CampaignExecution`, the `Transaction`
  row lock, webhooks, adapters, `/sales`, API-key auth or opt-out
  setting. Those belong to later phases (see "Where Phase 04 stops").
- Every new service function has a unit test. There is no concrete
  adapter, so there is no fixture-based contract test yet. The
  pipeline is tested with a test-only `FakeAdapter`, and per-adapter
  contract tests arrive in Phase 06/17.

## Definition of done
All of the following are verified by `pytest` against real
PostgreSQL, with Celery eager. Webhook scenarios are exercised at the
**service level** (`record_event` + `process_integration_event`)
because Phase 04 has no HTTP receiver.

**Models, migrations and RLS**
- [ ] `python manage.py migrate` applies all 8 migrations on a clean
  database, and `makemigrations --check` reports no changes.
- [ ] RLS is `ENABLE`d and `FORCE`d with a `tenant_isolation` policy on
  `integrations_integration`, `integrations_integrationlocationmapping`,
  `events_integrationevent`, `customers_customer` and
  `transactions_transaction`.
- [ ] **Tenant isolation, both layers.**
  - Under merchant A's context, each model's scoped manager returns
    none of merchant B's rows.
  - A raw SQL `SELECT` or `INSERT` on each table with merchant A's
    `SET LOCAL` cannot read or write B's rows (RLS backstop).
  - A query with no context returns nothing.
- [ ] **RLS pooled connection.** After a `tenant_atomic()` block ends,
  a new transaction on the same connection sees no tenant rows.

**Inbox and idempotency** (`Testing-Strategy.md` priority scenarios)
- [ ] **Duplicate Webhook Event.** `record_event` called 5× with the
  same `(integration, external_event_id)`, then processed, yields
  exactly one `IntegrationEvent` and one `Transaction`. Calls 2–5
  return `created=False` and enqueue nothing.
- [ ] **Cross-Merchant Event Isolation.** Merchant A's and merchant B's
  integrations each record `external_event_id="1"`. That creates two
  events, each with its own `merchant_id`.
- [ ] **Duplicate External Event ID Across Integrations.** Two
  integrations under the *same* merchant with the same
  `external_event_id` create two events.
- [ ] `record_event` sets `merchant_id == integration.merchant_id`,
  `source == integration.provider`, `status=RECEIVED`, and a non-null
  `received_at`.
- [ ] `record_event` raises `TenantContextError` with no context or a
  different merchant's context, and raises `IntegrationDisconnected`
  for a `DISCONNECTED` integration.

**Pipeline**
- [ ] A recorded event with a valid payload (`FakeAdapter`) ends
  `PROCESSED`, with `location` and `processed_at` set.
  - It creates one `Customer` (E.164 phone) and one `COMPLETED`
    `Transaction` with correct `merchant`, `location`, `customer`,
    `integration`, amount, currency and `occurred_at`.
  - `Customer.total_transactions == 1` and `first/last_seen_at ==
    occurred_at`.
- [ ] A second event (different `external_event_id`) for the same
  `(location, external_transaction_id)` ends `PROCESSED`. It creates no
  second `Transaction`, and `total_transactions` stays 1.
- [ ] A second sale for the same phone reuses the `Customer`,
  increments `total_transactions` and widens `first/last_seen_at`
  correctly.
- [ ] **Duplicate task enqueue.** Calling `process_integration_event`
  twice for one event produces one `Transaction`. The second call is a
  no-op on `PROCESSED`.
- [ ] **Celery task tenant context.**
  `process_integration_event(merchant_A, event_of_B)` raises
  `DoesNotExist` and never reads or writes B's rows.

**Sales with and without a customer phone** (Decision 17)
- [ ] **Sale with phone.** It creates a `Customer` and a `Transaction`
  whose `customer` is that `Customer`. The event is `PROCESSED`.
- [ ] **Sale without phone.** The phone is absent, `None`, `""` or
  whitespace.
  - It creates a `COMPLETED` `Transaction` with `customer IS NULL`.
  - The event is `PROCESSED`, with `attempt_count == 0` and no
    `error_code`.
  - **No `Customer` row is created.**
  - The event is never picked up by `retry_failed_events`.
- [ ] **Later sale with phone.** After a phoneless sale, a new sale
  (different `external_transaction_id`) with a phone creates the
  `Customer` normally, with `total_transactions == 1`.
- [ ] A **supplied invalid** phone (for example, `"98765"`) still
  fails: `FAILED`, `error_code == "INVALID_PHONE"`, no `Customer` and
  no `Transaction`. E.164 validation is not weakened.
- [ ] `GET /transactions` and `GET /transactions/{id}` return
  `customer: null` for a phoneless transaction.

**Safe error storage** (Decision 19)
- [ ] Bad currency, negative amount, a naive `occurred_at` or a blank
  `external_transaction_id` each leave the event `FAILED` with
  `attempt_count == 1`, the matching `error_code`/`error_message` from
  `SAFE_ERRORS`, and no `Customer`/`Transaction`.
- [ ] A provider with no registered adapter leaves the event `FAILED`
  with `ADAPTER_NOT_FOUND`, not a crash. An unresolvable location
  gives `LOCATION_UNRESOLVED`.
- [ ] **PII cannot leak.** A `FakeAdapter` whose `normalize()` raises
  a `ReviewFlowError` subclass, and another that raises a plain
  `ValueError`, each with the message `"+919999999999 secret-token
  {payload}"`, both result in `error_code == "PROCESSING_ERROR"` and
  `error_message == "Event processing failed"`. Neither field, nor any
  `caplog` record, contains the phone, `secret-token` or any payload
  value.
- [ ] A `PayloadValidationError` with an unrecognized code is stored
  as `PAYLOAD_INVALID`.
- [ ] Every stored `(error_code, error_message)` pair is a member of
  `SAFE_ERRORS`, which is asserted across all failure-path tests.
- [ ] An exception raised mid-pipeline (after the customer insert)
  rolls back every partial write. The event is `FAILED` with
  `PROCESSING_ERROR`.

**Location resolution** (Multi-location integrations)
- [ ] One active mapping without `external_location_id` resolves to
  that location.
- [ ] One integration mapped to two locations with distinct
  `external_location_id`s resolves each incoming provider location to
  the correct ReviewFlow location.
- [ ] An unknown `external_location_id`, a missing
  `external_location_id` with more than one active mapping, or zero
  active mappings leaves the event `FAILED` with no `Transaction`
  created.
- [ ] Inactive mappings are ignored.
- [ ] A later retry succeeds after the merchant adds the missing
  mapping.

**Retry and dead letter**
- [ ] A `FAILED` event with `attempt_count=n` is not due before
  `5 min × 2^(n−1)` since `updated_at`, and is due after it.
  `retry_failed_events` enqueues only due events.
- [ ] `FAILED → retry → PROCESSED` works.
- [ ] The 5th failed attempt sets `DEAD_LETTER`. `retry_failed_events`
  never enqueues `DEAD_LETTER` or `PROCESSED` events, and
  `process_event` on a `DEAD_LETTER` event is a no-op.

**Disconnect and `CANCELLED`** (Decision 18)
- [ ] **Disconnect cancels pending events.** An integration has
  events in `RECEIVED`, `FAILED`, `PROCESSED`, `DEAD_LETTER` and
  `CANCELLED`. After `DELETE /integrations/{id}`:
  - `RECEIVED` and `FAILED` become `CANCELLED`, with their
    `error_code`, `error_message` and `attempt_count` unchanged.
  - `PROCESSED`, `DEAD_LETTER` and `CANCELLED` are unchanged.
  - No event row is deleted.
  - Another integration's pending events are untouched.
- [ ] Disconnect is atomic. If the audit write is forced to fail, the
  integration stays `CONNECTED`, mappings stay active, credentials are
  kept and no event is cancelled.
- [ ] **`CANCELLED` is never retried.** `retry_failed_events`
  (including the stale-`RECEIVED` sweep, with `received_at` old
  enough) never enqueues a `CANCELLED` event.
- [ ] **`process_event` on `CANCELLED` is a no-op.** Status, fields and
  `updated_at` are unchanged, and no `Customer` or `Transaction` is
  created.
- [ ] **Race guard.** A `RECEIVED` event whose integration is
  `DISCONNECTED`, simulating the late insert, is set to `CANCELLED` by
  `process_event` without creating any `Customer` or `Transaction`.
- [ ] A `RECEIVED` event older than 10 minutes is re-enqueued. A fresh
  `RECEIVED` event is not.
- [ ] `retry_failed_events` covers events from two merchants in one
  run. Each is enqueued with its own `merchant_id`, and no RLS bypass
  or privileged connection is used.
- [ ] `CELERY_BEAT_SCHEDULE["retry-failed-events"]` targets
  `events.tasks.retry_failed_events` every 300 s on queue `events`.
- [ ] No task in this phase uses `eta` or `countdown`.

**Integrations API and services**
- [ ] `POST /integrations/webhook/connect`, with `webhook` registered
  in the test, returns `201`.
  - The response has no credentials field and `has_credentials: true`.
  - `credentials_encrypted` decrypts to the submitted JSON and is not
    plaintext in the database.
  - One `integration.connected` audit row is written, and its metadata
    contains no credentials.
- [ ] `connect` returns `422` for a non-enum provider and for an enum
  provider with no registered adapter.
- [ ] MANAGER and VIEWER get `403` on every `/integrations` endpoint.
  Unauthenticated calls get `403`. A missing CSRF token on writes gets
  `403`.
- [ ] `GET /integrations` returns only the current merchant's
  integrations, with mapping summaries, and never exposes credentials.
- [ ] `PATCH` replaces `config_json`. A cross-merchant id returns
  `404`.
- [ ] `DELETE` sets `DISCONNECTED`, clears credentials, deactivates
  every mapping and writes one `integration.disconnected` audit row. A
  repeat returns `204` with no second audit row.
- [ ] `POST /integrations/{id}/locations` returns `201`. A duplicate
  returns `409 mapping_exists`. Unknown and cross-merchant
  `location_id` both return the identical `422` body. A duplicate
  active `external_location_id` returns `422`.
- [ ] **Cross-merchant `IntegrationLocationMapping` is rejected.**
  `_assert_same_merchant` raises for a mismatched, unsaved `Location`,
  reached without going through the scoped read.
- [ ] `PUT /integrations/{id}/locations` atomically replaces the set:
  it upserts listed locations and deletes unlisted ones. A validation
  failure on any item leaves the previous set unchanged, whether the
  failure is on the first or the last item.
- [ ] **Concurrent mapping replacement is serialized** (Decision 20).
  This is a `@pytest.mark.django_db(transaction=True)` test with two
  threads, each using its own DB connection and tenant context:
  - Thread A calls `replace_location_mappings` with set X and pauses
    inside its transaction after acquiring the `Integration` lock,
    using a `threading.Event` hook.
  - Thread B calls it with set Y. While A holds the lock, B is shown
    to be blocked: it has not returned.
  - After A commits, B completes. The final mapping set equals Y
    exactly. There is no `IntegrityError` and no mix of X and Y.
  - A service-level check also confirms that
    `replace_location_mappings` issues `SELECT … FOR UPDATE` on
    `integrations_integration` before any mapping query (captured
    SQL).

**Transactions API**
- [ ] `GET /transactions` returns only the current merchant's
  transactions, with the documented body.
  - Cursor pagination works: `next_cursor`, `limit` max 100.
  - `location_id`, `date_from`/`date_to` and `status` filters work.
  - A malformed filter returns `422`.
- [ ] A MANAGER sees only transactions at assigned locations, through
  `accessible_locations`. `GET /transactions/{id}` for an unassigned
  location returns `404`.
- [ ] `GET /transactions/{id}` for another merchant's id returns `404`,
  never the data.
- [ ] VIEWER can read both endpoints.

**Hygiene**
- [ ] No log call in the new apps emits a phone number, credentials or
  payload content. A test asserts this for the failure path using
  `caplog`.
- [ ] Tenant and RLS isolation (above) holds for every new table,
  including a customer-less `Transaction` row.
- [ ] The full `pytest` suite passes. Existing Phase 00–03 tests are
  unchanged and passing: none was edited, skipped, `xfail`ed or
  weakened.

**Explicitly out of scope** (their priority scenarios are owned
elsewhere): Duplicate Campaign Execution (same campaign and racing
campaigns), Concurrent Workers–Eligibility, Concurrent
Workers–Dispatch, Refund Race, Opt-Out Enforcement,
Quota/Expiry/Resume (Phases 10–11), and adapter contract tests (Phase
06/17).
