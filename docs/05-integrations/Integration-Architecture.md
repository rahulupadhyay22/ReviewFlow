# Integration Architecture

*Per-provider implementation docs (Shopify.md, WooCommerce.md, Petpooja.md, GoFrugal.md, Generic-Webhook.md, CSV-Import.md, Zapier.md, Make.md) are created during implementation of each adapter, once real payload samples are available. This document specifies the shared architecture every provider adapter must follow, and the full product-scope integration list — see `../01-product/Feature-Scope.md` for how V1 scope and implementation phasing relate.*

## Goal

"Connect almost any business system" — every integration, whether a native POS adapter, a generic webhook, or a future automation connector, ends at the same normalized event. No downstream code (eligibility, campaign scheduling) ever needs to know which provider produced a `Transaction`.

## Product Scope vs. Implementation Phase

**All of the following are part of ReviewFlow's V1 product/architecture scope.** None are removed from V1 — what differs between them is *when* each is implemented, per the roadmap, not *whether* it belongs in the architecture:

| Integration | V1 product scope? | Implementation phase |
|---|---|---|
| Shopify | Yes | **V1 implementation priority** — built first |
| Generic REST API | Yes | V1 implementation, alongside Shopify |
| Generic Webhook | Yes | V1 implementation, alongside Shopify |
| CSV Import | Yes | V1 implementation, alongside Shopify |
| WooCommerce | Yes | V1 product scope; implementation phased later per roadmap |
| Petpooja | Yes | V1 product scope; implementation phased later per roadmap |
| GoFrugal | Yes | V1 product scope; implementation phased later per roadmap |
| Zapier | Yes | V1 product/architecture scope; implementation phase as defined by the roadmap |
| Make | Yes | V1 product/architecture scope; implementation phase as defined by the roadmap |

An `Integration` is merchant-level. `IntegrationLocationMapping` maps one integration to one or more internal locations; provider payload normalization resolves the location through that mapping or an explicit provider location identifier. The architecture supports all of these identically through the adapter pattern below — a later implementation phase for WooCommerce/Petpooja/GoFrugal/Zapier/Make is a sequencing decision, not an architectural limitation, and is not to be read as "removed from V1" or "moved to V2." Any future POS or automation platform beyond this list (a V2-scope addition) is added the same way, without changing this architecture.

## Directory Structure

```
integrations/
├── core/
│   ├── adapters.py     # BaseAdapter interface
│   ├── events.py        # SaleCreated dataclass — the normalized event
│   ├── schemas.py        # per-provider raw payload validation
│   └── registry.py        # maps an Integration's provider -> adapter class
├── shopify/              # V1 implementation priority
├── woocommerce/          # V1 product scope, phased implementation
├── petpooja/             # V1 product scope, phased implementation
├── gofrugal/             # V1 product scope, phased implementation
├── webhook/              # generic: merchant defines their own JSON field mapping — V1 implementation priority
├── csv_import/           # V1 implementation priority
├── zapier/               # V1 product/architecture scope, implementation phase per roadmap
└── make/                 # V1 product/architecture scope, implementation phase per roadmap
```

## Integration-to-Location Mapping

`Integration` is merchant-level. `IntegrationLocationMapping` is the only source of truth for associating a connected provider with one or more internal locations. Provider-specific location identifiers are stored in the mapping/configuration when required.

Inbound processing must resolve a ReviewFlow `Location` before creating/updating a `Transaction`. If a payload cannot be mapped unambiguously, the event remains in the Event Inbox for review/retry and must not create an unscoped transaction.

For providers where one connection represents exactly one store/location, the integration simply has one active mapping. For providers such as Shopify where one connection can represent multiple provider locations, the same integration can have multiple mappings.

## The Adapter Contract

```python
class BaseAdapter(ABC):
    def verify(self, request) -> bool: ...          # signature/secret check — fail closed
    def parse(self, raw_payload: bytes) -> dict: ...  # provider-specific parsing
    def normalize(self, parsed: dict) -> SaleCreated: ... # -> common schema
```

**Revised lookup**: `integrations/core/registry.py` resolves the adapter from the receiving `Integration` row (identified from the webhook URL/credentials) rather than from the event's `source` string alone — this is what lets the merchant be identified *before* the `IntegrationEvent` is created (see `../02-architecture/Multi-Tenancy.md` and `../04-api/Webhook-Specification.md`). The webhook receiver view stays thin: identify Integration → verify → store event (merchant_id already known) → enqueue `process_integration_event.delay(event_id)`.

## Native Adapters

Shopify, WooCommerce, Petpooja, GoFrugal — each implements `verify()`/`parse()`/`normalize()` against that provider's actual webhook/API payload shape. Payment method (UPI, cash, card, etc.) is captured as a `Transaction` attribute only — payment providers (Razorpay, BharatPe, etc.) are never treated as a primary integration or a triggering event source.

## Generic Webhook Adapter

For any system not natively supported: the merchant configures a field-mapping (JSON path → normalized field) in the dashboard, stored on `Integration.config_json`. The `webhook/adapter.py` interprets this mapping at parse time. This is what makes "connect almost any business system" true without writing a new adapter per merchant.

## CSV Import Adapter

Accepts a bulk file of past/offline transactions; each row is normalized the same way a single webhook event would be, and is subject to the same idempotency rule (`(location_id, external_transaction_id)`) so re-uploading the same file is safe.

## Idempotency & Normalization

See `../04-api/Webhook-Specification.md` for the full event-inbox and idempotency contract, and `../06-automation/Event-Processing.md` for what happens after normalization.

## Adding a New Native Adapter (beyond the 9 already in V1 scope)

1. Create `integrations/<provider>/adapter.py` implementing `BaseAdapter`.
2. Add fixture-based contract tests using real (anonymized) sample payloads from the provider's docs — see `../10-development/Testing-Strategy.md`.
3. Register the adapter in `registry.py` under the provider identifier (matched from `Integration.provider`, not the event's `source` field).
4. No changes needed anywhere downstream of `normalize()`.
