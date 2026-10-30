# Spec: Google Business Profile

## Overview

This spec delivers the whole of Phase 09: the `google_reviews` app.
It belongs to the **intelligence plane** (Architecture.md: "syncs Google
reviews, aggregates analytics"). It covers:

- `GoogleConnection`: a merchant-level OAuth authorization, with
  Fernet-encrypted tokens and `ACTIVE` / `NEEDS_REAUTH` status;
- the OAuth authorization-code connect flow (User-Flows.md §3);
- `GoogleLocation`: maps one internal `Location` to one Business
  Profile location. It stores `google_location_id` and
  `google_place_id` separately, and stores Google's documented
  `metadata.newReviewUri` as the review link (AM-2, OD-10 (b));
- `GoogleReview` sync: polling, per-merchant fan-out on the
  `google_sync` queue, and an upsert on
  `(google_location_id, google_review_id)`;
- `refresh_google_tokens` (daily Beat) and `NEEDS_REAUTH` handling;
- `GET /reviews`;
- the `GoogleSyncProvider` interface and its single V1
  implementation.

It exists now because Phase 09 depends only on Phase 03 (Done). Phase
10's activation rule ("a Google location is connected or explicitly
skipped") and Phases 13 and 14 need `GoogleLocation.review_link` and
`GoogleReview`.

The platform never creates, edits, replies to or posts a review.
ReviewFlow only reads reviews (Google-Reviews.md §Core Principle).

**Nothing about Google's API is asserted by this spec beyond
`docs/05-integrations/Google-Business-Profile.md`.** Every provider
fact is a gate item (G-1 … G-15, under "Google verification gate").
Each is verified against current Google documentation, with a URL and
date, in that file. This follows the `Shopify.md` and
`WhatsApp-Meta.md` precedent.

### Amendment AM-1 (2026-10-23): user decisions and gate verification

The user decided OD-1 … OD-9 on 2026-10-23, and the gate items were
read against official Google documentation on the same day. The
decisions are folded into the sections below; this list is the record.

- **OD-1: synchronous exchange preferred, not approved.** Option (a)
  is preferred, but only after G-4 is verified **and** the user
  explicitly approves GA-1. **GA-1 is not approved.** Until both hold,
  the code-exchange flow (`POST /google/oauth/complete` and everything
  behind it) is **not implemented**. If GA-1 is rejected, the
  asynchronous fallback defined under "OAuth attempt lifecycle"
  applies, after its own approval (OD-11).
- **OD-2: APPROVED.** `GoogleOAuthState`: a hashed, single-use state
  bound to merchant and user. AM-1 extends it into the attempt
  lifecycle below.
- **OD-3: DECIDED.** No Place ID from the provider → the mapping is
  refused with `422 google_place_id_missing`. `review_link` stays not
  null.
- **OD-4: DECIDED.** The reconnect key is the stable Google account
  identifier. G-6 is VERIFIED: the OpenID Connect ID token's `sub`
  ("unique among all Google Accounts and never reused"). It becomes
  the new column `google_account_sub` with
  `UNIQUE(merchant, google_account_sub)`. There is **no fallback to
  email**: an exchange that yields no `sub` fails.
- **OD-5: APPROVED.** The connect, complete, location-list, mapping
  and read endpoints, with the proposed roles. Unmapping and
  disconnect are **kept out of V1** (user decision); no `DELETE`
  endpoint is built.
- **OD-6: DECIDED.** `GET /reviews` opts in to API keys with
  `reviews:read`. A MANAGER `location_id` filter on an unassigned
  location returns an empty page.
- **OD-7: DECIDED.** `UNIQUE(google_connection, google_location_id)`
  on `GoogleLocation`.
- **OD-8: DECIDED.** Reviews that disappear upstream are kept locally
  in V1. There is no deletion detection. This is distinct from a
  deliberate local remap (RM-1), which deletes the old mapping's reviews.
- **RM-1: DECIDED (user, 2026-10-30).** When a `PUT` changes a
  mapping's `google_location_id`, the old Google location's reviews are
  deleted in the same transaction (see "Remapping a location").
- **PE-1: DECIDED (user, 2026-10-30).** Provider failures on the two
  request-path endpoints map to fixed `429`/`503`/`502` errors (see
  "Provider errors on the request path").
- **OD-9: DECIDED.** No audit event for `NEEDS_REAUTH`; a structured
  warning carrying only the connection UUID and a fixed reason code.
- **New, from gate verification:** OD-10 (review-link format, G-13
  UNVERIFIED; **blocking**), OD-11 (the asynchronous fallback's poll
  endpoint; blocking only if GA-1 is rejected), OD-12 (the stored form
  of `google_location_id`, G-11; non-blocking, proposed).
- **Issues resolved by AM-1:**
  - the async pending/completed/failed lifecycle and its safe failure
    handling ("OAuth attempt lifecycle");
  - refresh-token handling when Google returns no new refresh token
    ("Token handling");
  - atomic state transitions, expiry, duplicate tasks and failed
    exchanges ("OAuth attempt lifecycle");
  - the `bulk_create(update_conflicts=True)` field names, checked
    against the installed Django 5.2.17 ("Review upsert").

AM-1's OD-1, OD-10, OD-11 and OD-12 entries above are superseded by
AM-2.

### Amendment AM-2 (2026-10-23): OD-1, OD-10, OD-11, OD-12, ID-token validation

User decisions of 2026-10-23, folded into the sections below. Every
other AM-1 decision is unchanged.

- **OD-10: option (b) chosen.** `GoogleLocation.review_link` is
  Google's documented `metadata.newReviewUri` (G-9), stored as
  returned after validation. It is no longer built from the Place ID.
  - The undocumented `writereview?placeid=` format (G-13) is not used
    anywhere, so G-13 no longer gates anything.
  - `google_location_id` and `google_place_id` stay separate and are
    never substituted. The Place ID is still required for a mapping
    (OD-3 unchanged) and stored.
  - This changes locked text: **RL-1** under "Locked decisions
    touched" lists every document edit it needs. This spec does not
    apply them.
- **OD-1 / GA-1: GA-1 REJECTED.** The Architecture.md rule against
  synchronous non-read-only Google calls in a request stays as it is.
  The code exchange is asynchronous (`exchange_oauth_code` on
  `google_sync`). No Architecture.md exception is added.
- **OD-11: APPROVED.**
  - `GET /google/oauth/attempts/{id}`, readable only by the user who
    started the attempt;
  - the `exchange_oauth_code` task;
  - `409 google_token_refresh_pending` when the location list would
    need a token refresh, which the request path may not perform.
- **OD-12: ACCEPTED.** `google_location_id` stores the full
  `accounts/{accountId}/locations/{locationId}` resource name, because
  the v4 reviews call is addressed by that parent (G-11) while the
  Business Information API names a location only
  `locations/{locationId}` (G-8).
- **Security review: ID-token validation.** The ID token is fully
  validated before any claim is used: the RS256 signature against
  Google's JWKS, `iss`, `aud`, `exp` and a per-attempt `nonce`. This
  replaces AM-1's reliance on HTTPS delivery alone. The rules come
  from Google's documentation (gate **G-15**, VERIFIED) and are set
  out under "ID-token validation".

## Source docs

- `docs/ROADMAP.md`: V1 Phases table, §09 Google Business Profile,
  §10 (activation rule), §13, §14, §15 (dependents)
- `docs/06-automation/Google-Reviews.md`: all sections (Core
  Principle, Models, Flow, Review Link, Sync, OAuth Token Lifecycle,
  Multiple Connections Per Merchant, Attribution, Failure Cases)
- `docs/01-product/User-Flows.md`: §1 step 5, §3 Google OAuth Flow
- `docs/01-product/Business-Rules.md`: §1 (review integrity), §8
  (attribution), §7 (location overrides), activation prerequisites
- `docs/01-product/Feature-Scope.md`: V1 ("Google Business Profile
  integration: OAuth, location mapping, review link, review sync"); V2
  (AI reply suggestions incl. `reply_text`/`reply_status`, in-dashboard
  reply management)
- `docs/01-product/PRD.md`: V1 Scope, Key Flows
- `docs/02-architecture/Architecture.md`: Planes; the
  synchronous-call rule (line 61) and the ES-4 exception; Foundational
  Decision 3 (Google)
- `docs/02-architecture/SAD.md`: §2 (GoogleSyncService), §3
  (`google_reviews/`), §5 (queues, Beat `sync_google_reviews`,
  `refresh_google_tokens`), §6, §8 (Google rate limits)
- `docs/02-architecture/Multi-Tenancy.md`: Model, Layer 1, Layer 2,
  No Standing Privileged Role, Data Model Note
- `docs/02-architecture/Security-Architecture.md`: OAuth (Google,
  Meta), Authorization (roles), Threat Model (token revocation, IDOR),
  Encryption & Secrets
- `docs/FINAL-ARCHITECTURE-REVIEW.md`: §7 Google identifiers, §8 RLS,
  §9 Retention, Remaining work ("Verification of current Meta/Google
  provider requirements during adapter implementation")
- `docs/03-database/Data-Dictionary.md`: `GoogleConnection`,
  `GoogleLocation`, `GoogleReview`
- `docs/03-database/Database-Design.md`: §GoogleConnection,
  §GoogleLocation, §GoogleReview, §Location relationships
- `docs/03-database/ERD.md`: Google relationships and notes
- `docs/04-api/API-Specification.md`: §Reviews & Feedback (`GET
  /reviews`), §Locations (MANAGER `404` rule), General Conventions
- `docs/04-api/Authentication.md`: §2 (`reviews:read` scope, explicit
  per-endpoint opt-in), §4 OAuth (Google)
- `docs/09-security/Security-Controls.md`: OAuth tokens encrypted,
  secrets (`GOOGLE_OAUTH_CLIENT_SECRET`), identifiers, RLS
- `docs/09-security/Audit-Logging.md`: "Google connection added /
  reauthorized"
- `docs/09-security/Privacy-Data-Retention.md`: `GoogleReview`
  reviewer name/text, 24 months
- `docs/10-development/Coding-Standards.md`: §1–§7
- `docs/10-development/Testing-Strategy.md`: Google integration
  tests, Tenant Isolation, Google identifiers, RLS
- `docs/10-development/Development-Setup.md`: `GOOGLE_OAUTH_CLIENT_ID`
  / `GOOGLE_OAUTH_CLIENT_SECRET`, `google_sync` worker queue

## Depends on

- **Phase 03 (Done):**
  - `Location` (`locations_location`);
  - `locations.services.accessible_locations` and
    `get_accessible_location` (MANAGER scoping, `404` rule).
- **Phase 01/02 (Done):**
  - `BaseModel`, `TenantScopedManager` (with `tenant_field`),
    `tenant_context`, `tenant_atomic`, `tenant_task`;
  - `core/rls.py`: `rls_direct`, and `rls_via_parent`, which chains
    to any depth;
  - `core.crypto.encrypt` / `decrypt`;
  - `core.pagination.CursorPagination`;
  - role permission classes (`IsMerchantMember`, `IsOwnerOrAdmin`,
    `IsOwnerAdminOrManager`);
  - `auditlog.services.record`.
- **Phase 05 (Done):** `ApiKey.Scope.REVIEWS_READ` and the API-key
  opt-in mechanism (used only if OD-6 opts `/reviews` in).
- **Phase 07 (Done):** the per-merchant Beat fan-out pattern
  (`billing/tasks.py::run_billing_maintenance`).
- **Phase 08 (in progress, merged parts only):** the
  `EmbeddedSignupState` single-use, sha256-hashed state pattern
  (`whatsapp/models.py`, `whatsapp/services.py`), copied for the OAuth
  `state`. Nothing in this spec imports from `whatsapp`.

## Roadmap Phase

- Phase: 09 — Google Business Profile
- Completes entire phase: Yes
- If No: remaining phase work: n/a

This spec covers all five §09 bullets: `GoogleConnection`;
`GoogleLocation` with the two identifiers stored separately;
`GoogleReview` sync + upsert with the review link (from
`newReviewUri` under RL-1; the §09 bullet's "review link built from
Place ID only" is reworded by RL-1); `refresh_google_tokens` with
`NEEDS_REAUTH`; and `/reviews`.

Production use may still sit behind Google's own API-access approval
(gate G-1). That is an operational gate on the feature flag (see
"Production-readiness gates"), as ES-10 was for Embedded Signup. It
is not remaining roadmap work. `/ship-feature` verifies this and marks
the phase Done; this section does not.

**Condition (AM-1, updated by AM-2):** "Yes" holds only for a PR that
ships the asynchronous connection flow (`POST /google/oauth/complete`,
`exchange_oauth_code`, `GET /google/oauth/attempts/{id}`), the
mapping (`PUT /locations/{id}/google-location`), and the approved
RL-1 document edits. **A PR without all of these does not
complete Phase 09**, and `/ship-feature` must leave the phase
In progress.

## Locked decisions touched

- Shared schema, `merchant_id` isolation (`Multi-Tenancy.md` §Model)
  — DEPENDS ON
- `TenantScopedManager` as primary application scoping
  (`Multi-Tenancy.md` §Layer 1) — DEPENDS ON
- PostgreSQL RLS, transaction-local `SET LOCAL`, no RLS bypass
  (`FINAL-ARCHITECTURE-REVIEW.md` §8) — DEPENDS ON. No new lookup
  policy: every Google request carries a session, and every task
  carries an explicit `merchant_id`.
- Transitive tenant tables via documented parent `EXISTS` checks
  (`FINAL-ARCHITECTURE-REVIEW.md` §8; `Database-Design.md`
  §GoogleReview "LOCATION (via `google_location_id → location_id`)")
  — DEPENDS ON. `GoogleReview` reaches the merchant in two hops
  (`GoogleLocation → Location`). That path is the one the docs
  document, and `rls_via_parent` chains. No `merchant_id` column is
  added.
- Celery merchant context, explicit `merchant_id`
  (`Multi-Tenancy.md` §Layer 1) — DEPENDS ON
- Google architecture: merchant-level `GoogleConnection`,
  location-level `GoogleLocation`, `GoogleReview` per mapped location,
  never create a review (`Architecture.md` Foundational Decision 3) —
  DEPENDS ON
- Google identifiers: `google_location_id` ≠ `google_place_id`, never
  substituted (`FINAL-ARCHITECTURE-REVIEW.md` §7) — DEPENDS ON. The
  separation is unchanged. `google_location_id` holds the full v4
  resource name (OD-12).
- **Review link from the Place ID (`FINAL-ARCHITECTURE-REVIEW.md` §7
  "`google_place_id` = Place ID used for the public review URL";
  `Google-Reviews.md` §Review Link) — LOCKED DECISION CHANGE (RL-1).**
- `GoogleSyncProvider` adapter interface (`SAD.md` §6,
  `Coding-Standards.md` §3) — DEPENDS ON
- Google sync and WhatsApp sending fully decoupled; `NEEDS_REAUTH`
  never affects sending (`Google-Reviews.md` §OAuth Token Lifecycle)
  — DEPENDS ON
- No attribution heuristic (`Business-Rules.md` §8) — DEPENDS ON
- Retention: Google review PII 24 months
  (`FINAL-ARCHITECTURE-REVIEW.md` §9) — NO CHANGE (the purge job is
  Phase 15)
- Poll-based dispatch, no `eta`/`countdown` (`SAD.md` §5) — NO CHANGE.
  This spec sends nothing, and no Google retry uses `countdown`.
- One review request per transaction, quota reservation, billing —
  NO CHANGE (untouched)
- Synchronous provider calls in a request: "Django never talks to
  WhatsApp/Google synchronously in a request-response cycle (except
  short read-only dashboard calls)" (`Architecture.md` line 61) —
  **NO CHANGE.** GA-1 was REJECTED (AM-2). The only Google calls in a
  request are the short read-only location-list calls the rule already
  permits. Every token exchange, token refresh and JWKS fetch runs in
  Celery.

LOCKED DECISION CHANGE — USER SIGN-OFF REQUIRED

### RL-1 — Review link from Google's `newReviewUri` (APPROVED by the user, OD-10 (b), 2026-10-23)

**Status: APPROVED by the user on 2026-10-23**, including the exact
wording of items 1–8 below. `Architecture.md` stays unchanged. The
edits are applied verbatim in the implementation PR, and nothing is
broadened. The PR description records this sign-off
(Coding-Standards.md §8).

**What changes.** `GoogleLocation.review_link` stops being "generated
from the stored Google Place ID". It becomes the
`metadata.newReviewUri` that the Business Information API returns for
the location: "A link to the page on Google Search where a customer
can leave a review for the location" (G-9, VERIFIED field). The
documented `https://search.google.com/local/writereview?placeid=…`
format has no official source (G-13), and ReviewFlow stops using it.

**What does not change.**
- `google_location_id` and `google_place_id` remain two separate
  columns. Neither is ever substituted for the other.
- A link is never built from `google_location_id` (or from anything
  else).
- The Place ID is still required for a mapping (OD-3) and stored. It
  is simply no longer the source of the link.

**Required document edits (exact proposed text):**

1. `docs/FINAL-ARCHITECTURE-REVIEW.md` §7 Google identifiers.
   - Replace "`google_place_id` = Place ID used for the public review
     URL." with "`google_place_id` = Google Place ID of the location,
     stored for identification; it is not used to build the review
     URL."
   - Add the bullet: "The public review URL is Google's
     `metadata.newReviewUri` for the location, stored as returned; it
     is never built from `google_location_id` or `google_place_id`."
   - Add the line: "*Amended 2026-10-23 (Phase 09 spec RL-1,
     signed off by the user).*"
   - The bullet "The two identifiers are stored separately and are
     never substituted for one another." is unchanged.
2. `docs/06-automation/Google-Reviews.md`.
   - §Flow: replace "(stores the Google Business Profile location
     identifier and, separately, the Google Place ID used for the
     public review link)" with "(stores the Google Business Profile
     location resource name `accounts/{accountId}/locations/{locationId}`,
     separately the Google Place ID, and Google's `newReviewUri` as the
     review link)".
   - §Review Link, first sentence: replace "Built from the stored
     Google Place ID (not `google_location_id`):
     `https://search.google.com/local/writereview?placeid=<place_id>`."
     with "Google's own review URL for the location
     (`metadata.newReviewUri` from the Business Information API),
     stored as returned at mapping time and never constructed by
     ReviewFlow from `google_location_id` or `google_place_id`."
   - The rest of §Review Link is unchanged.
3. `docs/03-database/Data-Dictionary.md` §GoogleLocation.
   - `google_location_id` note: "Google Business Profile location
     resource name `accounts/{accountId}/locations/{locationId}` (the
     v4 reviews API is addressed by it); not the Place ID".
   - `google_place_id` note: "Google Place ID of the location
     (`metadata.placeId`); required by ReviewFlow when mapping;
     not used to build the review URL".
   - `review_link` note: "Google's `metadata.newReviewUri`, stored as
     returned; never built from `google_location_id` or
     `google_place_id`".
   - Also add the OD-4/OD-7 constraints and `GoogleOAuthState`, as
     listed under "Files to change".
4. `docs/03-database/Database-Design.md` §GoogleLocation, fields of
   note: replace "`google_place_id` (Place ID used for the public
   review URL)" with "`google_place_id` (Google Place ID) and
   `review_link` (Google's `newReviewUri`)".
5. `docs/10-development/Testing-Strategy.md` §Google identifiers:
   replace "review links are generated from Place ID only" with "the
   review link is Google's `newReviewUri`, never built from either
   identifier".
6. `docs/ROADMAP.md` §09: replace "`GoogleReview` sync + upsert;
   review link built from Place ID only" with "`GoogleReview` sync +
   upsert; review link from Google's `newReviewUri` (spec 09 RL-1)".
7. `CLAUDE.md` Warnings: replace "review links use the Place ID only"
   with "the review link is Google's `newReviewUri`, never built from
   either identifier". `docs/` wins over CLAUDE.md, but the warning
   would otherwise contradict it.

8. `.claude/agents/test-writer.md` line 116 (the Google row of its
   priority table): replace "review link uses Place ID" with "review
   link is Google's `newReviewUri`, never built from either
   identifier". Otherwise `/test-feature` would write tests asserting
   the old rule. `.claude/agents/quality-reviewer.md` and
   `test-runner.md` mention only the identifier separation, which is
   unchanged, and need no edit.

No other document mentions the link's source. `README.md` ("distinct
Google Business Profile location ID vs Place ID") stays true, and
Architecture.md is unchanged.

### GA-1 — Synchronous Google OAuth calls (REJECTED, AM-2, 2026-10-23)

The user rejected GA-1. Architecture.md line 61 is not amended, and
no synchronous token exchange or refresh exists in any request. The
code exchange runs in `exchange_oauth_code` (OD-11). The proposal's
text is not carried forward; this record is kept so it is not raised
again without fresh sign-off.

## Django apps

- **Created:** `google_reviews` (SAD.md §3).
- **Touched:**
  - `config` (settings, urls, Beat schedule);
  - `docs/` (new provider doc, API spec, the RL-1 edits; Architecture.md
    is not touched, because GA-1 was rejected).
- **Not touched:** `locations`, `accounts`, `auditlog`, `core`. They
  are used as they are.

## Models & database changes

Names and types are taken from `Data-Dictionary.md`. Every model
inherits `BaseModel` (UUID `id`, `created_at`, `updated_at`). All
three tables get `ENABLE` + `FORCE ROW LEVEL SECURITY`.

### `GoogleConnection` (table `google_reviews_googleconnection`): MERCHANT

| Field | Type | Notes |
|---|---|---|
| `merchant` | FK → `accounts.Merchant`, `PROTECT` | not null |
| `google_account_sub` | `CharField(255)` | **AM-1 (OD-4), new column, not in the Data Dictionary.** The ID token's `sub` (G-6 VERIFIED). Not null. Never returned by any API, logged or audited. |
| `google_account_email` | `CharField(254)` | not null (Data Dictionary). The ID token's `email` claim (G-6); refreshed on every reauthorization. Display only: never used as an identifier. |
| `access_token_enc` | `TextField` | Fernet ciphertext, not null |
| `refresh_token_enc` | `TextField` | Fernet ciphertext, not null |
| `expires_at` | `DateTimeField` | access-token expiry, not null |
| `status` | `CharField` choices `ACTIVE`, `NEEDS_REAUTH` | not null |
| `connected_at` | `DateTimeField` | nullable (Data Dictionary) |

- Manager: `TenantScopedManager` (`tenant_field = "merchant_id"`).
- RLS: `rls_direct("google_reviews_googleconnection")`.
- Uniqueness (AM-1, OD-4): `UNIQUE(merchant, google_account_sub)`.
  It is the reconnect key: a reauthorization of the same Google
  account updates the row (`google.connection_reauthorized`), and a
  different account creates one (`google.connection_added`). The upsert
  is `INSERT … ON CONFLICT (merchant_id, google_account_sub)`, never
  check-then-insert. The same Google account connected by two
  merchants gives two rows, one per merchant.
- Index: `(merchant, status)`.

### `GoogleOAuthState` (table `google_reviews_googleoauthstate`): MERCHANT

**APPROVED (OD-2, 2026-10-23).** It is not in the Data Dictionary and
is added there in this PR. It copies `EmbeddedSignupState` (spec 08
ES-18) and, under AM-1, also records one connect **attempt** of the
asynchronous exchange (GA-1 rejected, AM-2).

| Field | Type | Notes |
|---|---|---|
| `merchant` | FK → `accounts.Merchant`, `PROTECT` | |
| `user` | FK → `accounts.User`, `PROTECT` | the user who started the attempt; only that user can complete or read it |
| `state_hash` | `CharField(64)`, unique | sha256 of the plaintext state; the plaintext is never stored |
| `nonce_hash` | `CharField(64)` | **AM-2.** sha256 of the per-attempt OpenID Connect `nonce` sent in the authorization URL. The ID token's `nonce` claim must hash to it (G-15). Not null. |
| `status` | `CharField` choices `ISSUED`, `PENDING`, `COMPLETED`, `FAILED` | not null, default `ISSUED` |
| `code_enc` | `TextField`, nullable | The Fernet-encrypted authorization code, held only between `complete` and the task's "take code". |
| `failure_code` | `CharField` choices (fixed list below), nullable | set only with `FAILED` |
| `google_connection` | FK → `GoogleConnection`, `PROTECT`, nullable | set only with `COMPLETED` |
| `expires_at` | `DateTimeField` | `issued + GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS` |
| `completed_at` | `DateTimeField`, nullable | set on `COMPLETED` or `FAILED` |

- CHECK constraints (the database guarantees the safe shapes):
  - `code_enc IS NULL OR status = 'PENDING'`: a code can exist only
    while an exchange is pending;
  - `(status = 'FAILED') = (failure_code IS NOT NULL)`;
  - `status <> 'COMPLETED' OR google_connection_id IS NOT NULL`.
- Index: `(merchant, status, expires_at)`.
- RLS: `rls_direct`.
- `failure_code` values (fixed; no provider text is ever stored):
  `OAUTH_EXPIRED`, `EXCHANGE_FAILED`, `EXCHANGE_INTERRUPTED`,
  `SCOPE_MISSING`, `IDENTITY_MISSING`, `REFRESH_TOKEN_MISSING`, and
  (AM-2) `ID_TOKEN_INVALID` (a validation check failed) and
  `ID_TOKEN_UNVERIFIABLE` (Google's signing keys could not be
  fetched).
- Transitions are defined under "OAuth attempt lifecycle".

### `GoogleLocation` (table `google_reviews_googlelocation`): LOCATION (transitive)

| Field | Type | Notes |
|---|---|---|
| `location` | `OneToOneField` → `locations.Location`, `PROTECT` | `UNIQUE(location_id)` (Database-Design.md) |
| `google_connection` | FK → `GoogleConnection`, `PROTECT` | |
| `google_location_id` | `CharField(255)` | **OD-12 (accepted):** the full resource name `accounts/{accountId}/locations/{locationId}`. The v4 reviews call is addressed by exactly that parent (G-11), while the Business Information API names a location only `locations/{locationId}` (G-8). It is composed at mapping time from the `accounts/{accountId}` being listed and the location's `name`; each id segment is validated as non-empty with no `/`. **Never used to build a URL**, and never a Place ID. |
| `google_place_id` | `CharField(255)`, nullable | `metadata.placeId` (Data Dictionary: nullable). Under OD-3 every row written by this spec has one; the column stays nullable to match the Data Dictionary. Not used to build the review link (RL-1). |
| `review_link` | `URLField(2048)` | Not null (Data Dictionary). **RL-1 / OD-10 (b):** Google's `metadata.newReviewUri`, stored as returned once it passes `validate_review_link`. Google documents no maximum length, so 2048 is a ceiling, and a longer value is refused, not truncated. Never built from either identifier. |
| `average_rating` | `DecimalField(2,1)`, nullable | last synced value |
| `review_count` | `IntegerField`, nullable | last synced value |
| `last_synced_at` | `DateTimeField`, nullable | sync watermark |

- Manager: `TenantScopedManager`,
  `tenant_field = "location__merchant_id"` (the same pattern as
  `WhatsAppLocationMapping`).
- RLS: `rls_via_parent("google_reviews_googlelocation", "location_id",
  "locations_location")`.
- **Cross-merchant invariant (service layer, before insert or
  update):** `location.merchant_id == google_connection.merchant_id ==`
  the current merchant. PostgreSQL FK checks bypass RLS. Without this
  check, an insert under merchant A carrying merchant B's
  `google_connection_id` would pass the FK. This mirrors
  `TeamMemberLocation`.
- `UNIQUE(google_connection, google_location_id)` (OD-7, decided):
  one Business Profile location maps to at most one internal Location
  per connection. A second mapping attempt fails at the database and
  is answered `409 google_location_already_mapped`.

### `GoogleReview` (table `google_reviews_googlereview`): LOCATION (via `google_location`)

| Field | Type | Notes |
|---|---|---|
| `google_location` | FK → `GoogleLocation`, `PROTECT` | DB column `google_location_id` (the Data Dictionary name) |
| `google_review_id` | `CharField(255)` | |
| `reviewer_name` | `CharField(255)`, nullable | PII |
| `rating` | `SmallIntegerField`, CHECK 1–5 | not null |
| `text` | `TextField`, nullable | PII |
| `review_created_at` | `DateTimeField` | not null |
| `fetched_at` | `DateTimeField` | not null; set on every upsert |

- **`reply_text` and `reply_status` are NOT created.**
  **Status: V2 — Planned. V1 Implementation: NO.** The Data
  Dictionary allows the columns to exist ahead of time. This spec
  leaves them out so no V1 code path can write or expose them.
- Unique: `UNIQUE(google_location_id, google_review_id)`. This is the
  upsert key and the idempotency guarantee.
- Index: `(google_location_id, review_created_at)` (Database-Design.md).
- Manager: `TenantScopedManager`,
  `tenant_field = "google_location__location__merchant_id"`.
- RLS: `rls_via_parent("google_reviews_googlereview",
  "google_location_id", "google_reviews_googlelocation")`. It chains
  through the parent's own policy to `locations_location`.
- A review deleted upstream is kept locally (OD-8, decided). Sync is
  upsert-only and never deletes. The only deletion of synced reviews is
  the deliberate local remap in RM-1, never a sync.

### Review upsert (AM-1: field names verified)

Checked against the installed Django 5.2.17
(`django/db/models/query.py`, `bulk_create`;
`django/db/backends/postgresql/operations.py`,
`on_conflict_suffix_sql`). `bulk_create` resolves every name in
`unique_fields` and `update_fields` with `Model._meta.get_field(name)`.
A local check confirmed `get_field` resolves both the FK name and its
attname (`location` and `location_id` on `WhatsAppLocationMapping`) to
the same field. The SQL compiler then emits each field's **column**.
So the call is:

```python
GoogleReview.objects.bulk_create(
    rows,
    update_conflicts=True,
    unique_fields=["google_location", "google_review_id"],
    update_fields=["reviewer_name", "rating", "text",
                   "review_created_at", "fetched_at", "updated_at"],
)
```

and PostgreSQL receives
`ON CONFLICT("google_location_id", "google_review_id") DO UPDATE SET …`.

- The model's constraint must be exactly
  `UniqueConstraint(fields=["google_location", "google_review_id"],
  name="google_review_location_review_uniq")`: no `condition`, no
  expressions. PostgreSQL only accepts an `ON CONFLICT` target that
  matches a unique index, and a partial one would not match.
- `update_fields` never contains `id`, `created_at` or
  `google_location`. The primary key is never updated (Django refuses
  it), so a re-synced review keeps its UUID. `updated_at` is listed
  explicitly so that `auto_now` reaches the `DO UPDATE` (Django applies
  `pre_save` to the inserted values).
- Rows are deduplicated by `google_review_id` **within one call**
  before `bulk_create`. PostgreSQL rejects an `ON CONFLICT DO UPDATE`
  that touches the same row twice in one statement.
- `bulk_create` goes through the tenant-scoped default manager, so it
  runs inside the task's `tenant_atomic`, and RLS checks every inserted
  and updated row (`rls_via_parent`).
- Mapping from the v4 Review (G-11):
  - `google_review_id` = `reviewId`;
  - `rating` = `ONE`…`FIVE` → 1…5. A review with
    `STAR_RATING_UNSPECIFIED` is skipped, not stored (`rating` is not
    null), and counted in a log without its content;
  - `reviewer_name` = `reviewer.displayName` only when
    `reviewer.isAnonymous` is false, else NULL;
  - `text` = `comment` (may be absent → NULL);
  - `review_created_at` = `createTime`;
  - `fetched_at` = now.

  `reviewReply`, `updateTime` and `profilePhotoUrl` are not stored
  (V2 / not in the Data Dictionary).

### Migrations (one logical change each; non-destructive)

1. `0001_googleconnection` (with `google_account_sub` and its unique
   constraint) + `0002_googleconnection_rls`
2. `0003_googleoauthstate` (with its CHECK constraints) +
   `0004_googleoauthstate_rls`
3. `0005_googlelocation` + `0006_googlelocation_rls`
4. `0007_googlereview` + `0008_googlereview_rls`

Each reverses cleanly (`migrate google_reviews zero`).

## API endpoints

**Only `GET /reviews` exists in `API-Specification.md`.** The connect,
complete, location-list, mapping and read endpoints below were
**APPROVED with their roles under OD-5**. `GET
/google/oauth/attempts/{id}` was **APPROVED under OD-11 (AM-2)**. This
PR adds them to API-Specification.md under a new §Google.

`PUT /locations/{id}/google-location` stores the review link under
RL-1 (approved 2026-10-23).

All endpoints follow the General Conventions:
- error shape `{ error: { code, message, field_errors? } }`;
- UUIDs only;
- cursor pagination on lists;
- session + CSRF;
- `merchant_id` from the session, never from the request.

None of them ever returns an access or refresh token, an ID token, an
authorization code, or the plaintext state or nonce after issue.

**No endpoint makes a non-read-only Google call (GA-1 rejected).** The
only Google calls in a request are the read-only `accounts.list` /
`accounts.locations.list` calls of the location list and the mapping.
These are permitted by Architecture.md line 61.

**Session (dashboard), approved under OD-5 and OD-11:**

- `POST /google/oauth/start` — issues one single-use OAuth attempt
  (`ISSUED`, OD-2) and returns `200 { authorization_url }`.
  - The URL is `https://accounts.google.com/o/oauth2/v2/auth` (G-2),
    with `client_id`, the registered `redirect_uri`,
    `response_type=code`, `state`, `nonce` (AM-2, G-15),
    `scope=openid email https://www.googleapis.com/auth/business.manage`
    (G-5, G-6), `access_type=offline` and `prompt=consent` (G-7).
  - `state` and `nonce` are two independent
    `secrets.token_urlsafe(32)` values. Only their sha256 is stored.
  - `prompt=consent` is sent on **every** attempt, so a reauthorization
    gets a new refresh token (G-7: otherwise "only returned on the first
    authorization").
  - `include_granted_scopes` and `hd` are not sent.
  - Answers `503 google_not_configured` before storing anything when
    `GOOGLE_INTEGRATION_ENABLED` is off, or the client id/secret,
    redirect URI or `FERNET_KEY` is unset. No Google call.
  - Roles: OWNER, ADMIN.
- `POST /google/oauth/complete` — `{ code, state }`. Google redirects
  the browser to the **frontend** redirect URI (exact match, HTTPS:
  G-2). The frontend then POSTs here, so the side-effecting step is
  CSRF-protected and is not a backend `GET`.
  - In one conditional `UPDATE`, it claims the attempt
    (`ISSUED → PENDING`) and stores `code_enc = encrypt(code)`.
  - It enqueues `exchange_oauth_code` `on_commit`.
  - It makes **no Google call**.
  - Returns `202 { attempt_id, status: "PENDING" }`.
  - Errors:
    - `400 invalid_oauth_state`: missing, expired, already claimed, or
      another user's state, all with one generic response;
    - `422`: `code` missing or longer than 2048 characters;
    - `503 google_not_configured`.

    The outcome of the exchange is read from
    `GET /google/oauth/attempts/{id}`.
  - Roles: OWNER, ADMIN.
- `GET /google/oauth/attempts/{id}` — **APPROVED (OD-11).** Returns
  `{ id, status, failure_code | null, connection_id | null }`.
  - Readable **only by the user who started the attempt**. Anyone
    else, including another OWNER/ADMIN of the same merchant, gets
    `404`, the same as for an unknown or foreign id.
  - Before answering, the expire rules ("OAuth attempt lifecycle")
    run for that row, so a stale `ISSUED`/`PENDING` attempt is
    reported `FAILED`.
  - `failure_code` is one of the fixed values; no provider text.
  - Roles: OWNER, ADMIN. The frontend polls it after `complete`.
- `GET /google/connections` — the merchant's connections:
  `{ id, google_account_email, status, connected_at }`. This is what
  the dashboard `NEEDS_REAUTH` banner reads. `google_account_sub` is
  never returned. Roles: any role.
- `GET /google/connections/{id}/locations` — the Business Profile
  locations this connection can access, for the mapping UI:
  `{ google_location_id, title, has_place_id, has_review_link,
  mapped_location_id | null }`.
  - Built from `accounts.list` then `accounts.locations.list` for each
    account, with `readMask=name,title,metadata`, following every
    `nextPageToken` (G-8). `google_location_id` is the OD-12 full
    name.
  - Read-only provider calls only. **If the stored access token is
    expired or inside `GOOGLE_TOKEN_REFRESH_MARGIN_SECONDS`, the
    endpoint does not refresh it (GA-1 rejected).** It answers
    `409 google_token_refresh_pending` and enqueues one
    `refresh_connection_token(merchant_id, connection_pk)` task
    `on_commit` (OD-11, approved). The client retries afterwards.
  - Connection `NEEDS_REAUTH` → `409 google_needs_reauth`.
  - Roles: OWNER, ADMIN.
- `PUT /locations/{id}/google-location` — `{ google_connection_id,
  google_location_id }` maps or repoints a location.
  - The service re-reads the Google location from the provider list
    (read-only), never trusting a client-supplied Place ID or link.
    It then stores both identifiers and the validated `newReviewUri`
    as `review_link` (RL-1).
  - It enqueues the first sync `on_commit` (User-Flows §3 step 5).
  - Idempotent when unchanged: reviews and counters are kept. On a
    repoint, all three Google values are replaced together, and a
    changed `google_location_id` also deletes the old reviews (RM-1).
  - Errors:
    - `404`: an unknown or foreign location or connection (MANAGER: an
      unassigned location);
    - `422 google_place_id_missing`: no `metadata.placeId` (OD-3;
      nothing is written);
    - `422 google_review_link_missing`: no `metadata.newReviewUri`, or
      one that fails `validate_review_link` (AM-2; nothing is
      written);
    - `422`: an unknown `google_location_id` or a cross-merchant pair;
    - `409 google_location_already_mapped` (OD-7);
    - `409 google_needs_reauth`;
    - `409 google_token_refresh_pending`: the same rule as the location
      list;
    - `429 google_rate_limited`, `503 google_unavailable`,
      `502 google_request_failed`: provider failures (PE-1).

  **Remapping a location (RM-1).** The service locks the existing
  `GoogleLocation` row (`select_for_update()`). When the submitted
  `google_location_id` differs from the stored one, in the same
  transaction it deletes that mapping's `GoogleReview` rows, sets
  `average_rating`, `review_count` and `last_synced_at` to NULL, updates
  the mapping, and registers `sync_location` with `transaction.on_commit`.
  When it is unchanged, reviews and counters are preserved. A rollback
  keeps the old mapping and reviews. Only a structured log line with the
  mapping UUID and the deleted-row count is written; there is no
  `AuditLog` event. **Sync identity guard:** a sync captures the
  `google_location_id` it starts with. Before each page write it locks the
  mapping row and re-checks that identity, and the aggregate counters
  update is conditional on it; a changed mapping makes the sync write
  nothing. The remap and the sync write serialize on the row lock.

  **Provider errors on the request path (PE-1).** Only
  `GET /google/connections/{id}/locations` and
  `PUT /locations/{id}/google-location` translate them, in the service
  entry point: `GoogleRateLimited` -> `429 google_rate_limited`
  ("Google is limiting requests right now. Try again shortly.");
  `GoogleTransientError` -> `503 google_unavailable` ("Google is
  temporarily unavailable. Try again later."); `GooglePermanentError` ->
  `502 google_request_failed` ("Google could not complete the
  request."). The messages are fixed: no provider text, header or
  credential reaches a response. No `Retry-After` (Google documents
  none). The connection status is unchanged and a `PUT` writes and
  enqueues nothing. The provider classes and the background tasks are
  unchanged.

  Roles: OWNER, ADMIN, or a MANAGER assigned to the location (OD-5).
- `GET /locations/{id}/google-location` —
  `{ google_location: { id, google_location_id, google_place_id,
  review_link, average_rating, review_count, last_synced_at,
  connection_status } | null }`. Roles: any role; MANAGER `404` for
  an unassigned location.
- Unmapping (`DELETE /locations/{id}/google-location`) and
  connection disconnect are **out of V1 (OD-5, user decision
  2026-10-23)** and are not built. Repointing a location through `PUT`
  remains.

**Existing, documented:**

- `GET /reviews` — synced `GoogleReview` rows:
  `{ id, location_id, google_review_id, reviewer_name, rating, text,
  review_created_at }`.
  - Filters: `location_id`, `date_from`, `date_to`, `rating_min`.
  - Cursor pagination, newest `review_created_at` first.
  - **Never** includes an attribution field, `reply_text` or
    `reply_status`.
  - MANAGER: only reviews of assigned locations. A `location_id`
    filter on an unassigned or foreign location returns an empty page
    (OD-6, decided). It is never a `403` or `404` that would reveal
    whether the location exists.
  - Roles (session): any role.
  - **API key (OD-6, decided):** the view opts in explicitly for `GET`
    with scope `reviews:read`. A key is merchant-wide (no
    `TeamMember`, so no MANAGER scoping, as with `GET /sales`). A valid
    key without `reviews:read` is rejected by the existing
    `apikeys.permissions.HasApiKeyScope("reviews:read")`, which yields
    DRF's `403`: the same behaviour as `/sales` (`integrations/views.py`).
    An invalid key gets the documented generic `401 invalid_api_key`. A Bearer request is judged by the
    key alone, with no session fallback (Authentication.md §2). The
    API-key rate limits apply.

## Services & background tasks

### `google_reviews/providers/base.py`: `GoogleSyncProvider(ABC)`

The interface from SAD.md §6. It keeps the provider seam even with
one implementation, because the docs mandate it (Coding-Standards.md
§3). Providers do no database access, call no `services`, and never
log tokens, codes, emails, reviewer names or review text.

- `authorization_url(state: str, nonce: str) -> str` (G-2, G-5, G-7,
  G-15; parameters as under `POST /google/oauth/start`).
- `exchange_code(code: str) -> ExchangeResult`: `access_token`,
  `refresh_token | None`, `expires_at` (now + `expires_in`; G-3),
  `granted_scopes` (from `scope`), and the **raw `id_token`** string.
  - Claims are never read from an unvalidated token. The raw token is
    never stored, logged or passed outside the provider and
    `verify_id_token`.
  - Raises `GoogleTransientError` / `GooglePermanentError`.
  - Never retried (G-4 unverified, so a code is treated as
    single-use).
- `verify_id_token(id_token: str) -> IdTokenClaims` (AM-2, G-15):
  - performs the signature, `iss`, `aud` and `exp` checks under
    "ID-token validation";
  - returns `sub`, `email`, `email_verified` and `nonce`;
  - raises `IdTokenInvalid` or `IdTokenUnverifiable`.
- `refresh(refresh_token: str) -> TokenSet`: `access_token`,
  `refresh_token | None` (G-7: the documented refresh response has
  none), `expires_at`.
  - Raises `GoogleAuthRevoked` only for `invalid_grant` (G-10) and
    `admin_policy_enforced`.
  - Every other error is `GoogleTransientError` /
    `GooglePermanentError`, never revocation.
  - G-10 is PARTIAL: the exact error body is pinned by the contract
    fixture before this branch is written.
- `list_locations(access_token: str) -> list[ProviderLocation]`:
  - `google_location_id` is the OD-12 full name
    `accounts/{accountId}/locations/{locationId}`, composed from the
    account being listed and the location's `name`;
  - `title`;
  - `google_place_id | None` from `metadata.placeId`;
  - `review_uri | None` from `metadata.newReviewUri` (G-8, G-9).

  Follows `nextPageToken` for accounts and locations. Read-only.
- `list_reviews(access_token: str, google_location_id: str, page_token:
  str | None) -> ReviewPage` (reviews, `average_rating` from
  `averageRating`, `review_count` from `totalReviewCount`,
  `next_page_token`; `pageSize=50`, default order `updateTime desc`;
  G-11). `google_location_id` is used directly as the v4 `parent`
  (OD-12).
- `GoogleRateLimited` for HTTP `429` (G-12). It carries no
  `Retry-After`, because Google documents none; the caller stops
  until the next Beat tick.
- Removed by AM-1: `fetch_account_email`. The email comes from the
  validated ID token (G-6, G-15).

### `google_reviews/providers/google_business_profile.py`

`GoogleBusinessProfileProvider`, the single V1 implementation. HTTP
uses stdlib `urllib.request` with a fixed timeout, the same as
`billing/razorpay.py` and `MetaCloudProvider`. Endpoints, versions
and field names come only from the verified gate table. RS256
verification uses the already-installed `cryptography` package; no
JWT library is added.

### `google_reviews/services.py`

- `validate_review_link(uri: str) -> str` (AM-2, replaces
  `build_review_link`). It accepts the provider's `newReviewUri` only
  if:
  - it is a non-empty string of at most 2048 characters;
  - it parses as an absolute `https` URL with a host;
  - it has no userinfo and no fragment.

  It returns it unchanged; otherwise it raises `ReviewLinkMissing`
  (422). It never constructs or rewrites a URL. A host allowlist is
  **not** added: Google documents no host for the URI (G-9).
- `begin_google_oauth(*, actor) -> dict` — the config check (raises
  `GoogleNotConfigured`, 503), then issues the attempt and returns the
  authorization URL.
- `issue_oauth_state(*, actor) -> tuple[str, str]` — inserts an
  `ISSUED` attempt with `state_hash` and `nonce_hash`, and returns the
  plaintext state and nonce once, for the URL only. It first runs the
  merchant's housekeeping (the expire rules, then the purge; see "OAuth
  attempt lifecycle").
- `complete_google_oauth(*, actor, code, state) -> GoogleOAuthState` —
  claims the attempt and stores `code_enc = encrypt(code)` in one
  conditional `UPDATE` (raises `InvalidOAuthState`). Enqueues
  `exchange_oauth_code` `on_commit`. Makes no Google call.
- `exchange_attempt(attempt_id)` — the body of `exchange_oauth_code`:
  1. "take code";
  2. `exchange_code`;
  3. `verify_id_token`;
  4. the nonce check;
  5. the scope check;
  6. `finish_oauth_attempt`.

  Every Google call runs outside any transaction.
- `finish_oauth_attempt(attempt_id, *, result=None, failure_code=None)`
  — the `PENDING → COMPLETED/FAILED` transition. On success it runs
  `upsert_connection` first, in the same transaction ("Successful
  finish order").
- `get_oauth_attempt(*, actor, attempt_id) -> GoogleOAuthState` —
  applies the expire rules to that row, then returns it only when the
  attempt's `user` is the requesting user; otherwise it raises
  `NotFound` (404).
- `upsert_connection(*, user, result: VerifiedExchange) ->
  (GoogleConnection, created: bool)` — applies "Token handling",
  encrypts tokens, sets `ACTIVE` and `connected_at`, and writes
  `google.connection_added` or `google.connection_reauthorized` in the
  same transaction (actor = the attempt's user). Keyed on
  `UNIQUE(merchant, google_account_sub)` (OD-4).
- `list_available_locations(connection) -> list[ProviderLocation]` —
  request path, read-only.
  - `NEEDS_REAUTH` raises `GoogleNeedsReauth` (409).
  - An access token that is expired or inside the refresh margin
    raises `GoogleTokenRefreshPending` (409) and enqueues
    `refresh_connection_token` `on_commit`.
  - It **never refreshes** (GA-1 rejected).
- `map_location(*, location, connection, google_location_id, actor)
  -> GoogleLocation` — request path, read-only Google call.
  1. Enforces the three-way merchant invariant (raises
     `CrossMerchantMapping`, 422).
  2. Re-reads the location via `list_available_locations`.
  3. Requires the Place ID (OD-3, `422 google_place_id_missing`).
  4. Stores `review_link = validate_review_link(review_uri)`
     (`422 google_review_link_missing`).
  5. Upserts on `UNIQUE(location)` and enqueues `sync_location`
     `on_commit`.
- `refresh_connection(connection)` — task path only. Decrypts and
  refreshes when `expires_at` is within
  `GOOGLE_TOKEN_REFRESH_MARGIN_SECONDS` (G-3: lifetime comes only from
  `expires_in`), then persists under "Token handling". On
  `GoogleAuthRevoked` it calls `mark_needs_reauth`. Used by sync, the
  daily refresh and `refresh_connection_token`.
- `mark_needs_reauth(connection, *, refresh_token_enc_seen: str)` —
  conditional update
  `ACTIVE → NEEDS_REAUTH WHERE refresh_token_enc = refresh_token_enc_seen`.
  It never overwrites a reauthorization that committed meanwhile.
  Called only for `GoogleAuthRevoked`, never for a timeout, 5xx or rate
  limit. **No audit row (OD-9).** It emits one structured warning
  `google.connection_needs_reauth` carrying only the connection UUID
  and a fixed reason (`invalid_grant` or `admin_policy_enforced`):
  never a token, `sub`, email or provider text.
- `refresh_merchant_tokens()` — for each `ACTIVE` connection of the
  current merchant whose `expires_at` falls within the refresh
  horizon: `refresh_connection`.
- `sync_location_reviews(google_location_pk)` — skips when the
  connection is not `ACTIVE`. Otherwise it calls `refresh_connection`,
  then fetches every page (each provider call outside any
  transaction). It then upserts each page in its own short
  `tenant_atomic`, with the exact `bulk_create` call under "Review
  upsert", never `update_or_create`. Finally it updates
  `average_rating`, `review_count` and `last_synced_at`.
  - `GoogleRateLimited` ends the run quietly, with no retry and no
    `countdown`, and leaves `last_synced_at` unchanged so the next Beat
    tick resumes it.
  - A location the provider rejects (e.g. not verified: G-11) is
    skipped and logged by UUID. It never causes `NEEDS_REAUTH`.
- `sync_merchant_reviews()` — syncs every mapped location of the
  current merchant whose connection is `ACTIVE` and which is due
  (`last_synced_at` older than `GOOGLE_SYNC_INTERVAL_SECONDS`). Each
  location runs in its own short transactions. No provider call is
  made inside a transaction or row lock.
- `list_reviews(*, actor, filters) -> QuerySet[GoogleReview]` — scoped
  through `accessible_locations(actor)`.
- `merchant_ids_with_google() -> list[UUID]` — the GLOBAL `Merchant`
  read used by the Beat fan-out. The same pattern as
  `whatsapp.services.active_merchant_ids`.

### ID-token validation (AM-2, G-15)

HTTPS delivery from the token endpoint is **not** treated as proof of
validity. Google's page says such a token can be trusted, but also
that validation is required whenever the token is handed to another
component. ReviewFlow validates it fully before reading any claim. The
checks are those Google lists under "Validating an ID token", plus the
`nonce` from its authentication-request table. They run in
`verify_id_token` and `exchange_attempt`, in a task, never in a
request. Any failure ends the attempt `FAILED` with no connection
write, and the tokens are discarded unpersisted.

1. **Structure.** Three base64url segments. The header is JSON with
   `alg` and `kid`. `alg` must be exactly `RS256`, the only value in
   Google's discovery document `id_token_signing_alg_values_supported`.
   `none`, `HS256` and any other value → `ID_TOKEN_INVALID`.
2. **Signature.** Verify RS256 (RSASSA-PKCS1-v1_5 with SHA-256) over
   `header.payload` with the JWKS key whose `kid` matches. The JWKS is
   fetched from the discovery document's `jwks_uri`, recorded in G-15
   as `https://www.googleapis.com/oauth2/v3/certs` and held in the
   setting `GOOGLE_OIDC_JWKS_URI`.
   - Only keys with `kty = RSA`, `use = sig` and `alg = RS256` are
     considered.
   - Keys are cached **in process memory** for the response's
     `Cache-Control` `max-age` (Google: keys "can be cached using the
     HTTP response's cache directives"). They are not cached in Redis,
     which holds no business data, nor in the database.
   - An unknown `kid` forces one refetch, for rotation. A still-unknown
     `kid`, or a bad signature → `ID_TOKEN_INVALID`.
   - A JWKS fetch that fails (timeout, non-200, unparseable) →
     `ID_TOKEN_UNVERIFIABLE`. The exchange is not retried, because the
     code is spent.
   - The `tokeninfo` endpoint is **not** used: Google marks it for
     debugging only.
3. **Issuer.** `iss` must be exactly `https://accounts.google.com` or
   `accounts.google.com`.
4. **Audience.** `aud` must equal `GOOGLE_OAUTH_CLIENT_ID`.
   - If `aud` is a list, it is accepted only when it contains the
     client ID **and** `azp` equals the client ID.
   - Otherwise → `ID_TOKEN_INVALID`.
5. **Expiry.** Reject when `now >= exp` (Google: "must not be accepted
   on or after this time"). `exp` must be an integer. No clock-skew
   leeway is added, since Google specifies none.
6. **Nonce.** `nonce` must be present, and `sha256(nonce)` must equal
   the attempt's `nonce_hash` (constant-time compare). Google marks the
   request parameter required, and says to accept a given nonce only
   once. That holds because the attempt itself is single-use. A
   missing or mismatched nonce → `ID_TOKEN_INVALID`.
7. **Identity.** `sub` must be a non-empty string of at most 255
   characters (Google: "up to 255 ASCII characters"); otherwise →
   `IDENTITY_MISSING` (OD-4, no email fallback). `email` must be
   present for the not-null `google_account_email`; otherwise →
   `IDENTITY_MISSING`. `email_verified` is not required, because email
   is display-only and never an identifier.
8. **Hosted domain.** No `hd` parameter is sent, so no `hd` check
   applies (Google's check is conditional on sending it).

Only after all eight checks pass is `VerifiedExchange` built.
Validation failures log the attempt UUID and the failure code only,
never the token, header, claims, `kid` or key material.

### OAuth attempt lifecycle (AM-1)

One `GoogleOAuthState` row is one connect attempt. Every transition
is **one conditional `UPDATE` in its own short `tenant_atomic`**. The
affected-row count decides the outcome, so concurrent callers cannot
both win. Under READ COMMITTED, a second `UPDATE` waits for the first
to commit, then re-checks its `WHERE` and matches nothing. There is no
read-then-write anywhere.

```
            start                 complete (claim)
 (none) ─────────► ISSUED ─────────────────────────► PENDING ──► COMPLETED
                     │                                  │
                     │ expires_at passed                │ exchange/validation fails,
                     ▼                                  │ or expires_at passed
                   FAILED(OAUTH_EXPIRED)                ▼
                                                     FAILED(<code>)
```

| Transition | `WHERE` | `SET` |
|---|---|---|
| issue | (INSERT) | `ISSUED`, `state_hash`, `nonce_hash`, merchant, user, `expires_at` |
| claim (`complete`) | `state_hash = sha256(state)`, `user = actor`, `status = 'ISSUED'`, `expires_at > now()` | `PENDING`, `code_enc = encrypt(code)` |
| take code (`exchange_oauth_code`) | `id`, `status = 'PENDING'`, `code_enc = <the ciphertext just read>`, `expires_at > now()` (compare-and-swap: the affected-row count decides) | `code_enc = NULL` |
| finish ok | `id`, `status = 'PENDING'` | `COMPLETED`, `google_connection`, `completed_at`, `code_enc = NULL` |
| finish fail | `id`, `status = 'PENDING'` | `FAILED`, `failure_code`, `completed_at`, `code_enc = NULL` |
| expire (unclaimed) | `status = 'ISSUED'`, or `status = 'PENDING' AND code_enc IS NOT NULL`; and `expires_at <= now()` | `FAILED`, `OAUTH_EXPIRED`, `code_enc = NULL` |
| expire (in flight) | `status = 'PENDING' AND code_enc IS NULL`, `expires_at + GOOGLE_OAUTH_EXCHANGE_GRACE_SECONDS <= now()` | `FAILED`, `EXCHANGE_INTERRUPTED` |

- **Single use.** The claim is the consumption: a state can be
  claimed once. A replayed, expired, other-user or other-merchant state
  matches no row (RLS hides other merchants) and gets the one generic
  `400 invalid_oauth_state`.
- **The code leaves the database before Google sees it.** The
  task takes the code with the "take code" compare-and-swap `UPDATE` and
  commits, **then** calls Google outside any transaction. A crash after
  the take leaves no code at rest and cannot cause a second exchange.
- **Duplicate tasks.** A second `exchange_oauth_code` for the same
  attempt (redelivery, double enqueue) finds `code_enc IS NULL` or a
  terminal status, so "take code" matches nothing and the task returns
  without calling Google. The task has no automatic retry (`max_retries
  = 0`, no `autoretry_for`).
- **Failed exchanges are terminal.** A failed or uncertain exchange
  (timeout, 5xx, network error) is never retried with the same code
  (G-4: lifetime and reuse rules unverified). The attempt ends
  `FAILED` (`EXCHANGE_FAILED`), and the merchant starts a new attempt.
  No connection row is written or changed by a failed attempt.
- **Validation failures** after a successful exchange end the attempt
  `FAILED`, with no connection write and the tokens discarded
  unpersisted. They are checked in this order:
  1. `ID_TOKEN_UNVERIFIABLE` / `ID_TOKEN_INVALID`: any check under
     "ID-token validation" (AM-2), including the nonce;
  2. `IDENTITY_MISSING`: no valid `sub` or no `email`; no email
     fallback (OD-4);
  3. `SCOPE_MISSING`: `business.manage` is not in `granted_scopes`.
     The user can untick scopes on the consent screen;
  4. `REFRESH_TOKEN_MISSING`: see "Token handling".
- **Expiry.** `expires_at = issue + GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS`
  (default 600). The expire rule runs:
  - at the start of `issue_oauth_state`, for the merchant;
  - before `GET /google/oauth/attempts/{id}` answers;
  - in the daily `refresh_google_tokens` fan-out.

  The claim and the take-code `WHERE` re-check expiry themselves, so
  correctness never depends on the sweep. `COMPLETED`/`FAILED` stay
  terminal: no `UPDATE` matches them.
- **No expiry during an exchange.** A `PENDING` row with no code is
  an exchange in flight: the task after "take code". It expires only after `GOOGLE_OAUTH_EXCHANGE_GRACE_SECONDS`
  (default `GOOGLE_HTTP_TIMEOUT_SECONDS + 60`) past `expires_at`. An
  exchange that starts at the last second can therefore always finish
  before the sweep or the poll endpoint can fail it.
- **Successful finish order.** In **one** `tenant_atomic`:
  1. upsert the connection and write its audit row;
  2. run the conditional "finish ok" `UPDATE`.

  If that `UPDATE` matches zero rows (the attempt is no longer
  `PENDING`), raise, so the connection write and audit row roll back.
  A connection is never left behind an attempt the user sees as
  `FAILED`.
- **Finish after a crash.** If a worker dies between "take code" and
  "finish", the row stays `PENDING`
  without a code until `expires_at` plus the grace period, then
  expires to `EXCHANGE_INTERRUPTED`. If the exchange had succeeded at Google, the
  tokens are lost and the merchant reconnects. That is the accepted,
  safe outcome.
- **Publish failure.** If the broker publish after `complete` fails,
  the row keeps its encrypted code until `expires_at`. It then expires
  to `OAUTH_EXPIRED`, which clears the code.
- **Purge.** Terminal rows older than
  `GOOGLE_OAUTH_ATTEMPT_RETENTION_SECONDS` (default 86400) past
  `expires_at` are deleted by `issue_oauth_state` and the daily
  fan-out. A row referenced as `google_connection` is deleted
  normally; the FK is from the attempt to the connection, not the
  reverse.
- **Logging.** State, nonce, code, tokens, ID token, `sub` and email
  are never logged.
  Logs carry the attempt UUID, the transition and the `failure_code`
  only.

### Token handling (AM-1)

- **Every exchange requests a refresh token.** `access_type=offline`
  and `prompt=consent` are always sent (G-7).
- **When the exchange returns no `refresh_token`:**
  - a connection already exists for `(merchant, sub)` and is
    `ACTIVE`: keep its stored `refresh_token_enc` unchanged. Update the
    access token, `expires_at`, email and `connected_at`. The attempt
    is `COMPLETED` (`google.connection_reauthorized`).
  - a connection exists for `(merchant, sub)` but is `NEEDS_REAUTH`:
    its stored refresh token is presumed dead, so keeping it would
    produce a falsely `ACTIVE` connection. The attempt ends `FAILED`
    (`REFRESH_TOKEN_MISSING`), and the connection is left unchanged.
  - no connection exists for `(merchant, sub)`: the attempt ends
    `FAILED` (`REFRESH_TOKEN_MISSING`). No row is created, because
    `refresh_token_enc` is not null. An empty or placeholder refresh
    token is never written.
- **When it returns one:** the new token replaces the stored one in
  the same transaction that sets `ACTIVE`.
- **On refresh:**
  - the response's `access_token` and `expires_at` are persisted;
  - if the response carries a `refresh_token` (undocumented in G-7's
    sample, but not excluded), it replaces the stored one in the same
    `UPDATE`; if not, the stored one is kept;
  - the persisting `UPDATE` is conditional on
    `status = 'ACTIVE' AND refresh_token_enc = <the value read>`, so a
    refresh never overwrites a reauthorization that committed during
    the call. A zero-row result discards the refreshed token.
- **Concurrent refreshes** of one connection (the daily task and a
  sync) may both succeed. Either result is a valid token, and the
  conditional `UPDATE` keeps the row consistent. `NEEDS_REAUTH` is set
  only through `mark_needs_reauth`'s same condition.
- **Development note (G-10):** while the OAuth client is in "Testing",
  Google's refresh tokens expire after 7 days. Weekly `NEEDS_REAUTH`
  in development is expected, not a bug.

### Celery tasks (`google_reviews/tasks.py`), all on queue `google_sync`

Every tenant task takes `merchant_id` explicitly, via `@tenant_task`.

- `sync_google_reviews()` — Beat. Fans out one
  `sync_merchant_google_reviews(merchant_id)` per merchant. Takes no
  `merchant_id` and reads only `Merchant` ids.
- `sync_merchant_google_reviews(merchant_id)` →
  `sync_merchant_reviews()`.
- `sync_location(merchant_id, google_location_pk)` — the first sync
  after mapping.
- `refresh_google_tokens()` — daily Beat. Fans out
  `refresh_merchant_google_tokens(merchant_id)` →
  `refresh_merchant_tokens()`, plus the attempt expire rule and purge.
- `exchange_oauth_code(merchant_id, attempt_pk)` — **APPROVED (OD-11,
  AM-2)** → `exchange_attempt`. It takes the code with the "take code"
  `UPDATE` (never passed as an argument), exchanges it outside any
  transaction, validates the ID token, then finishes the attempt.
  `max_retries = 0`, no `autoretry_for`.
- `refresh_connection_token(merchant_id, connection_pk)` — enqueued
  `on_commit` by the `409 google_token_refresh_pending` path (OD-11,
  AM-2) → `refresh_connection`. Safe to run twice: the refresh is
  conditional, and a token outside the margin is left alone.

Every task is safe to run twice: the upsert is idempotent, refresh
is conditional, and an attempt is claimed and its code taken once.

### Celery Beat (`config/settings.py`)

- `"google-sync-reviews"`: `google_reviews.tasks.sync_google_reviews`
  every `GOOGLE_SYNC_BEAT_SECONDS` (default 900). A location is synced
  when `last_synced_at` is older than `GOOGLE_SYNC_INTERVAL_SECONDS`
  (default 2700, inside the documented 30–60 min). That gives the
  stagger SAD.md §5 asks for without broker scheduling.
- `"google-refresh-tokens"`:
  `google_reviews.tasks.refresh_google_tokens` every 86400 s (SAD.md
  §5). Because access tokens may be short-lived (G-3), sync also
  refreshes just before calling (`refresh_connection`). The daily
  task alone may not keep sync working.

### Settings (env only; empty defaults fail closed)

- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` (secret: never
  stored, logged, audited or returned)
- `GOOGLE_OAUTH_REDIRECT_URI`
- `GOOGLE_INTEGRATION_ENABLED` (default `False`)
- `GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS` (default 600)
- `GOOGLE_OAUTH_ATTEMPT_RETENTION_SECONDS` (default 86400)
- `GOOGLE_OAUTH_EXCHANGE_GRACE_SECONDS` (default
  `GOOGLE_HTTP_TIMEOUT_SECONDS + 60`)
- `GOOGLE_TOKEN_REFRESH_MARGIN_SECONDS` (default 300; G-3 gives no
  fixed lifetime, so the margin is applied to `expires_at` from
  `expires_in`)
- `GOOGLE_SYNC_BEAT_SECONDS`, `GOOGLE_SYNC_INTERVAL_SECONDS`
- `GOOGLE_HTTP_TIMEOUT_SECONDS`
- `GOOGLE_OIDC_JWKS_URI` (default `https://www.googleapis.com/oauth2/v3/certs`,
  the discovery document's `jwks_uri`, G-15). A public URL, not a secret.

## Admin

No admin changes. `google_reviews/admin.py` registers no model and
carries a docstring saying why. This is the same precedent as
`whatsapp/admin.py`: all four models are tenant-scoped and need a
tenant context. A staff viewer, if wanted, would go through the
audited `staff_merchant_read` path (Phase 16) in a follow-up. Token
columns are never shown in any admin.

## Files to change

- `config/settings.py` — `INSTALLED_APPS`, Google settings, two Beat
  entries
- `config/urls.py` — include `google_reviews.urls`
- `.env.example` — the Google variables, empty
- `docs/04-api/API-Specification.md` — new §Google with the
  endpoints approved under OD-5 and OD-11 (including
  `GET /google/oauth/attempts/{id}` and the
  `409 google_token_refresh_pending` / `422 google_review_link_missing`
  errors); `GET /reviews` auth/roles/response detail, and the
  `reviews:read` API-key opt-in (OD-6)
- `docs/03-database/Data-Dictionary.md`:
  - `GoogleOAuthState`, with `nonce_hash` (OD-2, AM-2);
  - `GoogleConnection.google_account_sub` and
    `UNIQUE(merchant, google_account_sub)` (OD-4);
  - `UNIQUE(google_connection, google_location_id)` on
    `GoogleLocation` (OD-7);
  - the §GoogleLocation notes for `google_location_id` (OD-12),
    `google_place_id` and `review_link` (RL-1 item 3).

  `review_link` stays not null.
- **RL-1 locked-document edits, exact text under RL-1, applied only
  with the user's sign-off:**
  - `docs/FINAL-ARCHITECTURE-REVIEW.md` §7;
  - `docs/06-automation/Google-Reviews.md` §Flow and §Review Link;
  - `docs/03-database/Database-Design.md` §GoogleLocation;
  - `docs/10-development/Testing-Strategy.md` §Google identifiers;
  - `docs/ROADMAP.md` §09 bullet;
  - `CLAUDE.md` Warnings;
  - `.claude/agents/test-writer.md` line 116.
- `docs/02-architecture/Architecture.md` — **not changed** (GA-1
  rejected)
- `docs/09-security/Audit-Logging.md` — the exact action names
  `google.connection_added` / `google.connection_reauthorized` and
  their metadata
- `docs/09-security/Security-Controls.md` — the new Google env vars in
  the secrets list
- `docs/ROADMAP.md` — §09 status (via `/ship-feature`)
- `CLAUDE.md` — the Current State app list (via `/ship-feature`)

## Files to create

- `google_reviews/__init__.py`, `apps.py`, `models.py`,
  `exceptions.py`, `services.py`, `tasks.py`, `serializers.py`,
  `views.py`, `urls.py`, `admin.py`
- `google_reviews/providers/__init__.py`, `base.py`,
  `google_business_profile.py`
- `google_reviews/migrations/0001`–`0008` (above)
- `google_reviews/tests/fixtures/` — anonymized payloads in the
  documented shapes (G-14): token response, error responses, location
  list (with and without `placeId` / `newReviewUri`), review list, and
  a JWKS. ID tokens are signed in the tests with a test RSA key whose
  public half is served as the fixture JWKS; no Google key material is
  committed.
- `google_reviews/tests/test_models_rls.py`, `test_oauth.py`,
  `test_id_token.py`, `test_mapping.py`, `test_sync.py`,
  `test_tokens.py`, `test_reviews_api.py`, `test_provider_contract.py`,
  `test_tenant_isolation.py`
- `docs/05-integrations/Google-Business-Profile.md` — the verified gate
  table (URL + date per item). **Created by AM-1 (2026-10-23)**;
  implementation updates it as open items are verified.

## New dependencies

No new dependencies. HTTP uses stdlib `urllib.request` (as in
`billing/razorpay.py` and `whatsapp/providers/meta_cloud.py`).
Encryption uses the existing `cryptography` via `core.crypto`. RS256
ID-token verification also uses `cryptography` (RSA public key from
the JWKS `n`/`e`, PKCS#1 v1.5, SHA-256), with stdlib `base64` and
`json`. No JWT library and no Google client library is added.

## Google verification gate

FINAL-ARCHITECTURE-REVIEW.md lists "verification of current … Google
provider requirements during adapter implementation" as remaining
work. Each item is verified against current Google documentation and
recorded with URL and date in
`docs/05-integrations/Google-Business-Profile.md` before the code it
affects is written. **This spec asserts only what that file records**;
the findings are quoted there, not here.

Status after AM-1 and AM-2 (all read 2026-10-23):

| # | To verify | Status | Affects |
|---|---|---|---|
| G-1 | API access approval, default quota | VERIFIED | Production-readiness gate; dev testing (0 QPM until approved) |
| G-2 | OAuth endpoints, redirect-URI rules, `state` check | VERIFIED | `authorization_url`, `exchange_code`, `refresh` |
| G-3 | Access-token lifetime | PARTIAL (only `expires_in`; no fixed lifetime) | `refresh_connection` margin |
| G-4 | Authorization-code lifetime | **UNVERIFIED** | No longer blocking (GA-1 rejected). Its effect: a code that expires while queued ends `EXCHANGE_FAILED`, and the merchant restarts |
| G-5 | Minimum scopes | VERIFIED (`business.manage`) | `authorization_url` |
| G-6 | Stable account id and email | VERIFIED (ID token `sub`, `email`; `id_token` is in the token-endpoint response of the OIDC server flow) | OD-4, `google_account_sub` |
| G-7 | Refresh-token issuance, re-consent | PARTIAL (first authorization only; `prompt=consent` for a new one; the refresh sample has none, but no statement that a refresh never returns one) | Token handling |
| G-8 | Account and location listing | VERIFIED | `list_locations` |
| G-9 | Place ID field and absence | PARTIAL (`metadata.placeId`, populated if the location appears on Maps) | OD-3 |
| G-10 | Revocation vs transient errors; testing-mode expiry | PARTIAL (`invalid_grant`, causes, 7-day testing expiry; exact error body open) | `GoogleAuthRevoked`, `NEEDS_REAUTH` |
| G-11 | Reviews list and Review resource | VERIFIED | `list_reviews`, Review upsert, OD-12 |
| G-12 | Rate limits | PARTIAL (`429`; backoff advised; **no `Retry-After` documented**) | `GoogleRateLimited` |
| G-13 | Review-link format `writereview?placeid=` | **UNVERIFIED**, and no longer used (OD-10 (b): `newReviewUri`) | nothing |
| G-14 | Official sample payloads | PARTIAL (documented shapes only; no full samples) | contract fixtures |
| G-15 | ID-token validation (AM-2) | VERIFIED (RS256 via the `jwks_uri` keys; `iss`; `aud`; `exp`; `nonce` required in the request table and accepted once; `tokeninfo` for debugging only) | "ID-token validation" |

## Production-readiness gates (the flag stays off until all pass)

`GOOGLE_INTEGRATION_ENABLED` defaults to `False`. With it off,
`POST /google/oauth/start` answers `503 google_not_configured`. Sync
and refresh also do nothing, because no connection can exist. It is
turned on only when:

- G-1 API access is granted for the production project (its quota is
  no longer 0 QPM). Google's eligibility rule applies to ReviewFlow's
  own application: the applicant must manage a Business Profile that
  is verified and active for 60+ days;
- the OAuth consent screen is published and verified, so production
  refresh tokens do not get the 7-day testing expiry (G-10);
- the RL-1 document edits (approved 2026-10-23) are merged;
- the first real connection on the development OAuth client confirms
  that Google echoes `nonce` in the ID token of the code flow. G-15's
  source is inconsistent about whether `nonce` is required, and a
  missing echo would fail every connection with `ID_TOKEN_INVALID`.
  The result is recorded in `Google-Business-Profile.md` as a live
  observation, like the L-series in `WhatsApp-Meta.md`;
- every gate item the shipped code depends on, other than G-4 and
  G-13 (neither of which the code relies on any longer), is recorded
  as VERIFIED, or its PARTIAL gap is pinned by a contract fixture.

## Rules for implementation

- Django + DRF monolith. Business logic lives only in
  `google_reviews/services.py`: views, serializers and tasks call
  services. Providers hold no business logic and no DB access.
- Every tenant-owned model uses `core.TenantScopedManager` (with the
  `tenant_field` paths above), and RLS is enabled **and forced** on
  all four tables.
- Never trust a client-supplied `merchant_id` or `location_id`:
  - the merchant comes from the session;
  - the location comes from the URL via `get_accessible_location`;
  - the connection is read under the session's tenant context;
  - the Place ID and the review link always come from the provider,
    never from the request.
- `GoogleLocation` is inserted only after the service checks
  `location.merchant_id == google_connection.merchant_id ==` the
  current merchant.
- Celery tasks take `merchant_id` explicitly and set the tenant
  context first (`@tenant_task`). The two Beat fan-out tasks read only
  GLOBAL `Merchant` ids.
- Idempotency comes from database unique constraints and conditional
  updates, never check-then-insert:
  - `UNIQUE(google_location_id, google_review_id)` with
    `ON CONFLICT DO UPDATE`, using exactly the call under "Review
    upsert";
  - `UNIQUE(location_id)`;
  - `UNIQUE(google_connection, google_location_id)` (OD-7);
  - `UNIQUE(state_hash)`;
  - `UNIQUE(merchant, google_account_sub)` (OD-4);
  - every OAuth attempt transition is the conditional `UPDATE` in its
    table;
  - `NEEDS_REAUTH` only from `ACTIVE` and only while the refresh token
    is the one read.
- No fallback from `google_account_sub` to email, ever (OD-4). No
  placeholder refresh token, ever ("Token handling").
- An authorization code is never retried, never logged, never in a
  task argument, and at rest only as Fernet ciphertext while `PENDING`.
- **ID tokens:** no claim is read before all eight checks under
  "ID-token validation" pass. `alg` is RS256 only. The `tokeninfo`
  endpoint is never used. JWKS keys are cached in process memory only.
  The raw ID token is never stored or logged.
- RL-1's document edits are applied verbatim, items 1–8 only.
  `Architecture.md` is not touched.
- The nonce production gate stays open until a live observation is
  recorded. Nothing may treat Google's `nonce` echo as confirmed
  before then.
- **Never substitute `google_place_id` for `google_location_id`, or the
  reverse, and never build a review link from either.** The link is
  only the validated `newReviewUri` (RL-1).
- No provider call inside a database transaction or row lock. **No
  synchronous non-read-only Google call in any request (GA-1
  rejected):** token exchange, token refresh and JWKS fetch run only in
  Celery tasks. A request that would need a refresh answers
  `409 google_token_refresh_pending`.
- Rate limit: stop and resume on the next Beat tick. No
  `retry(countdown=…)`, no `eta`, no busy-retry.
- `NEEDS_REAUTH` is set only for the verified revocation error (G-10),
  never for a timeout, 5xx or rate limit. It never touches WhatsApp,
  campaigns or sending.
- Role checks use DRF permission classes (`IsMerchantMember`,
  `IsOwnerOrAdmin`, `IsOwnerAdminOrManager` + `accessible_locations`),
  with no inline role `if`s.
- External IDs are UUIDs.
- Secrets come from env only. Tokens are Fernet-encrypted at rest,
  never logged, audited, returned or placed in a task argument. The
  authorization code, the ID token and the plaintext state and nonce
  are never stored in plaintext, logged or put in a task argument.
- Never log tokens, codes, `google_account_email`, reviewer names or
  review text.
- Status enums are `UPPER_SNAKE_CASE`, and timestamps end in `_at`.
- **No V2:**
  - no `reply_text`/`reply_status` columns or code;
  - no reply API;
  - no sentiment or summaries;
  - no attribution field anywhere.

  No bespoke admin app. Nothing creates, edits or posts a review.
- Do not implement any item behind an unresolved **blocking** Open
  Decision until the user settles it, and do not decide it in a plan.
- Every new service function has a unit test. Every provider method
  has a fixture-based contract test using an anonymized official
  Google payload (G-14).
- Mock only the Google HTTP boundary (the provider's `urllib` call),
  never the services under test.

## Definition of done

Run with `pytest` against real PostgreSQL, with Celery eager where a
task boundary is crossed, unless stated otherwise.

### Schema & RLS

- [ ] `python manage.py migrate` applies `google_reviews`
      0001–0008. `migrate google_reviews zero` reverses them.
- [ ] RLS is enabled and forced on all four tables (checked via
      `pg_class.relrowsecurity` / `relforcerowsecurity`).
- [ ] `GoogleReview` has no `reply_text` or `reply_status` column.
- [ ] Tenant isolation, both layers: under merchant A, the manager and
      a raw `SELECT` each return zero of merchant B's
      `GoogleConnection` / `GoogleLocation` / `GoogleReview` /
      `GoogleOAuthState` rows. A raw `INSERT` of a `GoogleReview` under
      B's `GoogleLocation` fails RLS.
- [ ] Inserting the same `(google_location_id, google_review_id)` twice
      raises `IntegrityError`. `UNIQUE(location_id)` and
      `UNIQUE(google_connection, google_location_id)` hold on
      `GoogleLocation`. `UNIQUE(merchant, google_account_sub)` holds on
      `GoogleConnection`, while the same `sub` under two merchants is
      allowed.
- [ ] `GoogleOAuthState` CHECK constraints reject a non-NULL
      `code_enc` outside `PENDING`, `FAILED` without a
      `failure_code` (and the reverse), and `COMPLETED` without a
      connection.
- [ ] Pooled-connection test: after a tenant transaction ends, a new
      transaction with no context sees no Google rows.

### OAuth & connection

- [ ] `POST /google/oauth/start` with the flag off, or any required
      setting unset, → `503 google_not_configured`, and no state row is
      written.
- [ ] `start` stores only `sha256(state)` and `sha256(nonce)` in an
      `ISSUED` row. Both plaintexts appear only in the response URL.
      The URL carries `scope=openid email …/auth/business.manage`,
      `access_type=offline`, `prompt=consent`, `state` and `nonce`.
- [ ] Roles: MANAGER and VIEWER get `403` on `start`, `complete` and
      `GET /google/oauth/attempts/{id}`.
- [ ] `complete` makes **no Google call**: the HTTP mock records zero
      requests during the request. It returns
      `202 { attempt_id, status: "PENDING" }` and enqueues exactly one
      `exchange_oauth_code` after commit (none on rollback).
- [ ] `complete` with an expired, reused, other-user or other-merchant
      state → the identical `400 invalid_oauth_state`. Two concurrent
      completes with one state → exactly one claims it
      (`ISSUED → PENDING`).
- [ ] A successful exchange stores Fernet ciphertext (the plaintext
      token is not in the DB), sets `ACTIVE` and `connected_at`, ends
      the attempt `COMPLETED`, and writes `google.connection_added`. A
      reauthorization with the same `sub` writes
      `google.connection_reauthorized` and does not create a second
      row. A different `sub` creates a new row. A changed email with
      the same `sub` updates the email on the same row.
- [ ] Token handling: an exchange without `refresh_token`:
      - keeps the stored token of an `ACTIVE` connection (`COMPLETED`);
      - fails `REFRESH_TOKEN_MISSING` for a `NEEDS_REAUTH` connection
        (connection unchanged) and for a new account (no row).
- [ ] Missing `business.manage` in the granted scopes →
      `SCOPE_MISSING`. No `sub` or no `email` → `IDENTITY_MISSING`.
      Neither writes a connection, and there is no email fallback.
- [ ] A failed exchange (timeout, 5xx, `invalid_grant` for an expired
      code) ends `FAILED` (`EXCHANGE_FAILED`), is not retried, and
      writes no connection.
- [ ] Expiry: an `ISSUED` or code-holding `PENDING` attempt past
      `expires_at` cannot be claimed or taken, and expires to
      `OAUTH_EXPIRED` with the code cleared. A `PENDING` attempt with
      no code (exchange in flight) is not expired before `expires_at +
      GOOGLE_OAUTH_EXCHANGE_GRACE_SECONDS`, then becomes
      `EXCHANGE_INTERRUPTED`. Terminal rows never change.
- [ ] Finish race: if the attempt stops being `PENDING` before the
      successful finish, the "finish ok" `UPDATE` matches zero rows and
      the connection upsert and audit row roll back.
- [ ] The code is at rest only as ciphertext while `PENDING`, and is
      cleared by "take code" before the Google call. A second
      `exchange_oauth_code` for the same attempt makes no Google call.
      A task that dies after "take code" leaves no code and expires to
      `EXCHANGE_INTERRUPTED`. A broker publish failure leaves the
      attempt to expire to `OAUTH_EXPIRED` with the code cleared.
- [ ] `GET /google/oauth/attempts/{id}`: the starting user sees
      `status`, `failure_code` and `connection_id`. Another OWNER/ADMIN
      of the same merchant, and any other merchant, get `404`. A stale
      `ISSUED`/`PENDING` attempt is reported `FAILED` with the right
      code.
- [ ] No token, ID token, code, state, nonce, `sub` or email appears in
      any log record, audit metadata, task argument or response body.
      Asserted with `caplog` and broker-call inspection.

### ID-token validation (AM-2)

Run against a test RSA key served as the fixture JWKS; no network.

- [ ] A correctly signed token with valid `iss`, `aud`, `exp` and
      `nonce` passes, and the connection is written.
- [ ] Each of these ends the attempt `FAILED (ID_TOKEN_INVALID)` with
      no connection write:
      - `alg` `none`;
      - `alg` `HS256` (including one "signed" with the public key as an
        HMAC secret);
      - a tampered payload;
      - a signature by a key not in the JWKS;
      - an unknown `kid` still unknown after one refetch;
      - `iss` `https://evil.example`;
      - `aud` of another client;
      - a list `aud` without `azp == client_id`;
      - `now == exp` and `now > exp`;
      - a missing `nonce`;
      - the nonce of another attempt.
- [ ] Both accepted issuers (`https://accounts.google.com`,
      `accounts.google.com`) pass.
- [ ] A JWKS fetch failure → `FAILED (ID_TOKEN_UNVERIFIABLE)`, and the
      exchange is not retried.
- [ ] JWKS caching: two validations within `max-age` make one JWKS
      request. An unknown `kid` triggers exactly one refetch.
- [ ] `tokeninfo` is never called (the HTTP mock asserts the URL set).

### Location list without in-request refresh (OD-11)

- [ ] With an access token inside the refresh margin,
      `GET /google/connections/{id}/locations` and the mapping `PUT`
      answer `409 google_token_refresh_pending`. They make **no** call
      to the token endpoint, and enqueue one `refresh_connection_token`
      after commit.
- [ ] With a fresh token, the list makes only `accounts.list` /
      `accounts.locations.list` `GET` calls.

### Mapping & review link


- [ ] Mapping stores `google_location_id` as
      `accounts/{accountId}/locations/{locationId}` (OD-12),
      `google_place_id` from `metadata.placeId`, and `review_link`
      equal to the provider's `metadata.newReviewUri`, unchanged. These
      are three separate values, none derived from another.
- [ ] No `metadata.placeId` → `422 google_place_id_missing`. No
      `metadata.newReviewUri`, a non-`https` one, one with userinfo or a
      fragment, or one over 2048 characters →
      `422 google_review_link_missing`. Nothing is written in either
      case.
- [ ] No code path builds a link from `google_location_id` or
      `google_place_id` (no `writereview` string in the app code; a
      grep-based test).
- [ ] A client-supplied Place ID or link in the request body is
      ignored.
- [ ] Sync calls the v4 reviews endpoint with the stored
      `google_location_id` as `parent` verbatim.
- [ ] Mapping a second internal Location to the same Google location
      under one connection → `409 google_location_already_mapped`,
      enforced by the constraint (OD-7).
- [ ] Mapping with another merchant's connection or location → `404`
      or `422` per the endpoint section, and no row is written. The
      cross-merchant invariant test inserts directly through the
      service with mismatched merchants and expects rejection before
      insert.
- [ ] MANAGER: may map an assigned location; gets `404` for an
      unassigned one. VIEWER: `403`.
- [ ] A successful mapping enqueues exactly one first sync after
      commit. On rollback, nothing is enqueued.

### Sync

- [ ] Syncing the same provider page twice yields the same number of
      `GoogleReview` rows (re-sync does not duplicate:
      Testing-Strategy.md, Google integration tests). An edited review
      updates in place, keeps its `id` and `created_at`, and refreshes
      `fetched_at` and `updated_at`.
- [ ] A page carrying the same `reviewId` twice upserts once, without
      the PostgreSQL "cannot affect row a second time" error.
- [ ] `STAR_RATING_UNSPECIFIED` is skipped. An anonymous reviewer is
      stored with `reviewer_name` NULL. `ONE`…`FIVE` map to 1…5.
- [ ] A review absent from a later sync is still stored (OD-8).
- [ ] Take code (real PostgreSQL): a stale-read interleaving gives the
      loser 0 rows and no provider call; N barrier-started threads give
      one winner and one `exchange_code` call; a sequential duplicate
      delivery makes one call; the loser changes nothing; an expired,
      non-`PENDING` or code-less attempt makes no call; `code_enc` is
      NULL in the database when the provider is called.
- [ ] Provider errors (PE-1): each class gives the exact status, code
      and fixed body on both endpoints, with no provider text, the
      connection `ACTIVE`, no mapping written, no `delay` on `PUT`, and
      the call made with `in_atomic_block` False.
- [ ] Remap (RM-1): A to B deletes only that mapping's reviews and resets
      the counters; the next sync leaves only B's reviews; a re-`PUT` of
      the same id keeps them; a block-B failure keeps the old mapping
      and reviews; a sync mid-page when the remap commits writes
      nothing afterwards (reviews and counters); a cross-tenant remap
      deletes nothing; `sync_location` is enqueued once, after commit.
- [ ] `average_rating`, `review_count` and `last_synced_at` are
      updated after a successful sync.
- [ ] A location of a `NEEDS_REAUTH` connection is not synced, and no
      provider call is made.
- [ ] An unmapped location is never synced.
- [ ] A rate-limited response ends the run without a retry or
      `countdown`. The next run resumes. No `NEEDS_REAUTH` is set.
- [ ] `sync_merchant_google_reviews(merchant_id=A)` cannot read or
      write merchant B's rows, even when given B's
      `google_location_pk` (Celery tenant context, Multi-Tenancy.md
      test 3).
- [ ] The Beat fan-out enqueues one task per merchant, with an
      explicit `merchant_id`, on queue `google_sync`.

### Tokens

- [ ] `refresh_merchant_tokens` refreshes connections inside the
      horizon and persists new ciphertext and `expires_at`.
- [ ] `invalid_grant` → `NEEDS_REAUTH`, plus one structured warning
      with only the connection UUID and reason. No `AuditLog` row
      (OD-9). A timeout, 5xx or `429` → status unchanged.
- [ ] A refresh response without `refresh_token` keeps the stored
      one; with one, it replaces it. A refresh whose read
      `refresh_token_enc` no longer matches (a reauthorization
      committed meanwhile) writes nothing and does not set
      `NEEDS_REAUTH`.
- [ ] `NEEDS_REAUTH` has no effect on any WhatsApp or sender row
      (sending is unaffected).
- [ ] `GET /google/connections` exposes `status` for the banner.

### `GET /reviews`

- [ ] Returns the documented fields only: no attribution field, no
      reply field. Filters `location_id`, `date_from`, `date_to` and
      `rating_min` work. Cursor pagination works.
- [ ] MANAGER sees only reviews of assigned locations. Merchant A
      never sees B's reviews (`403`/`404`/empty, never the data).
- [ ] A MANAGER `location_id` filter on an unassigned or foreign
      location → `200` with an empty page (OD-6).
- [ ] API key with `reviews:read` → `200`, merchant-wide. A key without
      it → `403`. An invalid key → `401` with no session fallback,
      even with a valid session cookie. Merchant A's key never returns
      B's reviews.

### Provider contract

- [ ] One fixture test per provider method, using anonymized official
      Google samples (G-14): the token response, the revocation error,
      the location list (with and without `placeId` and
      `newReviewUri`), a review page, a rate-limit response, and a
      JWKS (G-15).

### Config & docs

- [ ] `docs/05-integrations/Google-Business-Profile.md` records
      G-1 … G-15 with URL and date. Unverified items are marked
      UNVERIFIED, and no shipped code depends on them.
- [ ] API-Specification.md, Audit-Logging.md, Security-Controls.md and
      Data-Dictionary.md are updated per "Files to change".
- [ ] The RL-1 edits are applied exactly as signed off, and
      Architecture.md is unchanged.

### Priority scenarios from Testing-Strategy.md touched by this spec

- Tenant Isolation (both layers): Schema & RLS, `/reviews`.
- Celery task tenant context: Sync.
- Google integration tests (OAuth flow, sync, upsert; re-sync does not
  duplicate): OAuth, Sync.
- Google identifiers stored separately (Testing-Strategy.md wording
  amended by RL-1 item 5: the link is `newReviewUri`, never built from
  either identifier): Mapping.
- RLS pooled connections: Schema & RLS.
- Security tests (IDOR on UUIDs; secrets never exposed; forged or
  replayed ID tokens rejected): Mapping, OAuth, ID-token validation.

## Out of scope

- The "explicitly skipped" Google location used by campaign
  activation. That is Phase 10. No skip field is added here.
- Disconnecting a connection and unmapping a location: out of V1
  (OD-5, user decision 2026-10-23).
- Deleting synced reviews because they were removed upstream (OD-8). The
  only deletion is the deliberate local remap (RM-1).
- Building a review URL in ReviewFlow, including the undocumented
  `writereview?placeid=` format (RL-1).
- Synchronous token exchange or refresh in any request (GA-1
  rejected).
- Review replies and every V2 item (Feature-Scope.md).
- The 24-month review PII purge (Phase 15).
- Analytics counts (Phase 14) and QR links (Phase 13).
- A `seed_dev` Google fixture. It is not in the §09 bullets, and seed
  data would need fake tokens.
- Any admin registration.

## Open Decisions

**Blocking** means the item must be settled before the code it names is
written. All other code in this spec can proceed.

Status after AM-2 (2026-10-23): **no open decision remains.** The user
approved RL-1's wording and `refresh_connection_token` on 2026-10-23.
Those approvals do not authorize implementation, commits or
deployment: a reviewed implementation plan comes first.

- **OD-1: DECIDED (AM-2): GA-1 REJECTED.** The code exchange is
  asynchronous ("OAuth attempt lifecycle"). Architecture.md line 61 is
  unchanged. G-4 stays UNVERIFIED but no longer blocks: a code that
  expires in the queue ends `EXCHANGE_FAILED`, and the merchant
  restarts.
- **OD-2: APPROVED (2026-10-23).** `GoogleOAuthState`: hashed,
  single-use state bound to merchant and user. AM-1 extended it into
  the attempt lifecycle, and AM-2 added `nonce_hash`.
- **OD-3: DECIDED (2026-10-23).** No Place ID → `422
  google_place_id_missing`, nothing written. Unchanged by RL-1: the
  Place ID is still required and stored, just not used for the link.
- **OD-4: DECIDED (2026-10-23).** The stable identifier is the ID
  token `sub` (G-6), read only after full validation (G-15). It is
  stored as `google_account_sub` with
  `UNIQUE(merchant, google_account_sub)`. There is no email fallback.
- **OD-5: APPROVED (2026-10-23).** The connect, complete,
  location-list, mapping and read endpoints, with their roles.
  Unmapping and disconnect are kept out of V1.
- **OD-6: DECIDED (2026-10-23).** `GET /reviews` accepts API keys with
  `reviews:read`. A MANAGER filter on an unassigned location → empty
  page.
- **OD-7: DECIDED (2026-10-23).**
  `UNIQUE(google_connection, google_location_id)`.
- **OD-8: DECIDED (2026-10-23).** Reviews that disappear upstream are
  kept locally in V1. A deliberate local remap is different (RM-1,
  2026-10-30): it deletes the old mapping's reviews.
- **OD-9: DECIDED (2026-10-23).** No audit event for `NEEDS_REAUTH`;
  one structured warning with only the connection UUID and a fixed
  reason.
- **OD-10: DECIDED (AM-2): option (b).** `review_link` is Google's
  `metadata.newReviewUri`, validated and stored as returned. This is
  the LOCKED DECISION CHANGE **RL-1**, **APPROVED with its exact
  wording on 2026-10-23**.
- **OD-11: APPROVED (AM-2).**
  - `GET /google/oauth/attempts/{id}` (OWNER, ADMIN; the starting user
    only, everyone else `404`);
  - `exchange_oauth_code`;
  - `409 google_token_refresh_pending` on the location list and the
    mapping `PUT` when the access token would need a refresh.

  AM-2 adds one supporting task so that state is not sticky:
  `refresh_connection_token`, enqueued `on_commit` by the `409` path.
  It runs the same conditional refresh as the daily task. **APPROVED by
  the user on 2026-10-23**: token refresh stays out of request
  handling, and the conditional token-update safeguards ("Token
  handling") are unchanged.
- **OD-12: ACCEPTED (AM-2).** `google_location_id` stores
  `accounts/{accountId}/locations/{locationId}`. The v4 reviews call is
  addressed by that parent (G-11), while the Business Information API
  names a location only `locations/{locationId}` (G-8). It is written
  into the Data Dictionary note by RL-1 item 3.
