# ReviewFlow — Product Requirements Document (PRD)

*Status: V1 locked. This is the product source of truth — architecture and implementation docs derive from it, not the other way around.*

## 1. Product Overview

ReviewFlow is a SaaS platform for local, multi-location businesses (cafes, restaurants, salons, clinics, retail, gyms, spas, hotels) that automatically turns completed sales into genuine Google review opportunities. It connects once to a merchant's existing sale-capture system (POS, e-commerce, or a generic webhook), waits a configurable delay after each sale, sends the customer a WhatsApp message inviting them to review the business on Google, and syncs the resulting Google reviews back into a merchant dashboard with multi-location analytics.

## 2. Problem

Local businesses generate real customer satisfaction every day but rarely convert it into visible Google reviews, because asking in person is inconsistent, easy to forget, and awkward to scale across multiple staff and locations. Existing tools either require heavy manual work, risk manipulating reviews (fake reviews, selective routing of only happy customers), or don't handle multi-location businesses well.

## 3. Target Customers

Independent and small-chain local businesses, 1-20 locations, using a POS or e-commerce system, who want a low-effort, compliant way to grow their Google review volume and understand review activity across locations. Primary verticals: cafes, restaurants, salons, clinics, retail, gyms, spas, hotels.

## 4. Goals

- Automate the ask: every eligible completed sale results in a WhatsApp review request with no manual staff action.
- Keep it genuine: never fabricate, gate, or selectively route reviews.
- Make multi-location businesses first-class, not an afterthought.
- Give owners one dashboard to understand review performance across every location.
- Ship a focused V1 fast, architected so V2 (AI insights, more integrations, more platforms) doesn't require a rewrite.

*This document's V1/V2 boundaries are summarized here; `Feature-Scope.md` in this folder is the authoritative source of truth if anything below reads ambiguously.*

## 5. V1 Scope

- Account, business profile, subscription/billing
- Multi-location management with per-location settings
- Integrations (all in V1 **product/architecture** scope; implementation phased — see `Feature-Scope.md` and `../05-integrations/Integration-Architecture.md`): Shopify (implementation priority), Generic Webhook, Generic REST API, CSV Import (implementation priority, alongside Shopify), WooCommerce, Petpooja, GoFrugal (V1 scope, later implementation phase), Zapier, Make (V1 scope, implementation phase per roadmap)
- Sale ingestion with idempotent event processing and eligibility checks
- WhatsApp automation: templates, scheduled sending, delivery/read/failed tracking, opt-out
- Google Business Profile OAuth, review link, review sync, review dashboard
- Optional customer feedback page (rating + comment + categories)
- Analytics: business-wide + per-location, funnel, review trends, campaign performance
- QR codes for review/feedback links
- Public API: API keys, webhooks, REST endpoints, event logs
- Billing tiers: Starter / Growth / Pro / Business
- Internal admin (Django Admin, customized)

## 6. V1 Exclusions

AI sentiment analysis, AI review replies/summaries, advanced CRM, loyalty/referrals, email/SMS campaigns, a visual workflow builder, competitor intelligence, POS integrations beyond the set listed in §5, a mobile app, advanced customer segmentation, and additional review platforms (Tripadvisor, Facebook, Yelp, Zomato, Swiggy). Every item here carries **Status: V2 — Planned, V1 Implementation: NO** wherever it appears in other documents — see `Feature-Scope.md` for the authoritative list and section 12 below for the V2 roadmap.

## 7. User Roles

| Role | Can do |
|---|---|
| Owner | Everything, including billing and account deletion |
| Admin | Manage locations, integrations, campaigns - not billing/deletion |
| Manager | Manage campaigns for their assigned location(s) only |
| Viewer | Read-only dashboard access |

## 8. Features

See the full feature list in V1 Scope above; detailed behavior for each is specified in its own document: WhatsApp automation -> `06-automation/WhatsApp-Architecture.md`; Google -> `06-automation/Google-Reviews.md`; ingestion -> `06-automation/Event-Processing.md`; eligibility/scheduling -> `06-automation/Campaign-Engine.md`.

## 9. User Journeys

Summarized here; full step-by-step flows are in `User-Flows.md` in this folder:
- Merchant onboarding (sign up -> connect integration -> configure WhatsApp -> connect Google -> activate campaign)
- Customer review journey (sale -> WhatsApp -> Google review link -> customer submits on Google)
- Optional feedback journey (sale -> WhatsApp -> feedback page -> Google CTA shown regardless of rating)

## 10. Business Rules

Summarized here; full detail in `Business-Rules.md` in this folder. Key rules: no fake reviews, no review creation on our end, no selective routing by rating, one review request per transaction overall (enforced transactionally across campaigns — see `Business-Rules.md` §2 and `../06-automation/Campaign-Engine.md`), respect opt-out and frequency caps, quota enforcement never silently drops a request (7-day retention + auto-resume — see `../08-billing/Billing-Specification.md`).

## 11. Success Metrics

- Review requests sent per merchant per month
- WhatsApp delivery + read rate
- Google CTA click-through rate
- Growth in Google review count / rating per connected location, month over month
- Merchant retention by plan tier
- Time from signup to first successful review request sent (activation speed)

## 12. V2 Roadmap

AI sentiment/topic/complaint detection, AI review summaries and reply suggestions, in-dashboard review reply management, advanced analytics (rating/review velocity, sentiment trends), customer segmentation, a visual workflow builder replacing the single campaign mode, additional POS integrations (Toast, Square, Lightspeed, more Indian POS), additional review platforms (Tripadvisor, Facebook, Zomato, Swiggy), loyalty/referral campaigns, and competitor/market intelligence. See `Architecture.md` "V2 Extension" section for how V1 is designed to absorb these without a rewrite.
