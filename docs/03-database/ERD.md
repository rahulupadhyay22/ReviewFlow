# Entity Relationship Diagram (ERD)

```
Merchant 1───* TeamMember *───1 User
Merchant 1───* TeamMemberLocation *───1 Location    (explicit through-model — see below)
Merchant 1───* Location
Merchant 1───* Customer
Merchant 1───* Integration
Merchant 1───* IntegrationEvent      (direct — merchant_id resolved at creation, never null)
Merchant 1───* ApiKey
Merchant 1───* GoogleConnection
Merchant 1───* WhatsAppAccount
Merchant 1───1 Subscription ───1 Plan

Merchant 1───* Integration
Integration 1───* IntegrationLocationMapping *───1 Location
Location 1───1 GoogleLocation ───* GoogleReview
Location 1───* ReviewCampaign
Location 1───* QRCode ───* QRScanEvent
Location 1───1 WhatsAppLocationMapping ───*..1 WhatsAppAccount ───1 Merchant

Integration 1───* IntegrationEvent   (unique on (integration_id, external_event_id))
IntegrationEvent 0..1───1 Transaction        (event may be rejected/retried; transaction is created by successful normalization)

Customer 1───* Transaction                 (optional: a sale recorded without a customer phone has no Customer)
Transaction 1───* CampaignExecution        (one per campaign — see Database-Design.md;
                                             creation is lock-guarded — see Campaign-Engine.md)
CampaignExecution 1───1 WhatsAppMessage
CampaignExecution 1───0..1 Feedback

GoogleConnection 1───* GoogleLocation

Merchant 1───* UsageRecord
Merchant 1───* PaymentAttempt
Merchant 1───* AuditLog
```

## Reading This Diagram

- **`Merchant` is the tenant root.** Almost every relationship in the system eventually traces back to one `Merchant` row — this is what `merchant_id` scoping and Row-Level Security key off of (see `../02-architecture/Multi-Tenancy.md`).
- **`Location` is the second-most-connected node.** Campaigns, Google mapping, WhatsApp mapping, QR codes, and integration-to-location mappings attach here. Integrations themselves are merchant-level and may map to multiple locations.
- **`Transaction → CampaignExecution` is many-to-many in spirit but one-to-one in V1 practice.** The schema allows one transaction to have executions under multiple campaigns (`UNIQUE(campaign_id, transaction_id)`), but V1's eligibility rules ensure only one review-type campaign ever fires per transaction. See `Database-Design.md` and `../06-automation/Campaign-Engine.md`.
- **`WhatsAppAccount` is merchant-owned, not location-owned.** A location resolves its active sender through `WhatsAppLocationMapping` — this is what lets one number serve every location, one number per location, or a mix with the shared platform account, without a schema change.
- **`GoogleConnection` sits above `GoogleLocation`.** One OAuth connection can cover many locations; `GoogleReview` always belongs to a `GoogleLocation`, never directly to a `Location` or `GoogleConnection`.
- **`IntegrationEvent` now attaches to `Merchant` directly and to `Integration`, not to a nullable `location_id` alone.** This closes a tenant-isolation gap where the same `external_event_id` could theoretically collide across two different merchants' integrations — see `Database-Design.md` and `../02-architecture/Multi-Tenancy.md`.
- **`TeamMemberLocation` is an explicit through-model, not a default M2M table.** It exists so a cross-tenant assignment (a `TeamMember` from Merchant A pointing at a `Location` owned by Merchant B) is rejected by an application-level invariant rather than only being theoretically possible and merely untested.
