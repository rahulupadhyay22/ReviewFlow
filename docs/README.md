# ReviewFlow Documentation Set

This is the initial documentation set, generated from the locked Architecture Blueprint. It covers everything needed to start implementation of the core system.

## What's Included

```
01-product/        PRD, User Flows, Business Rules, Feature Scope (V1/V2 source of truth)
02-architecture/    Architecture overview, SAD, Multi-Tenancy, Security Architecture
03-database/        Database Design, ERD, Data Dictionary
04-api/             API Specification, Webhook Specification, Authentication
05-integrations/    Integration Architecture (shared adapter pattern; scope-vs-phase table
                    for all 9 planned integrations)
06-automation/      Event Processing, Campaign Engine, WhatsApp Architecture, Google Reviews
08-billing/         Billing Specification (finalized quota/usage business rules)
09-security/        Security Controls, Privacy & Data Retention, Audit Logging
10-development/     Development Setup, Coding Standards, Testing Strategy
```

## Deferred — Create During Implementation

The following are intentionally **not** included yet — write them once the relevant code exists and real specifics (payload samples, deployed monitoring) are available to document accurately, rather than guessed in advance:

- `05-integrations/`: `Shopify.md`, `WooCommerce.md`, `Petpooja.md`, `GoFrugal.md`, `Generic-Webhook.md`, `CSV-Import.md`, `Zapier.md`, `Make.md`
- `07-analytics/`: `Analytics-Specification.md`
- `10-development/`: `Deployment.md`
- `11-operations/`: `Admin-Operations.md`, `Monitoring.md`, `Troubleshooting.md`
- `QR-Code-Specification.md` (the QR data model and API surface are finalized in `03-database/` and `04-api/`; a dedicated narrative doc can still be split out later if useful)

`08-billing/Billing-Specification.md` was originally deferred but has since been written — concrete V1 quota/usage business rules were finalized during the architecture review, so guessing was no longer required.

## Relationship to the Architecture Blueprint

This documentation set decomposes the single master Architecture Blueprint into the folder structure you specified. Two rounds of architecture review have been applied on top of the original blueprint:

1. The **first review** produced six corrections (WhatsApp account model, `CampaignExecution` uniqueness, poll-based scheduling, no attribution heuristic, analytics-as-later-optimization, Django Admin over a bespoke admin app).
2. A **second review** produced eight further corrections, reflected consistently across every affected document: `IntegrationEvent` tenant isolation (`merchant_id` resolved before creation, `UNIQUE(integration_id, external_event_id)`), an explicit `TeamMemberLocation` through-model, transactional locking for campaign-execution concurrency, a clean `CampaignExecution`/`WhatsAppMessage` responsibility split, finalized billing/quota rules (atomic quota reservation, 7-day `QUOTA_EXCEEDED` retention, auto-resume, no rollover, Razorpay + 7-day `PAST_DUE` policy), confirmation that all 9 planned integrations remain V1 scope (with implementation phasing, not removal), a single `Feature-Scope.md` source of truth for V1 vs. V2, and a finalized `QRCode`/`QRScanEvent` design.

Nothing in this documentation set reopens or contradicts either round of corrections.

## Suggested Reading Order for a New Developer

1. `01-product/PRD.md` → `Feature-Scope.md` — what and why, and what's actually in V1
2. `02-architecture/Architecture.md` → `SAD.md` → `Multi-Tenancy.md` — how, at a system level
3. `03-database/Database-Design.md` + `ERD.md` + `Data-Dictionary.md` — the data model
4. `06-automation/Event-Processing.md` → `Campaign-Engine.md` → `WhatsApp-Architecture.md` → `Google-Reviews.md` — the core product loop
5. `08-billing/Billing-Specification.md` — quota/usage rules
6. `04-api/` — the API contract
7. `10-development/Development-Setup.md` → `Testing-Strategy.md` — get running locally, and what must be tested


## Architecture Sign-off Status

The final consistency pass has resolved the remaining architecture ambiguities: merchant-level integrations with explicit `IntegrationLocationMapping`, atomic quota reservation, refund timing, merchant-wide frequency caps, distinct Google Business Profile location ID vs Place ID, transaction-local RLS context, and V1 retention/payment policies. The documentation is ready for Phase 0 implementation.


## Final Architecture Decisions

The final consistency pass also locks: merchant-level integrations with `IntegrationLocationMapping`; atomic quota reservation at `SCHEDULED → SENDING`; zero quota for `QUOTA_EXCEEDED`; refund handling based on whether quota has been reserved; merchant-wide frequency caps across locations/campaigns; distinct Google Business Profile location ID and Place ID; transaction-local `SET LOCAL` RLS context; a 7-day `PAST_DUE` grace period with sending disabled and dunning on days 0/3/6; and V1 operational retention periods.
