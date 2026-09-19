# WhatsApp Architecture

*Note: the exact Meta onboarding/API requirements below should be verified against current Meta documentation before implementation — Meta's API and policies evolve independently of this document.*

## Models

- **`WhatsAppAccount`** — merchant-owned (`sender_type = OWN_NUMBER`) or platform-owned (`sender_type = SHARED_POOL`). See `../03-database/Data-Dictionary.md` for fields.
- **`WhatsAppLocationMapping`** — resolves which account a `Location` currently sends from (`UNIQUE(location_id)`).
- **`WhatsAppMessage`** — one sent/attempted message and its delivery lifecycle (`QUEUED, SENT, DELIVERED, READ, FAILED`). This is the **only** place delivery/read status is tracked — see "Responsibility Split" in `Campaign-Engine.md`. `CampaignExecution` (the business-workflow record) never mirrors `DELIVERED`/`READ`.
- **`MessageTemplate`** — approved template text, variables, Meta approval status.

## Why the Sender Belongs to the Merchant, Not the Location

A location resolves its active sender via `WhatsAppLocationMapping` at send time — the campaign scheduler never needs to know whether a merchant uses one number for everything, one per location, or the shared platform fallback; it just asks "what account does this location map to." This supports all three real shapes without a schema change.

## Service Interface

```python
class WhatsAppService:
    def send_template(execution, template, variables) -> WhatsAppMessage: ...
    def get_message_status(provider_message_id) -> status: ...
    def handle_webhook(payload) -> None: ...          # updates WhatsAppMessage status
    def validate_template(template) -> bool: ...
    def handle_opt_out(phone) -> None: ...             # marks Customer.opted_out
```

## Provider Adapter

```python
class WhatsAppProvider(ABC):
    def send(self, to: str, template: MessageTemplate, variables: dict) -> ProviderSendResult: ...
    def parse_status_webhook(self, payload: dict) -> list[StatusUpdate]: ...
    def register_number(self, merchant, number) -> ProviderNumberRef: ...
```

`MetaCloudProvider` is the V1 (and only) implementation, against Meta's Graph API. The interface exists so a future `GupshupProvider`/`ThreeSixtyDialogProvider` can be added without touching `campaigns/` code — the scheduler only ever calls `WhatsAppService.send_template()`.

## Template Structure & Variables

- Templates must be pre-approved by Meta before use — no free-form marketing copy.
- Standard variables: `{{customer_name}}`, `{{business_name}}`, `{{location_name}}`, `{{review_link}}`.
- Example: *"Hi {{customer_name}}, thanks for visiting {{business_name}}! We'd love to hear about your experience. [Review us on Google]"*
- **Shared-pool sends must lead with `{{business_name}}` prominently** — the sending number gives the customer no brand signal otherwise.

## Sending

- Always through Celery, never inline in a request-response cycle.
- Triggered by the poll-based dispatch loop (see `Campaign-Engine.md`).

## Delivery / Read / Failure Tracking

- Status updates arrive exclusively via `POST /webhooks/whatsapp/status` (see `../04-api/Webhook-Specification.md`) and update `WhatsAppMessage` — this is the only place message status ever changes.
- Failure handling distinguishes transient errors (retry with backoff) from permanent ones (invalid number, opted-out — no retry, mark `FAILED`).
- **These updates never propagate back onto `CampaignExecution`.** `CampaignExecution` reaching `SENT` is a one-way business-workflow fact; `WhatsAppMessage` reaching `DELIVERED` or `READ` afterward does not change `CampaignExecution.status`. See `Campaign-Engine.md` §"Responsibility Split" for the full rationale and the retry-doesn't-recount-usage rule.

## Opt-Out

- A customer replying with an opt-out keyword is parsed from `POST /webhooks/whatsapp/inbound` and sets `Customer.opted_out = True`.
- Also settable manually from the dashboard.
- Once opted out, no future `CampaignExecution` is created for that phone — checked at eligibility, not just at send time.

## Quota

- Quota (`UsageRecord`) is reserved atomically when a `CampaignExecution` transitions from `SCHEDULED` to `SENDING`, immediately before the provider call. `SCHEDULED` and `QUOTA_EXCEEDED` consume zero quota; retries of the same execution never consume additional quota.
- Exceeding quota holds the execution as `QUOTA_EXCEEDED` for a 7-day retention window, after which it expires if quota never becomes available. Full rules: `../08-billing/Billing-Specification.md`.

## Shared Number (`SHARED_POOL`)

- V1: exactly one `SHARED_POOL` `WhatsAppAccount`. Because the pool is just more rows of the same model, V1.1 can add rotation across multiple `SHARED_POOL` accounts without a schema change.
- Quality-rating monitoring: a scheduled task polls Meta's phone-number quality rating for every shared account; a drop below threshold triggers an internal alert (not yet merchant-facing in V1).

## Migration: Shared → Own Number

A merchant on the shared number can connect their own later: a new `WhatsAppAccount(sender_type=OWN_NUMBER)` is created, and `WhatsAppLocationMapping` is repointed to it. Existing message history stays attributed to whichever account was active at send time via `WhatsAppMessage.whatsapp_account_id` — no backfill needed.

## Onboarding Flow

See `../01-product/User-Flows.md` §2 for the full step-by-step onboarding for both `OWN_NUMBER` (Embedded Signup) and `SHARED_POOL` paths.
