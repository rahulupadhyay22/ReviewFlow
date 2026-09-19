# Testing Strategy

## Test Categories

| Category | Purpose | Example |
|---|---|---|
| Unit tests | Pure functions, service-layer logic in isolation | Eligibility rule engine, `normalize()` per adapter |
| Integration tests | Multiple components together, Celery in eager mode | Webhook → event inbox → Celery task → `CampaignExecution` created |
| API tests | DRF endpoint contracts | Every endpoint in `../04-api/API-Specification.md`: auth, permissions, validation, error shapes |
| Webhook tests | Signature verification, idempotency | Same payload posted twice → one `Transaction` |
| Tenant isolation tests | Cross-merchant access is blocked | See below |
| Permission tests | Role-based access enforced | MANAGER cannot access unassigned locations |
| Celery tests | Task idempotency, tenant context | Duplicate task enqueue is safe |
| Database constraint tests | Unique/FK constraints hold under concurrency | See below |
| WhatsApp tests | Provider adapter mocked, status webhook handling | Delivery/read/failed transitions update `WhatsAppMessage` correctly |
| Google integration tests | OAuth flow, sync, upsert behavior | Re-syncing the same review doesn't duplicate it |
| Billing tests | Quota enforcement, 7-day expiry, auto-resume, subscription state transitions | Quota exhaustion holds executions as `QUOTA_EXCEEDED`; expiry after 7 days; resume re-checks all eligibility rules |
| Security tests | Auth bypass, IDOR, fail-closed webhook behavior | See `../09-security/Security-Controls.md` |

## Priority Test Scenarios (must exist before V1 ships)

### Tenant Isolation

```
Merchant A's session/API key
   → attempts to read/write Merchant B's Transaction/Location/etc.
   → expect: 403 or 404, never the data
```

Test this at both layers independently: with `TenantScopedManager` active (application-level), and with a raw query that bypasses it (confirming Postgres RLS catches it as the backstop).

### Duplicate Webhook Event

```
Same webhook payload
   → posted 5 times (same integration_id + external_event_id)
   → expect: exactly ONE Transaction, ONE IntegrationEvent (subsequent posts are no-ops)
```

### Cross-Merchant Event Isolation *(new)*

```
Merchant A's Integration and Merchant B's Integration
   → both receive a webhook with the SAME external_event_id (e.g. both providers
     numbered an invoice "1")
   → expect: TWO separate IntegrationEvent rows are created, one per integration/merchant —
     NOT treated as a duplicate of each other, since uniqueness is scoped to
     (integration_id, external_event_id), not the bare source string
```

### Duplicate External Event ID Across Integrations *(new)*

```
Same external_event_id posted to the SAME integration twice
   → expect: one no-op (true idempotency)
Same external_event_id posted once each to TWO DIFFERENT integrations
   (even under the same merchant)
   → expect: two separate IntegrationEvent rows — uniqueness is per-integration, not per-merchant
```

### Cross-Tenant Location Assignment *(new)*

```
Attempt to create a TeamMemberLocation row where team_member.merchant_id != location.merchant_id
   → expect: rejected before insert (422 or equivalent), never silently created
```

### Duplicate Campaign Execution — Same Campaign

```
Same transaction, same campaign
   → eligibility check + execution creation attempted twice (e.g. a retried Celery task)
   → expect: exactly ONE CampaignExecution (UNIQUE(campaign_id, transaction_id) enforces this
     at the database, not just in application logic)
```

### Duplicate Campaign Execution — Different Campaigns Racing *(new)*

```
Campaign A + Transaction X, Campaign B + Transaction X
   → both attempt eligibility check + execution creation concurrently
   → expect: exactly ONE CampaignExecution is ultimately created (across BOTH campaigns) —
     this is the case the UNIQUE(campaign_id, transaction_id) constraint alone does NOT catch;
     it requires the SELECT ... FOR UPDATE lock on Transaction described in
     ../06-automation/Campaign-Engine.md §"Concurrency & Locking"
```

### Concurrent Workers — Eligibility/Creation

```
Worker A and Worker B both evaluate eligibility for the same Transaction at the same instant
   (regardless of which campaign(s) are involved)
   → expect: the row lock serializes them; only ONE CampaignExecution is ultimately created;
     the second worker's check observes the already-created execution and no-ops
```

