# Google Reviews Specification

## Core Principle — Restated

**ReviewFlow does not create Google reviews.** The Google review page is owned by Google; the customer manually writes and submits their own review there. The platform never builds a review composer, never posts on a customer's behalf, and never calls an unsupported Google API to fabricate review creation.

## Models

- **`GoogleConnection`** — a merchant-level OAuth authorization (tokens encrypted at rest).
- **`GoogleLocation`** — maps one internal `Location` to one Google Business Profile location; stores the review link, cached rating/review count, last sync timestamp.
- **`GoogleReview`** — one synced review, upserted by `(google_location_id, google_review_id)`.

This three-model separation is deliberate: OAuth/authentication (`GoogleConnection`) is never mixed with location-level review data (`GoogleLocation`/`GoogleReview`).

## Flow

```
Merchant clicks "Connect Google"
   ↓
OAuth consent screen (Google)
   ↓
Callback -> exchange code for access + refresh tokens -> GoogleConnection (encrypted)
   ↓
Call Account/Location-list API -> present mapping UI
   ↓
Merchant maps each Google location to an internal Location
   ↓
GoogleLocation created (stores the Google Business Profile location identifier and, separately, the Google Place ID used for the public review link)
   ↓
First sync enqueued immediately; subsequent syncs on schedule
```

## Review Link

Built from the stored Google Place ID (not `google_location_id`): `https://search.google.com/local/writereview?placeid=<place_id>`. `google_location_id` remains the Business Profile location resource identifier used for review sync. Usable in WhatsApp messages, QR codes, the feedback page, receipts, packaging, or counter displays. The business can test the link before activating a campaign.

## Sync

- Polling-based (Google doesn't broadly offer real-time review webhooks), via `GoogleLocation.last_synced_at` as the watermark.
- Scheduled by Celery Beat (`sync_google_reviews`), every 30–60 minutes per location, staggered to respect API quota.
- New/updated reviews are upserted on `(google_location_id, google_review_id)`.
- Rate-limit handling: back off per Google's `Retry-After` header, resume on the next scheduled tick rather than busy-retrying.

## OAuth Token Lifecycle

- Refreshed proactively by a daily Celery Beat task (`refresh_google_tokens`), before expiry.
- On refresh failure or explicit revocation: `GoogleConnection.status = NEEDS_REAUTH`, a dashboard banner appears on every location using that connection, and sync pauses for those locations — **sending is never affected**, since WhatsApp sending and Google sync are fully decoupled.

## Multiple Connections Per Merchant (edge case)

Most merchants manage every location under one Google account, so onboarding defaults to a single `GoogleConnection` mapped to multiple locations. Franchise-style merchants where different locations are managed under different Google accounts can add a second `GoogleConnection` later — this is supported by the schema (`GoogleConnection` is 1───* per `Merchant`) without being the default UI path.

## Attribution — What Is and Is Not Shown

**No attribution heuristic exists in V1.** The dashboard shows three independent, honestly-labeled metrics with no implied causal link:

- **Review Requests** — count of `CampaignExecution` rows that reached `SENT`
- **Google CTA Clicks** — count of `google_click_at` timestamps set
- **Google Reviews** — count of synced `GoogleReview` rows

Google gives no reliable mechanism to link a specific review back to a specific click or message, so none is manufactured. If a reliable signal is ever available (e.g. Google ships one), an explicit **Attributable Reviews** metric can be added then — not before. See `../01-product/Business-Rules.md` §8.

## Failure Cases

| Failure | Handling |
|---|---|
| Token expired/revoked | `NEEDS_REAUTH` status, dashboard banner, sync paused, sending unaffected |
| Google API rate-limited | Back off per `Retry-After`, resume on next tick |
| Location unmapped | No sync attempted; campaign activation is blocked until mapped or explicitly skipped |
