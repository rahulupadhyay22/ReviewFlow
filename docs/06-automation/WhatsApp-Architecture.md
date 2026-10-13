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
    def register_number(self, credential, phone_number_id, pin) -> None: ...
```

*Signature amended by spec 08-whatsapp-embedded-signup (approved 2026-10-09).* The provider has no database access, so it never takes a `Merchant`.

**Credentials per account.** A `SHARED_POOL` account uses the platform env token. An `OWN_NUMBER` account uses its merchant's decrypted `access_token_enc`, Meta's per-customer business token (`../05-integrations/WhatsApp-Meta.md` E-3).

**Onboarding calls.** The provider also exposes `exchange_code`, `verify_number_access` and `subscribe_app` for the Embedded Signup flow.

`MetaCloudProvider` is the V1 (and only) implementation, against Meta's Graph API. The interface exists so a future `GupshupProvider`/`ThreeSixtyDialogProvider` can be added without touching `campaigns/` code — the scheduler only ever calls `WhatsAppService.send_template()`.

## Template Structure & Variables

- Templates must be pre-approved by Meta before use — no free-form marketing copy.
- Standard variables: `{{customer_name}}`, `{{business_name}}`, `{{location_name}}`, `{{review_link}}`.
- Example: *"Hi {{customer_name}}, thanks for visiting {{business_name}}! We'd love to hear about your experience. [Review us on Google]"*
- **Shared-pool sends must lead with `{{business_name}}` prominently** — the sending number gives the customer no brand signal otherwise. This rule applies to shared-pool templates only: a template bound to a merchant's own `OWN_NUMBER` account is not required to include `{{business_name}}` (spec 08-whatsapp-embedded-signup, approved 2026-10-09).

**Known V1 limitation (Phase 08, accepted 2026-10-08):** a template whose submission to Meta never completes (the submit task's transient-error retries are exhausted, or the recovery lookup after a refused submit fails permanently) stays `PENDING` with no `provider_template_id`. Approval polling covers only templates that have a `provider_template_id`, so such a template is not retried automatically. It is never marked `REJECTED` from an incomplete lookup. The merchant can create a new template under a different name. An automatic re-enqueue of old unsubmitted templates is the documented upgrade path.

## Sending

- Always through Celery, never inline in a request-response cycle.
- Triggered by the poll-based dispatch loop (see `Campaign-Engine.md`).

## Delivery / Read / Failure Tracking

- Status updates arrive exclusively via `POST /webhooks/whatsapp`, Meta's single `messages` callback shared with inbound messages (see `../04-api/Webhook-Specification.md`) and update `WhatsAppMessage` — this is the only place message status ever changes.
- Failure handling distinguishes transient errors (retry with backoff) from permanent ones (invalid number, opted-out — no retry, mark `FAILED`).
- **These updates never propagate back onto `CampaignExecution`.** `CampaignExecution` reaching `SENT` is a one-way business-workflow fact; `WhatsAppMessage` reaching `DELIVERED` or `READ` afterward does not change `CampaignExecution.status`. See `Campaign-Engine.md` §"Responsibility Split" for the full rationale and the retry-doesn't-recount-usage rule.

## Opt-Out

- A customer replying with an opt-out keyword is parsed from `POST /webhooks/whatsapp` (the `messages` entries of Meta's single callback) and sets `Customer.opted_out = True`.
- Also settable manually from the dashboard.
- **Reliability (amended 2026-10-12):** if the keyword cannot be published to the broker, the webhook answers `503` so Meta redelivers it. The fan-out then opts the customer out inline, merchant by merchant, attempts every merchant, and retries only the failed ones a bounded number of times, never dropping a failure silently. It uses `acks_late` with `reject_on_worker_lost`, and everything is idempotent. **Limitations:** the broker is not durable storage, so a message lost from Redis after a successful publish is not recovered; a retry needs the broker; and after a hard kill of the whole worker a queued STOP is redelivered only after the broker's visibility timeout (3600 s by default, not overridden), from reading the Celery and kombu source rather than an observed test.
- Once opted out, no future `CampaignExecution` is created for that phone — checked at eligibility, not just at send time.

## Quota

- Quota (`UsageRecord`) is reserved atomically when a `CampaignExecution` transitions from `SCHEDULED` to `SENDING`, immediately before the provider call. `SCHEDULED` and `QUOTA_EXCEEDED` consume zero quota; retries of the same execution never consume additional quota.
- Exceeding quota holds the execution as `QUOTA_EXCEEDED` for a 7-day retention window, after which it expires if quota never becomes available. Full rules: `../08-billing/Billing-Specification.md`.

## Shared Number (`SHARED_POOL`)

- V1: exactly one `SHARED_POOL` `WhatsAppAccount`. Because the pool is just more rows of the same model, V1.1 can add rotation across multiple `SHARED_POOL` accounts without a schema change.
- Quality-rating monitoring: a scheduled task polls Meta's phone-number quality rating for every shared account; a drop below threshold triggers an internal alert (not yet merchant-facing in V1).

## Migration: Shared → Own Number

A merchant on the shared number can connect their own later: a new `WhatsAppAccount(sender_type=OWN_NUMBER)` is created, and `WhatsAppLocationMapping` is repointed to it. Existing message history stays attributed to whichever account was active at send time via `WhatsAppMessage.whatsapp_account_id` — no backfill needed. The repointing is a separate `PUT /locations/{id}/whatsapp-sender`, made once the own number is `ACTIVE`; Embedded Signup never maps locations itself.

**Own-number lifecycle** *(spec 08-whatsapp-embedded-signup, approved 2026-10-09)*:

- `PENDING` after Embedded Signup completes and the number is registered;
- `ACTIVE` once the app is subscribed to the WABA. `ACTIVE` means "registered and subscribed". Meta's exact ready-to-send criteria are unverified (WhatsApp-Meta.md E-11);
- `SUSPENDED` on disconnect, or when activation fails permanently.

Disconnect is refused while locations are mapped. It is local only: ReviewFlow clears its stored token, the number is never deregistered, and the app stays subscribed, so STOP replies are still honoured. Clearing the stored token does not revoke the merchant's grant to the app in Meta (WhatsApp-Meta.md E-8c, unverified).

**Own-number templates** belong to that number's WABA (`MessageTemplate.whatsapp_account`; NULL = the shared WABA). Template operations on an own WABA stay disabled until E-7b is verified.

**Opt-out on an own number:** a STOP to an own number opts the customer out of that number's merchant only. The merchant is resolved after signature verification by the approved `whatsapp_number_lookup` (ES-3) (`../02-architecture/Multi-Tenancy.md`).

**Known limitation:** ReviewFlow does not yet react when a merchant removes its app in Meta (`account_update` `PARTNER_APP_UNINSTALLED`).

## Onboarding Flow

See `../01-product/User-Flows.md` §2 for the full step-by-step onboarding for both `OWN_NUMBER` (Embedded Signup) and `SHARED_POOL` paths.
