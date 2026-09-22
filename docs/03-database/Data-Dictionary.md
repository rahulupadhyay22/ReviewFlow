# Data Dictionary

Exhaustive field-level reference for every model. Types are logical (Django-agnostic); map to the appropriate Django field type during implementation (e.g. `string` → `CharField`/`TextField` depending on length, `money` → `DecimalField`, `enum` → `CharField` with `choices` or a Postgres enum).

Every model additionally has `id` (PK, UUID recommended for externally-exposed models per `../02-architecture/Security-Architecture.md`), `created_at`, and — where the row is ever updated — `updated_at`. These are omitted from the tables below for brevity.

### Merchant
| Field | Type | Nullable | Notes |
|---|---|---|---|
| name | string | No | |
| business_type | string | Yes | e.g. cafe, salon, clinic |
| timezone | string | No | IANA tz name |
| plan_id | FK → Plan | Yes | |
| status | enum | No | ACTIVE, SUSPENDED, DELETED |

### User
| Field | Type | Nullable | Notes |
|---|---|---|---|
| email | string | No | unique |
| password_hash | string | No | |
| is_active | bool | No | |
| totp_secret_encrypted | text | Yes | Fernet-encrypted base32 TOTP secret. Set while enrollment is pending or confirmed; NULL when 2FA is off. Never logged or exposed after the one-time setup response. |
| totp_confirmed_at | datetime | Yes | 2FA is enabled iff non-null |
| totp_last_used_step | bigint | Yes | last accepted TOTP time step; a code at or before this step is rejected (replay protection) |
| totp_recovery_code_hashes | jsonb | No, default `[]` | sha256 hashes of the unused one-time recovery codes |

### TeamMember
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | |
| user_id | FK → User | No | |
| role | enum | No | OWNER, ADMIN, MANAGER, VIEWER (unchanged) |
| invited_at / accepted_at | datetime | Yes | |

Location assignment for MANAGER role is **not** a Django default M2M — see `TeamMemberLocation` below.

### TeamMemberLocation

Explicit through-model for TeamMember-to-Location assignment (replaces an implicit/default M2M table). Exists specifically to prevent a TeamMember from Merchant A ever being assignable to a Location belonging to Merchant B — see `../02-architecture/Multi-Tenancy.md`.

| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | must equal both `team_member.merchant_id` and `location.merchant_id` — enforced by the service layer; RLS filters the row by its own merchant_id but does not prove the three-way parent equality |
| team_member_id | FK → TeamMember | No | |
| location_id | FK → Location | No | |

**Unique constraint**: `(team_member_id, location_id)`.
**Application-level invariant** (enforced in `services.py`, not just documented): `TeamMemberLocation.merchant_id == TeamMember.merchant_id == Location.merchant_id`. Any attempt to create a row where these three don't match is rejected before it reaches the database.

### Location
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | |
| name | string | No | |
| address | string | Yes | |
| phone | string | Yes | |
| timezone | string | Yes | overrides merchant default if set |
| is_active | bool | No | |

### Integration
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | merchant-level integration owner |
| provider | enum | No | shopify, woocommerce, petpooja, gofrugal, webhook, csv, zapier, make — all V1 product scope; implementation phased |
| status | enum | No | CONNECTED, ERROR, DISCONNECTED |
| credentials_encrypted | encrypted blob | Yes | never plaintext |
| config_json | json | Yes | provider settings and generic field mapping |

### IntegrationLocationMapping
Explicit mapping between a merchant-level integration and an internal location. One integration may map to multiple locations.

| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | must equal Integration.merchant_id and Location.merchant_id |
| integration_id | FK → Integration | No | |
| location_id | FK → Location | No | |
| config_json | json | Yes | optional location-specific provider mapping |
| is_active | bool | No | |

**Unique constraint**: `(integration_id, location_id)`. **Service-layer invariant**: all three merchant IDs must match.

### IntegrationEvent

**Revised for tenant safety** — `merchant_id` is now resolved and set at creation time, never left `NULL` for later resolution. The same `external_event_id` can legitimately recur across different integrations/merchants (e.g. two different Shopify stores both using their own invoice numbering), so uniqueness is scoped to `integration_id`, not the bare `source` string.

| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | **No** | resolved from `integration_id` at creation — never null, never resolved "later" |
| integration_id | FK → Integration | **No** | identifies which connected system sent this |
| source | string | No | provider identifier (denormalized from `Integration.provider` for fast filtering/logging) |
| external_event_id | string | No | unique with `integration_id` |
| location_id | FK → Location | Yes | resolved during normalization (may still be unknown at creation if the payload doesn't map cleanly) |
| payload | json | No | raw inbound payload |
| status | enum | No | RECEIVED, PROCESSED, FAILED, DEAD_LETTER |
| received_at / processed_at | datetime | Yes | |

**Unique constraint**: `(integration_id, external_event_id)` — **not** `(source, external_event_id)`.
**Application-level invariant**: `IntegrationEvent.merchant_id == Integration.merchant_id`, enforced at creation, not merely documented — see `../02-architecture/Multi-Tenancy.md`.

### Customer
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | |
| phone | string (E.164) | No | unique with `merchant_id` |
| name | string | Yes | |
| first_seen_at / last_seen_at | datetime | Yes | |
| total_transactions | integer | No | denormalized counter |
| opted_out | bool | No | default false |
| opted_out_at | datetime | Yes | |

### Transaction
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | |
| location_id | FK → Location | No | |
| customer_id | FK → Customer | No | |
| integration_id | FK → Integration | Yes | |
| external_transaction_id | string | No | unique with `location_id` |
| amount | money | No | |
| currency | string (ISO 4217) | No | e.g. INR |
| payment_method | string | Yes | attribute, not a driver of logic |
| status | enum | No | COMPLETED, REFUNDED, VOIDED |
| occurred_at | datetime | No | |

### ReviewCampaign
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | |
| location_id | FK → Location | No | |
| mode | enum | No | DIRECT_GOOGLE, FEEDBACK_FIRST |
| delay_minutes | integer | No | default per spec: 30–60 |
| is_active | bool | No | |
| message_template_id | FK → MessageTemplate | Yes | |
| frequency_cap_days | integer | No | |

### CampaignExecution

**Status set revised** to own only the business-workflow lifecycle — delivery-lifecycle states (`DELIVERED`, `READ`) belong to `WhatsAppMessage`, not here. See `../06-automation/Campaign-Engine.md` for the full separation-of-responsibilities rationale.

| Field | Type | Nullable | Notes |
|---|---|---|---|
| campaign_id | FK → ReviewCampaign | No | unique with `transaction_id` |
| transaction_id | FK → Transaction | No | |
| customer_id | FK → Customer | No | |
| status | enum | No | SCHEDULED, SENDING, SENT, FAILED, CANCELLED, QUOTA_EXCEEDED, EXPIRED |
| scheduled_at | datetime | No | indexed with `status` |
| sent_at | datetime | Yes | |
| quota_exceeded_at | datetime | Yes | set when the execution transitions to `QUOTA_EXCEEDED` |
| expires_at | datetime | Yes | set alongside `quota_exceeded_at` = `quota_exceeded_at + 7 days`; when `expires_at < now()` and status is still `QUOTA_EXCEEDED`, transition to `EXPIRED` (see `../01-product/Business-Rules.md` §6) |
| google_click_at | datetime | Yes | |
| feedback_submitted_at | datetime | Yes | |

### MessageTemplate
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | |
| name | string | No | |
| language | string | No | |
| body | text | No | contains `{{variable}}` placeholders |
| status | enum | No | PENDING, APPROVED, REJECTED |
| provider_template_id | string | Yes | Meta's own template ID |

### WhatsAppAccount
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | Yes | null for platform-owned `SHARED_POOL` rows |
| sender_type | enum | No | OWN_NUMBER, SHARED_POOL |
| provider | enum | No | meta_cloud (extensible) |
| phone_number_id | string | No | provider reference |
| business_account_id | string | Yes | |
| status | enum | No | ACTIVE, PENDING, SUSPENDED |
| connected_at | datetime | Yes | |

### WhatsAppLocationMapping
| Field | Type | Nullable | Notes |
|---|---|---|---|
| whatsapp_account_id | FK → WhatsAppAccount | No | |
| location_id | FK → Location | No | unique |

### WhatsAppMessage
| Field | Type | Nullable | Notes |
|---|---|---|---|
| execution_id | FK → CampaignExecution | No | 1:1 |
| whatsapp_account_id | FK → WhatsAppAccount | No | account active at send time |
| provider | enum | No | |
| provider_message_id | string | Yes | indexed for webhook lookup |
| status | enum | No | QUEUED, SENT, DELIVERED, READ, FAILED |
| sent_at / delivered_at / read_at / failed_at | datetime | Yes | |
| failure_reason | string | Yes | |

### GoogleConnection
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | |
| google_account_email | string | No | |
| access_token_enc | encrypted blob | No | |
| refresh_token_enc | encrypted blob | No | |
| expires_at | datetime | No | |
| status | enum | No | ACTIVE, NEEDS_REAUTH |
| connected_at | datetime | Yes | |

### GoogleLocation
| Field | Type | Nullable | Notes |
|---|---|---|---|
| location_id | FK → Location | No | unique |
| google_connection_id | FK → GoogleConnection | No | |
| google_location_id | string | No | Google Business Profile location resource identifier; not the Place ID used in the public review URL |
| google_place_id | string | Yes | Google Place ID used to construct/validate the customer review URL; populated during mapping when available |
| review_link | string (URL) | No | generated from the stored Google Place ID; never inferred by treating `google_location_id` as a Place ID |
| average_rating | decimal | Yes | last synced value |
| review_count | integer | Yes | last synced value |
| last_synced_at | datetime | Yes | |

### GoogleReview
| Field | Type | Nullable | Notes |
|---|---|---|---|
| google_location_id | FK → GoogleLocation | No | unique with `google_review_id` |
| google_review_id | string | No | |
| reviewer_name | string | Yes | |
| rating | integer (1–5) | No | |
| text | text | Yes | |
| review_created_at | datetime | No | indexed with `google_location_id` |
| fetched_at | datetime | No | |
| reply_text | text | Yes | **Status: V2 — Planned. V1 Implementation: NO.** Column may exist in the schema ahead of time but must not be written to or exposed by any V1 code path — see `../01-product/Feature-Scope.md`. |
| reply_status | enum | Yes | **Status: V2 — Planned. V1 Implementation: NO.** Same as above. |

### Feedback
| Field | Type | Nullable | Notes |
|---|---|---|---|
| execution_id | FK → CampaignExecution | No | 1:1 |
| location_id | FK → Location | No | |
| rating | integer (1–5) | No | |
| comment | text | Yes | |
| categories_json | json | Yes | food, service, staff, wait_time, cleanliness, other |

### QRCode

**Finalized design**: scan tracking is owned by `QRScanEvent`, not a denormalized counter on this model — `scan_count` is removed. Total/location scan counts are derived by counting `QRScanEvent` rows (a `COUNT()` query, or a future rollup if that ever proves slow — same pattern as the analytics rollup decision elsewhere in the architecture).

| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | denormalized from `location_id` for direct tenant scoping |
| location_id | FK → Location | No | |
| name | string | No | merchant-facing label, e.g. "Counter QR — Hyderabad" |
| destination_type | enum | No | GOOGLE_REVIEW, FEEDBACK_PAGE |
| destination_url | string (URL) | No | the Google review link or feedback page URL this code redirects to |
| active | bool | No | inactive codes 404/redirect to a neutral page instead of tracking |

### QRScanEvent

One row per scan. Privacy-conscious by design — **no raw IP address is ever stored**.

| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | denormalized from `qr_code_id` for direct tenant scoping |
| qr_code_id | FK → QRCode | No | |
| location_id | FK → Location | No | denormalized from `qr_code_id` for fast per-location analytics queries |
| scanned_at | datetime | No | |
| user_agent | string | Yes | optional, for abuse-pattern detection |
| ip_hash | string | Yes | optional — a salted hash of the requester's IP, used only for abuse prevention/deduplication; the raw IP is never persisted |
| referrer | string | Yes | optional |

### Plan
| Field | Type | Nullable | Notes |
|---|---|---|---|
| name | string | No | Starter, Growth, Pro, Business |
| monthly_price | money | No | |
| quota_requests | integer | No | |
| features_json | json | Yes | |

### Subscription
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | |
| plan_id | FK → Plan | No | |
| status | enum | No | ACTIVE, PAST_DUE, CANCELLED, EXPIRED |
| current_period_start / end | datetime | No | |
| payment_provider_ref | string | Yes | |

### UsageRecord
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | |
| period_start / period_end | date | No | |
| requests_used | integer | No | atomically incremented when a `CampaignExecution` enters `SENDING` and reserves quota; `SCHEDULED`/`QUOTA_EXCEEDED` consume zero |

### PaymentAttempt
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | |
| subscription_id | FK → Subscription | No | |
| provider | enum | No | razorpay |
| provider_attempt_id | string | No | idempotency/provider reference |
| attempt_type | enum | No | RENEWAL, RETRY |
| status | enum | No | INITIATED, SUCCEEDED, FAILED |
| attempted_at | datetime | No | |

**Unique constraint**: `(provider, provider_attempt_id)`. Used to make dunning/payment retries idempotent.

### ApiKey
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | |
| key_hash | string | No | sha256, never plaintext |
| scopes_json | json | No | |
| is_active | bool | No | |
| last_used_at | datetime | Yes | |

### WebhookEndpoint
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | No | |
| url | string (URL) | No | |
| secret | string | No | for outbound HMAC signing |
| is_active | bool | No | |

### AuditLog
| Field | Type | Nullable | Notes |
|---|---|---|---|
| merchant_id | FK → Merchant | Yes | null for platform-level actions |
| actor_user_id | FK → User | Yes | |
| action | string | No | |
| target_type / target_id | string | Yes | polymorphic reference |
| metadata_json | json | Yes | |