### Concurrent Workers — Dispatch

```
Worker A and Worker B both pick up the same due CampaignExecution
   in dispatch_due_executions at the same time
   → expect: SELECT ... FOR UPDATE SKIP LOCKED means only ONE worker sends;
     the other skips the locked row entirely
```

### CampaignExecution / WhatsAppMessage Status Independence *(new)*

```
CampaignExecution transitions to SENT
WhatsAppMessage for that execution later transitions to DELIVERED, then READ
   → expect: CampaignExecution.status remains SENT throughout — it never becomes
     DELIVERED or READ; those states exist only on WhatsAppMessage
```

### Quota Exhaustion, Expiry, and Resume *(new)*

```
Transaction eligible, quota exhausted
   → expect: CampaignExecution.status = QUOTA_EXCEEDED, quota_exceeded_at set,
     expires_at = quota_exceeded_at + 7 days, no WhatsApp message sent

7 days pass, quota still unavailable
   → expect: CampaignExecution.status = EXPIRED; a later resume attempt must NOT send it

Merchant upgrades plan within the 7-day window
   → expect: eligible QUOTA_EXCEEDED executions are re-checked against EVERY eligibility
     rule (not just quota) before being set back to SCHEDULED; an execution whose
     transaction was refunded in the meantime is NOT resumed
   → expect: resumed executions go through the normal dispatch pipeline, not an
     immediate burst send

Retry of an already-SENT execution
   → expect: UsageRecord.requests_used does not increment a second time
```

### QR Scan Tracking *(new)*

```
Customer scans a QRCode
   → expect: a QRScanEvent row is created (merchant_id, qr_code_id, location_id, scanned_at
     set); no raw IP address is ever stored, only ip_hash if captured at all
   → expect: the scan count derived from QRScanEvent matches the number of scans exactly
     (no separate counter to drift out of sync, since scan_count was removed from QRCode)
   → expect: analytics never present a QR scan as equivalent to, or attributed to, a
     specific Google review
```

### Refund Race Condition

```
Transaction refunded
   → arrives after CampaignExecution is scheduled but before scheduled_at
   → dispatch_due_executions re-checks Transaction.status at send time
   → expect: execution is CANCELLED, no WhatsApp message sent
```

### Opt-Out Enforcement

```
Customer opts out
   → new eligible transaction for that customer arrives afterward
   → expect: eligibility check fails at rule #2, no CampaignExecution created
```

## Contract Tests for Adapters

Each native POS adapter (Shopify, WooCommerce, Petpooja, GoFrugal) has a fixture test using a real (anonymized) sample payload from that provider's own documentation, asserting `normalize()` produces the exact `SaleCreated` shape. A provider API change should be caught by a failing fixture test, not discovered in production.

## Tooling

- `pytest` (or Django's test runner) as the primary framework.
- Celery's eager-execution mode for integration tests that span a task boundary.
- Factory fixtures (e.g. `factory_boy`) for `Merchant`/`Location`/`Customer`/`Transaction` to keep tests readable.
- CI runs the full suite plus dependency scanning (`pip-audit`) on every PR.


## Final Consistency Tests Added

### Billing / quota reservation
- Two concurrent dispatchers with one remaining quota unit → exactly one execution reserves quota and enters `SENDING`; the other becomes `QUOTA_EXCEEDED`.
- `SCHEDULED` and `QUOTA_EXCEEDED` executions consume zero quota.
- Provider failure after quota reservation does not increment usage again on retry.
- Refund before `SENDING` causes `CANCELLED` with zero usage.
- Refund after quota reservation does not reverse usage.
- `PAST_DUE` blocks new sends; successful payment restores `ACTIVE`.

### Multi-location integrations
- One merchant-level Integration mapped to multiple locations resolves each incoming provider location to the correct ReviewFlow location.
- Cross-merchant IntegrationLocationMapping is rejected.
- Same provider event ID can exist under separate integrations without collision.

### Google identifiers
- `google_location_id` and `google_place_id` are stored separately; review links are generated from Place ID only.

### RLS
- Pooled database connections cannot retain a previous merchant context because `SET LOCAL` is used inside each transaction.

### Frequency cap
- Customer request from Location A blocks another request from Location B for the merchant-wide cap window.
