# Event Processing

*Covers what happens after an inbound event is received and verified — see `../04-api/Webhook-Specification.md` for the receiving/idempotency contract itself.*

## Pipeline

```
IntegrationEvent (status=RECEIVED, merchant_id already set — see Webhook-Specification.md)
   ↓ Celery task: process_integration_event(event_id, merchant_id)
Check: status != PROCESSED? (idempotency guard against duplicate task enqueue)
   ↓
adapter.normalize(parsed_payload) -> SaleCreated
   ↓
Customer get-or-create (merchant_id, phone)
   ↓
Transaction create (unique on location_id + external_transaction_id)
   ↓
SELECT Transaction FOR UPDATE (lock held for the remainder of this step)
   ↓
Eligibility check (see Campaign-Engine.md and ../01-product/Business-Rules.md §2), including a
check for any existing active/sent execution for this transaction under ANY campaign
   ↓
If eligible: CampaignExecution create (status=SCHEDULED, scheduled_at = now() + delay_minutes)
If not eligible solely because another execution already exists: no-op, no error
   ↓
COMMIT (releases the Transaction lock)
   ↓
IntegrationEvent.status = PROCESSED
```

The Celery task always receives `merchant_id` explicitly (per the tenant-scoping rule in `../02-architecture/Multi-Tenancy.md`) — it is never re-derived from the event payload inside the task.

## Failure Handling

| Failure point | Behavior |
|---|---|
| `parse()` throws (malformed payload) | `IntegrationEvent.status = FAILED`, error stored |
| `normalize()` throws | same as above |
| Transaction already exists (duplicate) | no-op, `status = PROCESSED` (this is expected on webhook retries, not an error) |
| Ineligible | `IntegrationEvent.status = PROCESSED`, no `CampaignExecution` created — this is a normal outcome, not a failure |
| Unexpected exception | `FAILED`, retried by `retry_failed_events` with backoff |
| Retry cap exceeded | `DEAD_LETTER`, visible in the admin panel for manual review |

## Idempotency Guarantees (recap)

1. `IntegrationEvent(integration_id, external_event_id)` — unique — a re-delivered webhook never creates a second event row. **Revised** from `(source, external_event_id)`, which was not tenant-safe (see `../02-architecture/Multi-Tenancy.md`).
2. `Transaction(location_id, external_transaction_id)` — unique — a duplicate sale payload never creates a second transaction.
3. `CampaignExecution(campaign_id, transaction_id)` — unique, **combined with a `SELECT ... FOR UPDATE` lock on the `Transaction` row during eligibility + creation** (see `../06-automation/Campaign-Engine.md` §"Concurrency & Locking"). The lock is the primary defense against a race between two campaigns or two concurrent workers; the unique constraint is the database-level backstop underneath it, not a substitute for it.

## Concurrency Considerations

- Multiple Celery workers may pick up retries of the same failed event; the `status != PROCESSED` guard on `IntegrationEvent`, combined with the `Transaction` row lock and the unique constraints above, makes concurrent processing of the same event — or of two different events touching the same transaction — safe without a separate distributed lock.
- The row lock is held only for the eligibility-check-and-execution-creation step, not for the entire event-processing pipeline, to avoid unnecessarily serializing unrelated work.
- See `../10-development/Testing-Strategy.md` for the specific concurrent-worker test cases required before this is considered proven.
