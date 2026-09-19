# Feature Scope — V1 vs. V2 (Single Source of Truth)

This document is the authoritative answer to "is X in V1?" for every feature discussed anywhere in the ReviewFlow documentation. Where another document's wording could be read either way, **this document wins.**

## The Scope Rule

```
V1 = IMPLEMENT NOW
V2 = DOCUMENTED / PLANNED / DO NOT IMPLEMENT NOW
```

A feature or field can appear in the schema, an interface, or a document ahead of its implementation (this is normal — see "V1 Product Scope vs. Implementation Phase" below) without being a V2 feature. **V2 status is about the capability itself being out of scope for V1, not about when within V1 a piece of it gets built.**

## V1 — Implement Now

- Account, auth, roles (OWNER/ADMIN/MANAGER/VIEWER), multi-location management
- Review request automation: eligibility engine, campaign scheduling, WhatsApp sending (own number or shared pool)
- Google Business Profile integration: OAuth, location mapping, review link, review sync
- Optional customer feedback (direct-to-Google or feedback-first mode)
- Multi-location review analytics (direct PostgreSQL aggregation — see `../02-architecture/SAD.md` §7)
- QR tracking (`QRCode` + `QRScanEvent`, per `../03-database/Data-Dictionary.md`)
- Billing: Plan/Subscription/UsageRecord, atomic quota reservation, Razorpay, 7-day PAST_DUE grace/dunning policy, and full quota rules per `../08-billing/Billing-Specification.md`
- **All integrations listed in `../05-integrations/Integration-Architecture.md`** — Shopify, WooCommerce, Petpooja, GoFrugal, Generic REST API, Generic Webhook, CSV Import, Zapier, Make — are V1 **product/architecture scope**. See "V1 Product Scope vs. Implementation Phase" below for how their build order differs without any of them being removed from V1.
- Internal admin: customized Django Admin (no bespoke admin app — see `../02-architecture/SAD.md` §3)
- Security/audit: multi-tenancy isolation, transaction-local RLS, `AuditLog`, and the controls in `../09-security/Security-Controls.md`; V1 operational retention is defined in `../09-security/Privacy-Data-Retention.md`

## V2 — Planned, Documented, Do NOT Implement in V1

Every item below carries this status wherever it is mentioned in any ReviewFlow document:

> **Status: V2 — Planned. V1 Implementation: NO.**

- AI sentiment analysis
- AI review summaries
- AI topic/complaint detection
- AI reply suggestions (including the `GoogleReview.reply_text`/`reply_status` fields already present in the schema — see `../03-database/Data-Dictionary.md`)
- In-dashboard review reply management
- Advanced customer segmentation
- A visual workflow builder (replacing the single `ReviewCampaign.mode` enum)
- Loyalty / referral programs
- Competitor / market intelligence
- Advanced AI-driven analytics reports
- A mobile app
- Additional review platforms beyond Google (Tripadvisor, Facebook, Zomato, Swiggy, etc.)
- Additional POS integrations beyond the nine listed in V1 scope above (e.g. Toast, Square, Lightspeed) — note this is a **different** category from WooCommerce/Petpooja/GoFrugal/Zapier/Make, which **are** V1 scope, just phased (see below)

## V1 Product Scope vs. Implementation Phase — Do Not Confuse the Two

These are two different axes and must not be conflated:

| | Meaning | Example |
|---|---|---|
| **V1 product/architecture scope, later implementation phase** | The capability IS part of what V1 is designed to support; the adapter architecture already accommodates it; the code for it just hasn't been written yet, per the roadmap sequencing | WooCommerce, Petpooja, GoFrugal, Zapier, Make |
| **V2 — Planned, do not implement in V1** | The capability is explicitly out of V1's scope; it may be documented (data model hooks, architecture extension points) but must not be built during V1 development | AI sentiment analysis, loyalty/referrals, additional review platforms |

**The failure mode this document exists to prevent**: a field existing in the Data Dictionary (e.g. `GoogleReview.reply_text`) does not mean it's safe to implement in V1. Conversely, an integration being scheduled for a later development phase (e.g. GoFrugal) does not mean it's V2 — it is fully V1 in scope, just sequenced later in the roadmap.

## How to Use This Document

- When starting work on any feature, check here first if its V1/V2 status is ambiguous elsewhere.
- When writing new documentation, any V2 item must carry the exact status line above, not a paraphrase — this keeps the phrase machine-greppable for future audits.
- This document is updated whenever a genuinely new scope decision is made — it does not get updated to reflect implementation *order* changes (that's the roadmap's job, not this document's).
