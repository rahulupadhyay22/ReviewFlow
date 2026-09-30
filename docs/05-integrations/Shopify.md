# Shopify

*Per-provider implementation doc for the ReviewFlow Shopify app, written
before any Shopify code (spec `.claude/specs/06-shopify-app.md`, Files to
create; parent spec `.claude/specs/06-priority-integrations.md` Decision
3, "Shopify verification gate"). Every fact below is sourced from
shopify.dev/help.shopify.com, retrieved on the date shown per finding, or
is a stated ReviewFlow design decision.*

## Summary

ReviewFlow's Shopify integration is a standalone/API-only Shopify app
(public distribution, limited App Store visibility), using the OAuth
authorization code grant. Webhooks are HMAC-verified with the platform
app client secret. `orders/paid` becomes a sale; `app/uninstalled`
disconnects the integration. See the spec for the full normative design;
this doc holds the supporting research, findings and verification status.

**Pinned API version:** `SHOPIFY_API_VERSION` is set to the latest stable
version at implementation time, confirmed as `2026-07` on 2026-09-29 (the
`webhooks/latest` reference page's `api_version:` banner). It determines
the payload version of ReviewFlow's shop-specific subscriptions (F20) and
is the version segment in every Admin GraphQL request URL.

## Findings (F1–F27)

Retrieved from shopify.dev and help.shopify.com. F1–F23 were retrieved on
2026-09-24, except where a row states otherwise. F8 was re-read, and
F24–F27 retrieved, on 2026-09-28. Each is a close paraphrase or a short
quote.

| # | Finding | Source |
|---|---|---|
| F1 | HTTPS webhook HMAC: the key is "your app's client secret"; HMAC-SHA256 over "the raw request body", base64 in `X-Shopify-Hmac-SHA256`. "Reject any delivery where the signatures don't match." | https://shopify.dev/docs/apps/build/webhooks/subscribe/https |
| F2 | "Use `X-Shopify-Webhook-Id` to deduplicate individual deliveries. Use `X-Shopify-Event-Id` to correlate deliveries that originated from the same merchant action." Each subscription gets its own delivery id. | https://shopify.dev/docs/apps/build/webhooks/ignore-duplicates |
| F3 | Shopify "retries 8 times over the next 4 hours" on no response or an error. | page from F1 |
| F4 | Merchants can also create webhooks in admin (Settings → Notifications → Webhooks). This is historical context for the rejected approach, and it is not used. | https://help.shopify.com/en/manual/fulfillment/setup/notifications/webhooks |
| F5 | Admin webhooks "are signed with an ID … unique to your store". The page does not say it is visible, copyable or the HMAC key. This is why the manual approach is rejected. | page from F4 |
| F6 | Admin-created subscriptions are deleted after repeated non-`200` responses. Historical context. | page from F4 |
| F7 | Admin-created subscriptions "won't be returned in API calls … associated solely to the shop". Historical context: an app cannot manage them. | https://shopify.dev/docs/api/admin-rest/latest/resources/webhook (2026-07) |
| F8 | Authorization code grant, which Shopify recommends for standalone apps outside the admin. The install URL is `https://{shop}/admin/oauth/authorize?client_id&scope&redirect_uri&state`, where `redirect_uri` must match the configured value. The callback carries `code, hmac, shop, state, timestamp`. The app must verify `state`, verify `hmac` (remove `hmac`, sort the remaining params, HMAC-SHA256 with the client secret, constant-time compare), and check that `shop` matches `^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$`. The page's example stores the nonce "in a signed cookie" (`oauth_state`) and clears it after validation. It documents no separate check of `timestamp`, and it does not describe the request Shopify sends to the App URL for a Shopify-initiated install. | https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/authorization-code-grant (re-read 2026-09-28) |
| F9 | The token exchange is a `POST https://{shop}/admin/oauth/access_token` with `client_id, client_secret, code, expiring=1`. The response has `access_token`, `refresh_token`, `expires_in`, `scope` and `refresh_token_expires_in` (90 days). The granted `scope` may differ from the requested one, so it must be checked. | page from F8 |
| F10 | Expiring offline tokens are mandatory for public apps: a 1-hour access token (`expires_in` is 3600) and a 90-day refresh token. A refresh is a `POST` to the same endpoint with the `refresh_token`, and it returns a new pair. Uninstalling "ends all of a token's access" (`401`). | https://shopify.dev/docs/apps/build/authentication-authorization/access-token-types/offline-access-tokens |
| F11 | Rotating the client secret (Dev Dashboard → Settings → Credentials → Rotate): the old secret "stays active until you revoke it". Webhooks are signed with the "oldest unrevoked client secret", so verification should accept both during the transition. Tokens are "pinned to the secret that minted it", so refresh them before revoking. | https://shopify.dev/docs/apps/build/authentication-authorization/client-secrets/rotate-revoke-client-credentials |
| F12 | App-specific subscriptions (`shopify.app.toml`) use one URI for all shops. Shop-specific subscriptions ("created using GraphQL Admin API; configuration can differ per shop") allow a per-shop URI. Shopify recommends app-specific unless "delivery URIs … need to vary between shops". Failing shop-specific subscriptions **are deleted** by Shopify; failing app-specific ones are not. Compliance topics cannot use the Admin API. | https://shopify.dev/docs/apps/build/webhooks/subscribe |
| F13 | `ORDERS_PAID` is "the webhook topic for `orders/paid` events. Occurs whenever an order is paid." It needs `read_orders` or `read_marketplace_orders`. `APP_UNINSTALLED` "Occurs whenever a shop has uninstalled the app." | https://shopify.dev/docs/api/admin-graphql/latest/enums/WebhookSubscriptionTopic (2026-07) |
| F14 | The delivery headers are `X-Shopify-Topic`, `X-Shopify-Hmac-Sha256`, `X-Shopify-Shop-Domain` ("the `myshopify.com` domain of the store"), `X-Shopify-API-Version` ("the API version used to serialize the payload"), `X-Shopify-Webhook-Id` ("a unique composite key per delivery"), `X-Shopify-Triggered-At` and `X-Shopify-Event-Id`. Shopify allows a 1-second connect timeout and a 5-second total timeout. After a secret rotation it "can take up to an hour" for HMACs to use the new secret. | https://shopify.dev/docs/apps/build/webhooks/delivery-structure and page from F1 |
| F15 | App Store apps must subscribe to `customers/data_request`, `customers/redact` and `shop/redact` in `shopify.app.toml`. They must return `200`, or `401` on an invalid HMAC, and act within 30 days. `shop/redact` arrives 48 h after uninstall. | https://shopify.dev/docs/apps/build/compliance/privacy-law-compliance |
| F16 | `webhookSubscriptionCreate(topic, webhookSubscription: { uri, filter, includeFields, … })` returns the subscription and `userErrors`. The GraphQL endpoint is `https://{shop}.myshopify.com/admin/api/{version}/graphql.json`, with the `X-Shopify-Access-Token` header. The API version of shop-specific subscriptions is covered by F20, not by this page. | https://shopify.dev/docs/api/admin-graphql/latest/mutations/webhookSubscriptionCreate and https://shopify.dev/docs/api/admin-graphql (2026-07) |
| F17 | Protected customer data: level 2 covers "name, address, phone, or email fields". Public apps need approval, and unapproved fields are returned as `null`, including in webhooks. Development stores skip review. | https://shopify.dev/docs/apps/launch/protected-customer-data |
| F18 | Distribution: public distribution is the method for multiple merchants outside the developer's organization; it needs app review. Custom distribution is limited to a single store or one Plus organization. | https://shopify.dev/docs/apps/launch/distribution |
| F19 | App Store requirements: apps "must be installed and initiated only on Shopify services … must not request the manual entry of a myshopify.com URL or a shop's domain" (2.3.1). They must "immediately authenticate using OAuth before any other steps occur" (2.3.2) and redirect to the app UI after accepting (2.3.3). Embedded apps must work without third-party cookies (1.1.1). | https://shopify.dev/docs/apps/launch/shopify-app-store/app-store-requirements |
| F20 | App-specific subscriptions: "the `api_version` field in `[webhooks]` controls the GraphQL Admin API version used to serialize payloads". Shop-specific subscriptions "created using the GraphQL Admin API": "the version is determined by the request URL". For HTTPS deliveries, the `X-Shopify-API-Version` header shows the version that serialized each payload. Re-verified by the user on 2026-09-24. | https://shopify.dev/docs/apps/build/webhooks/subscribe |
| F21 | Public App Store apps can be listed with **limited visibility**: not indexed in category pages, search results or third-party search engines. "Merchants can install both fully visible and limited visibility apps from an app listing page that uses a Shopify App Store URL." | https://shopify.dev/docs/apps/launch/distribution/visibility |
| F22 | "Standalone and API-only apps run outside the Shopify admin, unlike embedded apps. A standalone app serves its own UI." They use the OAuth authorization code grant. Embedded apps use ID-token (token) exchange. | https://shopify.dev/docs/apps/build/authentication-authorization and https://shopify.dev/docs/apps/build/authentication-authorization/authenticate-standalone-apps |
| F24 | "Apps should be usable without having an additional login or sign-up prompt." For apps whose access "cannot be easily obtained by merchants in a self-service manner": "The first step to the in-admin onboarding of these apps must always be a workflow that enables a merchant to link the current store with their existing credentials." If the app "offers both self-service and business-to-business sign up", onboarding "must include an option to sign up for the service using the merchant's existing Shopify credentials." | https://shopify.dev/docs/apps/build/integrating-with-shopify (retrieved 2026-09-28) |
| F25 | The `app/uninstalled` webhook payload type is the **Shop** resource. Shopify's sample payload has `id` (548380009), `name`, `email`, `shop_owner`, `currency` and `plan_name`, with `domain`, `myshopify_domain`, `created_at` and `updated_at` shown as `null`. `orders/paid` uses the Order resource, which has different top-level fields. (ReviewFlow uses this only as the Phase 06 current-topic discriminator, Decision 3 step 2b. It does not establish that a Shop-shaped body is an `app/uninstalled` payload in general.) | https://shopify.dev/docs/api/webhooks/latest (topic `app/uninstalled`; API 2026-07; retrieved 2026-09-28) |
| F26 | A Global ID is `gid://shopify/{object_name}/{id}`, for example `gid://shopify/Shop/123`. Its numeric id equals the REST Admin API resource id (`legacyResourceId` "provides a simple ID for the equivalent resource in the REST Admin API"). | https://shopify.dev/docs/api/usage/gids (retrieved 2026-09-28) |
| F27 | The GraphQL `shop` query returns "The Shop resource corresponding to the access token used in the request", with `id` ("A globally-unique ID") and `myshopifyDomain` ("The shop's .myshopify.com domain name"). No required access scope is stated. | https://shopify.dev/docs/api/admin-graphql/latest/queries/shop (API 2026-07; retrieved 2026-09-28) |
| F23 | `webhookSubscriptionDelete(id)` "removes a webhook subscription and halts all future webhook deliveries". It returns `deletedWebhookSubscriptionId` and `userErrors`. | https://shopify.dev/docs/api/admin-graphql/latest/mutations/webhookSubscriptionDelete (2026-07) |

## Additional verification (V-a to V-d)

These answer implementation-mechanics questions the parent spec's F-table
left open. Retrieved 2026-09-29.

- **V-a: OAuth callback `hmac` mechanics** (extends F8).
  shopify.dev's authorization-code-grant example:
  - The comparison digest is **hex**-encoded HMAC-SHA256, compared with
    a constant-time comparison (`hmac.compare_digest` on the hex
    strings, or byte-buffer comparison — ReviewFlow uses
    `verify_hmac_sha256(..., encoding="hex")`, the shared helper already
    shipped in `integrations/core/schemas.py`).
  - The message is built by: removing `hmac` from the query parameters,
    sorting the remaining parameters lexicographically by key, then
    joining as `key=value` pairs with `&`, with **no URL-encoding** of
    keys or values in the message (i.e. the raw decoded query values are
    used, not their re-encoded form).
  - Source: `https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/authorization-code-grant`.
- **V-b: token-exchange request encoding** (extends F9). The `POST
  https://{shop}/admin/oauth/access_token` body is
  **`application/x-www-form-urlencoded`**, not JSON: `client_id`,
  `client_secret`, `code` and `expiring=1` as form fields. Same source
  as V-a.
- **V-c: GraphQL response shapes** (extends F16, F23, F27).
  - `webhookSubscriptionCreate` returns
    `{ webhookSubscription: { id, topic, uri, ... }, userErrors: [{ field, message }] }`.
    `webhookSubscription` is null when `userErrors` is non-empty.
    Source: `https://shopify.dev/docs/api/admin-graphql/latest/mutations/webhookSubscriptionCreate`.
  - `webhookSubscriptionDelete` returns
    `{ deletedWebhookSubscriptionId: ID, userErrors: [{ field, message }] }`.
    Source: `https://shopify.dev/docs/api/admin-graphql/latest/mutations/webhookSubscriptionDelete`.
  - `shop { id myshopifyDomain }` returns `id: ID!` (a GID, e.g.
    `gid://shopify/Shop/123456789`) and `myshopifyDomain: String!`. No
    required access scope is documented for these two fields (F27,
    confirmed again here). Source:
    `https://shopify.dev/docs/api/admin-graphql/latest/queries/shop`.
- **V-d: GraphQL URL notation.** The spec writes the request URL as
  `https://{shop}.myshopify.com/admin/api/{version}/graphql.json` (F16),
  where `{shop}` there is the shop *name* segment. Since ReviewFlow's own
  `shop` variable already holds the full `x.myshopify.com` domain (the
  OAuth-verified value), the implementation builds the URL as
  `https://{shop}/admin/api/{SHOPIFY_API_VERSION}/graphql.json` — the
  same URL, just avoiding a double `.myshopify.com`. This is a notational
  reading, not a design change.

## The `orders/paid` sample payload (fixture source)

Retrieved 2026-09-29 from
`https://shopify.dev/docs/api/webhooks/latest` (`api_version: 2026-07`,
machine-readable form at `https://shopify.dev/docs/api/webhooks/latest.md`).
The page publishes a full sample payload per topic; the `orders/paid`
entry is reproduced below (fields relevant to Decision 17 shown; the full
sample also carries many fields the adapter never reads, such as
`billing_address`, `tax_lines`, `discount_codes` — these are not part of
the adapter's field contract and are omitted here for brevity, but kept
in full in the fixture file).

Relevant field values, as published (before anonymization):
- `id`: `820982911946154508` (integer)
- `total_price`: `"404.95"` (string)
- `current_total_price`: `"414.95"` (string; differs from `total_price`
  — see the field table below, "sale total" resolution)
- `currency`: `"USD"`
- `processed_at`: `"2021-12-31T19:00:00-05:00"` (ISO 8601, offset-aware)
- `created_at`: `"2021-12-31T19:00:00-05:00"` (same, present)
- `phone`: `null`
- `customer.phone`: `null`
- `customer.first_name` / `customer.last_name`: `"John"` / `"Smith"`
- `payment_gateway_names`: `["visa", "bogus"]`
- `location_id`: `null`
- `order_number`: `1234`
- `financial_status`: `"voided"`

**This sample's `phone` and `customer.phone` are both `null`.** This
matches F17 (level-2 protected customer data returns `null` without
approval) even in Shopify's own published sample, and it means the
*primary* contract fixture naturally exercises the phoneless path
(Phase 04 Decision 17 behavior), not the phone-present path. Per Decision
17, "Names, phone, email and addresses are replaced with synthetic
values" during anonymization — this authorizes setting a synthetic
`customer.first_name`/`last_name` (already present here, replaced during
anonymization) but does **not** invent an order-level or customer-level
phone value in the primary fixture, since neither is present in Shopify's
own sample. Field-rule test variants (one per Decision 17 row, "starting
from the fixture with that field changed") separately set a synthetic
E.164 phone on a **copy** of the fixture to test the `phone` primary path
and the `customer.phone` fallback path — this is the standard field-rule
mutation the DoD already calls for, not a claim about what Shopify's
sample contains.

**Fixture file:**
`integrations/tests/fixtures/shopify_orders_paid_2026-07.json`
(the full, unmodified sample as published — no fields are removed).

**Anonymization edits** (per Decision 17, "ids and amounts are kept or
replaced consistently"; nothing here is a real merchant's data — it is
Shopify's own published documentation sample, already synthetic):
- No merchant, customer or order data in Shopify's sample is real; it
  is Shopify's own illustrative example. No further anonymization edit
  is needed beyond noting this.
- `email` / `contact_email` (`jon@example.com`) is left as published —
  it is Shopify's own placeholder domain, not a real address.
- No change is made to `id`, amounts, or any other field: the fixture is
  the **unmodified** published sample, so the contract test's expected
  `SaleCreated` is directly checkable against a known-good published
  reference, per Decision 17 ("must not be hand-written from memory").

## `orders/paid` field table, with the Shopify guarantee column filled

Retrieved 2026-09-29 from
`https://shopify.dev/docs/api/admin-rest/latest/resources/order`
(property reference; API `2026-07`) and the sample above.

| Shopify field | `SaleCreated` field | Adapter rule | Shopify guarantee (filled) |
|---|---|---|---|
| `id` | `external_transaction_id` (`str(id)`) | **Required.** Missing or empty → `INVALID_EXTERNAL_TRANSACTION_ID` | Always present in the sample; type **integer**. Not documented as nullable or deprecated. `str(id)` is applied defensively regardless. |
| `total_price` | `amount` | **Required.** `schemas.parse_amount`; missing/invalid → `INVALID_AMOUNT` | Documented: "The sum of all the prices of all the items in the order, taxes and discounts included (must be positive)." Type **string (decimal)**. Not deprecated. This is the correct "sale total" — it differs from `current_total_price` ("current total price... in the shop currency", which reflects post-order edits/refunds) in the sample (`404.95` vs `414.95`), confirming `total_price` is the order-time total, not the current one. |
| `currency` | `currency` | **Required.** Invalid → `INVALID_CURRENCY` | ISO 4217 (`"USD"` in the sample). Not documented as nullable. |
| `processed_at` | `occurred_at` (primary) | Must be tz-aware ISO 8601 | Documented: "The date and time when the order was processed." Present and non-null in the sample, ISO 8601 with a numeric UTC offset (`-05:00`). Not deprecated. |
| `created_at` | `occurred_at` (fallback) | Used only when `processed_at` is null/absent | Documented: "The date and time when the order was created." Present and non-null in the sample, same ISO 8601 format. Not deprecated. Semantically distinct from `processed_at` (creation vs. payment processing time) but both are order-level timestamps, so the fallback's meaning does not materially diverge — not a stop-rule trigger. |
| `phone` (order level) | `customer_phone` (primary) | Blank → `None` | Documented: "The customer's phone number for receiving SMS notifications." Type **string**, nullable (`null` in the sample). **No E.164 format guarantee is documented** — see "Phone format" below. |
| `customer.phone` | `customer_phone` (fallback) | Blank → `None` | `customer` object "might not have a customer" per the docs, so it can be entirely absent, not only its `phone`. `Customer.phone` is documented as "(E.164 format)" but the same page lists accepted **input** formats that are not E.164 (`6135551212`, `(613)555-1212`) — **no read-side E.164 guarantee is documented**. See "Phone format" below. |
| `customer.first_name`, `customer.last_name` | `customer_name` | Joined, trimmed, capped at 255; empty → `None` | Present in the sample (`"John"`, `"Smith"`). Not documented as deprecated. Depends on `customer` being present at all. |
| `payment_gateway_names` | `payment_method` | First element, capped at 32; empty/missing → `None` | Documented: "The names of the payment gateways used for the order." Type **array**. Present as `["visa", "bogus"]` in the sample. Not deprecated. |
| `location_id` | `external_location_id` (`str(...)`) | Null/absent → `None` | Documented: "The ID of the physical location where the order was processed." Type **integer**, nullable (`null` in the sample — this is an online order). Populated for POS orders, `null` for online orders, matching the spec's expectation. Not deprecated. |

### Phone format: E.164 is not guaranteed (resolved by the user, 2026-09-29)

Neither `Order.phone` nor `Customer.phone` is documented as guaranteed
E.164 on read:
- `Order.phone` has no format statement at all.
- `Customer.phone` documents E.164 as the *storage concept* but lists
  accepted **input** formats that are not E.164
  (`6135551212`, `(613)555-1212`, `+1 613-555-1212`), with no statement
  that reads are normalized to `+<countrycode><number>`.
- The `app/uninstalled` Shop sample's `phone` field (`"3213213210"`, no
  `+`) is consistent with non-normalized storage, though it is a
  different resource (Shop, not Customer/Order).

**Resolved by the user, 2026-09-29 — Decision 17 stays exactly as
written, unchanged:**
- `ShopifyAdapter` does **not** normalize, reformat or otherwise
  transform a Shopify phone value.
- If Shopify supplies a non-empty `phone` or `customer.phone` that fails
  ReviewFlow's existing `schemas.validate_e164` check, the sale fails
  with `INVALID_PHONE` — the same unchanged Phase 04 behavior every
  other provider gets.
- The phone is never silently dropped to let the sale continue.
- No Shopify-specific phone normalization dependency or heuristic is
  added.
- **Consequence, recorded here rather than assumed away:** some
  otherwise-valid Shopify orders may enter `FAILED` with `INVALID_PHONE`
  purely because Shopify did not return the phone in E.164 form. This is
  an accepted, documented behavior, not a defect to silently work around.
  It uses the existing Phase 04 validation and error path — no new
  Shopify-specific validation semantics are introduced.

## Merchant flow, install, OAuth `state`, account linking and uninstall (verbatim from Decision 3)

Each step is labelled **[Shopify]** (behavior Shopify documents or
requires) or **[ReviewFlow]** (ReviewFlow's own application-level
design).

  - **Merchant flow (U1 resolved).** Each step is labelled
**[Shopify]** when it is behavior Shopify documents or requires, or
**[ReviewFlow]** when it is ReviewFlow's own application-level design.
Three categories are kept distinct:
- **Shopify requirements** (F8, F19): installation begins on a
  Shopify-owned surface (2.3.1); OAuth happens immediately, before
  any other step (2.3.2); after OAuth the merchant is redirected to
  the app UI (2.3.3); and the documented callback checks (`hmac`,
  `state`, `shop`) apply.
- **Shopify documented onboarding guidance** (F24): apps should
  offer seamless sign-up using Shopify credentials, without an extra
  login or sign-up prompt. There is an **exception** for apps whose
  access cannot easily be obtained self-service (for example,
  business-to-business contracts). For those apps, the first
  onboarding step is a workflow that links the current store to the
  merchant's existing credentials. F24 does **not** require
  existing-account linking for every Shopify app.
- **ReviewFlow decision:** Shopify install → OAuth → ReviewFlow UI →
  explicit, authenticated OWNER/ADMIN linking to an **existing**
  ReviewFlow merchant. This follows the shape of F24's exception
  path. Whether ReviewFlow qualifies for that exception is U8,
  which is open.

Shopify documents neither how an app should hold `state` for a
Shopify-initiated install nor how account linking works internally.
Those parts are ReviewFlow decisions.
1. **Install entry.**
  - **[Shopify]** The merchant starts the install from the Shopify
    App Store listing or another Shopify-owned surface (F19 2.3.1,
    F21), and the app must "immediately authenticate using OAuth
    before any other steps occur" (F19 2.3.2).
  - **[ReviewFlow]** The app's configured App URL is
    `GET /integrations/shopify/install`. It receives `shop` from
    Shopify's redirect; the merchant never types it.
  - **VERIFY (U1a):** the exact query parameters Shopify sends to the
    App URL. The fetched docs do not state them, and no ReviewFlow
    check relies on any parameter other than `shop`.
  - **[ReviewFlow]** The endpoint does the following:
    - It is unauthenticated and pre-tenant, and it is per-IP
  throttled.
    - It rejects a `shop` that fails
  `^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$` with `400`.
    - It generates `state = secrets.token_urlsafe(32)`.
    - It stores `shopify_oauth = { state, shop, issued_at }` in the
  browser's Django session. This is server-side, keyed by the
  signed, `HttpOnly`, `Secure`, `SameSite=Lax` session cookie,
  which is the server-side equivalent of Shopify's example
  "store the nonce in a signed cookie" (F8). It replaces any
  earlier `shopify_oauth` in that session.
    - It immediately returns `302` to
  `https://{shop}/admin/oauth/authorize?client_id=SHOPIFY_CLIENT_ID&scope=read_orders&redirect_uri=SHOPIFY_REDIRECT_URI&state=<state>`.
    - It shows no UI, needs no login, and reads or writes no tenant
  data.
    - Visiting it with an arbitrary `shop` only produces a redirect
  to that shop's own Shopify OAuth page, which only that shop's
  staff can approve.
2. **[Shopify]** The shop's staff member approves the requested
  scope on Shopify's OAuth page.
3. **Callback, `GET /integrations/shopify/callback`.**
  - **[Shopify]** Shopify redirects the same browser here with
    `code`, `hmac`, `shop`, `state` and `timestamp` (F8).
  - **[Shopify]** The documented checks run first, and any failure
    returns the generic `400 shopify_install_invalid` with nothing
    stored:
    - The query `hmac` must verify: remove `hmac`, sort the rest,
  HMAC-SHA256 with the client secret (`_PREVIOUS` also accepted
  during a rotation), `compare_digest`.
    - `state` must equal the stored nonce.
    - `shop` must match the regex.
  - **[ReviewFlow]** On top of those:
    - The session's `shopify_oauth` is **popped**, so it is used at
  most once, whatever the outcome.
    - It must exist and be at most **10 minutes** old from
  `issued_at` (expiry).
    - Its `state` must match with `compare_digest`, and its `shop`
  must equal the callback's `shop`.
    - `timestamp` is inside the HMAC-signed parameter set. Shopify
  documents no separate freshness check for it (F8). Replay is
  bounded by the single-use, 10-minute `state`, so ReviewFlow
  adds no `timestamp` window.
  - **[Shopify]** Token exchange: `POST
    https://{shop}/admin/oauth/access_token` with `code` and
    `expiring=1` (F9). The granted `scope` must include `read_orders`,
    else `400`. A timeout or non-`200` returns `502
    shopify_unavailable`.
  - **[ReviewFlow]** On success the callback stores a **pending
    installation** in the same session:
    `shopify_pending = { shop, credentials_fernet, issued_at }`.
    - `credentials_fernet` is `core.crypto.encrypt(json)` of the
  token bundle. The token is never plaintext in the session
  store.
    - It expires 15 minutes after `issued_at`, and only one pending
  installation exists per session.
    - The callback then redirects (`302`) to the ReviewFlow link
  page, at the URL held in `SHOPIFY_LINK_PAGE_URL` (**[User
  decision 2026-09-29, spec amendment]**; see "Link page URL"
  below). This is the app UI that F19 2.3.3 requires after
  OAuth.
    - The redirect's `Location` carries no query string and no
  fragment: no `shop`, no `code`, no `hmac`, no `state`, no
  token and no pending-installation data. Everything the link
  page needs is fetched afterward, over the session, from `GET
  /integrations/shopify/pending`.
  - The callback **never** reads `request.merchant_id`, the session
    merchant or any callback parameter to decide a merchant, and it
    creates **no** `Integration`. `shop` identifies the Shopify
    installation only. It never determines `merchant_id`.

**Link page URL (resolves the spec's only open gap; [User decision
2026-09-29, spec amendment]).**
- `SHOPIFY_LINK_PAGE_URL` is a new environment-only setting: the
  full URL of the ReviewFlow link page on the Next.js frontend
  (for example `https://<dashboard-origin>/integrations/shopify/link`).
  It is read once at settings load, exactly like the other
  `SHOPIFY_*` settings, and is never hardcoded.
- It **must** be on the documented ReviewFlow dashboard/frontend
  origin — the same origin the session cookie is scoped to
  (`Authentication.md` §1: "same-origin (via Cloudflare) with the
  Next.js frontend"). This is an existing architectural constraint,
  not a new one: it keeps the callback's `302` inside the
  same-origin session flow that `shopify_pending`, the CSRF cookie
  and `/pending`/`/link` already rely on. No cross-origin or
  cross-site redirect is introduced.
- It is **server-side configuration only**. It is never derived
  from, or overridden by, any Shopify request parameter (`shop`,
  `hmac`, `state`, `code`) or anything the browser sends. The
  callback always redirects to the one configured value.
- The `302 Location` header built from it carries no Shopify
  credentials, OAuth code, HMAC, token, pending-installation bundle,
  `shop`, `merchant_id` or any other installation secret — see the
  "no query string and no fragment" rule just above.
- This setting resolves the spec's only remaining gap. It changes
  no other architecture: the TOTP carry-over and the
  `SHOPIFY_REDIRECT_URI`-derived webhook host are unaffected
  implementation-plan decisions, not spec content, and neither is
  touched here.
4. **Account linking: the explicit, authenticated step (a ReviewFlow
  decision, following F24's exception path; see U8).**
  - **[ReviewFlow]** The link page requires a normal ReviewFlow
    dashboard login. The merchant logs in (password + TOTP when
    enrolled) if they are not already logged in.
  - Django's `login()` cycles the session key and keeps the session
    data of an anonymous session, so `shopify_pending` survives the
    login. If the browser was logged in as a **different** user,
    Django flushes the session and the merchant must reinstall.
  - `GET /integrations/shopify/pending` (session, OWNER/ADMIN):
    - returns `{ shop }` for the confirmation screen: "Link
  `<shop>` to `<merchant name>`?";
    - returns `404` if nothing is pending or it has expired.
  - `POST /integrations/shopify/link` (session + CSRF, OWNER/ADMIN,
    empty body; any `shop`, `merchant_id` or `credentials` in the body
    is ignored):
    - It pops `shopify_pending`, so it is single-use, and checks
  the 15-minute expiry.
    - It decrypts the bundle, then runs the lifecycle below (steps
  5–8) under the **session** merchant.
    - It returns `201` with the Integration body.
    - A missing or expired pending installation returns
  `409 shopify_install_expired`: reinstall from Shopify.
    - MANAGER and VIEWER get `403`, and the pending installation is
  **not** consumed.
  - **Why a shop cannot be attached to an arbitrary merchant:**
    - The pending installation exists only in the session of the
  browser that completed Shopify's HMAC-verified OAuth for that
  shop, which needs that shop's staff approval.
    - It can be consumed only by an authenticated OWNER/ADMIN in
  that same session, with a CSRF-protected `POST`.
    - The merchant always comes from that session.
    - An attacker who installs their own shop holds the pending
  installation in *their* session, which the victim's browser
  never has. A victim's install can be linked only by whoever
  is logged in to ReviewFlow in the victim's browser.
  - **CSRF across the redirect:** OAuth `state` protects the callback
    (F8), and the Django CSRF token protects the link `POST`.
    `SameSite=Lax` sends the session cookie on Shopify's top-level
    `GET` redirect back to the callback.
- **Open item U8: OPEN, and not a development blocker.** What F24
  says:
  - The default is that apps "should be usable without having an
    additional login or sign-up prompt".
  - The link-existing-credentials workflow is the first onboarding
    step only for apps whose access "cannot be easily obtained by
    merchants in a self-service manner".
  - An app that offers both self-service and business-to-business
    sign-up "must include an option to sign up for the service using
    the merchant's existing Shopify credentials".

  **The unresolved question:** does ReviewFlow's intended commercial
  and onboarding model qualify for F24's exception? Or will App Store
  review require a Shopify-credential sign-up path that can create a
  **new** ReviewFlow merchant from an install?
  - This spec does not claim either answer.
  - The link-to-existing-merchant flow is built as the ReviewFlow
    decision.
  - Any Shopify-credential sign-up path is not designed here.
  - U8 is a product decision and an **App Store submission
    prerequisite**, not a blocker for building the linking flow.
5a. **Resolve the shop identity [ReviewFlow, using documented
  Shopify APIs].** This runs before the provisional insert, and no
  subscription exists yet.
  - Query GraphQL `{ shop { id myshopifyDomain } }` with the
    just-issued access token, against
    `https://{shop}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/graphql.json`
    (F16, F27).
  - `myshopifyDomain` must equal the OAuth-verified `shop`,
    otherwise `400 shopify_install_invalid`.
  - `id` must match `^gid://shopify/Shop/(\d+)$` (F26). The
    captured digits are stored as `config_json.shop_id`. That is the
    numeric id that equals the REST resource id (F26) carried in the
    `app/uninstalled` Shop payload's `id` (F25).
  - A timeout, a non-`200` or `errors` returns `502
    shopify_unavailable`. A malformed `id` returns `400
    shopify_install_invalid`. Nothing is stored in either case.
  - Whether the `shop` query needs any extra access scope is not
    stated (F27), so it is confirmed on a development store (U9).
5. **Provisional `Integration`.** Inside the transaction of the
  `POST /integrations/shopify/link` request (`TenantMiddleware`'s
  `tenant_atomic()` for the session merchant), the service opens a
  **savepoint** and inserts the `Integration`: provider `shopify`,
  `status=CONNECTED`, the encrypted OAuth credential, and
  `config_json.shop_domain`.
  - The row is **provisional because it is uncommitted**. Until the
    request transaction commits, no other connection sees it, so the
    `integration_lookup` read in a webhook receiver finds nothing
    and returns `401` (Decision 1).
  - The existing model therefore represents "connecting" safely,
    and **no new `Integration` status is introduced**.
6. **Registration.** Shop-specific subscriptions are registered with
  GraphQL `webhookSubscriptionCreate` (F12, F16), against the request
  URL
  `https://{shop}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/graphql.json`:
  - `ORDERS_PAID`, with
    `uri = https://<public API host>/api/v1/webhooks/shopify/{integration.id}`;
  - `APP_UNINSTALLED`, to the same `uri` (F13).
7. **Finalize.** The returned subscription ids are written to
  `config_json.webhook_subscription_ids`, the `integration.connected`
  audit row is written, and the savepoint is released. The
  `Integration` becomes visible, and therefore effectively
  `CONNECTED`, when the request transaction commits.
8. **Registration failure.** A `userErrors` response, a transport
  error, a timeout, or a failure in step 7:
  - For every subscription already created in step 6, the service
    calls `webhookSubscriptionDelete(id)` (F23), best-effort. Each
    delete gets its own timeout, and a failed delete is logged with
    the subscription id and the exception class only.
  - The savepoint is **rolled back** before the error is raised, so
    the provisional `Integration` and its audit row never exist.
    This must be an explicit savepoint rollback: `TenantMiddleware`
    commits on a normal DRF error response, so the service cannot
    rely on the request rolling back.
  - The service returns `502 shopify_unavailable`. No `CONNECTED`
    integration remains.
  - A subscription whose delete also failed is **not** knowingly
    left behind as the design. It is a logged, best-effort cleanup
    failure. Its deliveries get `401` (the integration id does not
    exist), and Shopify deletes failing shop-specific subscriptions
    (F12) as the backstop.
9. Shopify sends `orders/paid` to `/webhooks/shopify/{integration_id}`.
  ReviewFlow verifies `X-Shopify-Hmac-Sha256` with the ReviewFlow app
  client secret, records the `IntegrationEvent`, and runs the
  unchanged Phase 04 pipeline.

*Consequence of steps 5–7:* the Shopify HTTP calls run while the
request transaction is open. They use explicit short timeouts, and
the only row held is the new, uncommitted one, so no other request
waits on it. A delivery that races the commit gets `401`, and Shopify
retries it (F3).
  - **Webhook verification (normative).** `ShopifyAdapter.verify()`
computes `base64(HMAC-SHA256(SHOPIFY_CLIENT_SECRET, request.body))`
over the **raw** body, exactly as received. It compares the result
with `X-Shopify-Hmac-Sha256` using `hmac.compare_digest` (F1, F14).
- A missing header fails closed.
- During a rotation it also accepts a match against
  `SHOPIFY_CLIENT_SECRET_PREVIOUS` when that is set (F11).
- After the signature verifies, the receiver also requires
  `X-Shopify-Shop-Domain == Integration.config_json["shop_domain"]`
  (F14), otherwise the identical `401`. This is defense in depth
  **only**:
  - every installed shop's deliveries are signed with the same app
    secret;
  - **the HMAC covers the raw body, not the headers** (F1). So
    neither `X-Shopify-Shop-Domain` nor `X-Shopify-Topic` is
    authenticated.
- **Headers route; the signed body gates destructive action.**
  `X-Shopify-Topic` only selects a handler. The destructive uninstall
  additionally requires the **signed body** to pass a **Phase 06
  current-topic discriminator** bound to this integration's stored
  `shop_id` (see "Uninstall payload binding" below).
  - This does **not** cryptographically authenticate
    `X-Shopify-Topic`, which stays unsigned.
  - It only tells apart the two payload types that ReviewFlow's
    current subscriptions can deliver.
- The dedupe key is `X-Shopify-Webhook-Id`, the delivery-level id
  (F2, F14). **`X-Shopify-Event-Id` is not the delivery dedupe key.**
  If two subscriptions deliver the same order, the second is a no-op
  `PROCESSED` duplicate through Phase 04's
  `UNIQUE(location_id, external_transaction_id)`.
- `X-Shopify-Topic`: `orders/paid` becomes a sale. `app/uninstalled`
  triggers the uninstall handling below. Any other verified topic
  returns `200` and is not stored.
- Shopify allows a 5-second total request timeout (F14). The receiver
  only writes the inbox row and enqueues, which is already the
  Phase 04 design.
  - **Uninstall (boundary: in Phase 06, minimal).** On uninstall Shopify
sends `app/uninstalled` (F13), and the merchant's tokens stop working
(F10). Leaving the `Integration` `CONNECTED` would keep dead
credentials and mislead the dashboard. So in Phase 06, a verified
`app/uninstalled` delivery calls the existing
`disconnect_integration` as a **system** action, with `actor=None`:
- it sets `DISCONNECTED`;
- it clears `credentials_encrypted`;
- it deactivates the mappings;
- it cancels pending events;
- it writes one `integration.disconnected` audit row with
  `metadata={"provider": "shopify", "reason": "app_uninstalled"}`.

**Idempotency (normative).** The receiver handles `app/uninstalled`
in this order. No new `Integration` status is introduced.
1. Look up the `Integration` by `integration_id` (Decision 1). An
  unknown or malformed id, or a non-`shopify` provider, returns `401`.
2. Verify the HMAC with the platform client secret, and the
  `X-Shopify-Shop-Domain` header check against the stored
  `shop_domain`. **Both run even when the `Integration` is already
  `DISCONNECTED`.**
  - The key is the platform secret. `config_json` (`shop_domain`,
    `shop_id`) is kept on disconnect; only `credentials_encrypted`
    is cleared.
  - An invalid HMAC or a header mismatch returns `401`.
2a. **[User decision 2026-09-29, O1]** `X-Shopify-Webhook-Id` must
  be present. A missing header returns the identical `401
  invalid_signature`.
  - This check runs before step 2b, and before any state is read
    or changed.
  - It also runs when the `Integration` is already `DISCONNECTED`.
    The step 3 fast path is therefore never reached without it.
2b. **Uninstall payload binding: a Phase 06 current-topic
  discriminator.** This is required whenever
  `X-Shopify-Topic == "app/uninstalled"`, before any state is read
  or changed. The signed body must be a JSON object that:
  - has an integer top-level `id`, and contains **none** of the
    Order-resource keys `line_items`, `order_number`,
    `financial_status` or `total_price`;
  - has `str(id) == Integration.config_json["shop_id"]`.

  Any failure returns the identical `401 invalid_signature`. Nothing
  changes (no state, no audit row), and nothing is logged beyond the
  integration id.
  - **What it proves, and only this.** Phase 06 subscribes exactly
    `ORDERS_PAID` (an Order payload) and `APP_UNINSTALLED` (a Shop
    payload, F25). The check separates those two documented shapes
    and binds the result to this integration's shop.
    - A validly signed `orders/paid` body carries Order keys, and
  its `id` is an order id, not this shop's id. Relabelling its
  topic header as `app/uninstalled` therefore fails the check.
    - A Shop-shaped payload for **another** shop fails the `shop_id`
  comparison.
  - **What it does not prove.** It is **not** a general proof that
    a body is an `app/uninstalled` payload, and it does **not**
    cryptographically authenticate `X-Shopify-Topic`. Shopify's HMAC
    covers the raw body only (F1).
    - Per the 2026-09-28 review, other documented topics also carry
  a Shop resource (for example `shop/update`). The page fetched
  for F25 did not detail `shop/update`, so this is recorded as
  a review input and must be confirmed in `Shopify.md`. It is
  not an F-finding.
    - "Not an Order" therefore means "uninstall" **only** because
  no Shop-payload topic other than `app/uninstalled` is
  subscribed in Phase 06.
  - Neither `X-Shopify-Topic` nor `X-Shopify-Shop-Domain` alone can
    authorize the disconnect.
  - It relies only on documented facts: the payload types (F25),
    the `id` in the payload, and the GID's numeric id matching the
    REST id (F26). It does **not** rely on `myshopify_domain` or
    `domain`, which are `null` in Shopify's own sample payload
    (F25).
  - The live payload shape and id equality are confirmed on a
    development store (U9).
3. If the topic is `app/uninstalled` and step 2b passed:
  - **The `Integration` is `CONNECTED`:** call
    `disconnect_integration(actor=None, reason="app_uninstalled")`.
    It runs exactly once: it re-reads the row under its
    `FOR NO KEY UPDATE` lock and no-ops if another delivery already
    disconnected it, so concurrent duplicates write one audit row.
    Returns `200`.
  - **It is already `DISCONNECTED`:** an idempotent duplicate
    (Shopify's retry, or a second subscription's delivery). Return
    `200`, with **no** state change and **no** second audit row.
4. Any other verified topic for a `DISCONNECTED` integration returns
  `401 invalid_signature`, and nothing is stored. That keeps the
  Decision 1 rule for everything except the verified, bound
  uninstall duplicate.
- **Re-evaluation rule (normative).** The discriminator is valid
  only for the Phase 06 subscription set: `ORDERS_PAID` and
  `APP_UNINSTALLED` (step 6).
  - Before **any** Shopify topic is added to what ReviewFlow
    receives, the uninstall discriminator must be re-evaluated
    against that topic's documented payload. This covers new
    shop-specific subscriptions, app-specific ones, and the future
    compliance topics.
  - Topics carrying a Shop resource, such as `shop/update`, are the
    obvious case, but not the only one.
  - The implementation must never assume "not an order" means
    "uninstall" once the set changes. `Shopify.md` repeats this
    rule.
- **Residual U10: ACCEPTED as a V1 residual risk, and non-blocking.**
  A **genuine** `app/uninstalled` body for the same shop, captured
  and replayed later against a newer `Integration` for that shop
  (after a reinstall), would pass the binding.
  - Only Shopify can sign such a body, with the app secret.
  - It reaches ReviewFlow only over TLS, and ReviewFlow never logs
    request bodies.
  - Shopify documents no signed timestamp or nonce in the body that
    could bind it to a single integration.
  - The same residual applies to a captured signed `orders/paid`
    replayed to another integration of the same app.
  - The user accepts it for V1. No timestamp, nonce or other replay
    validation is added. The idempotency rules above are unchanged.

Reinstalling means a new install and link, which creates a new
`Integration` and a new URL.
**Compliance webhooks.** These are `customers/data_request`,

## Client-secret rotation

- **Client-secret rotation is a platform-level operation.** There is
  no merchant-level rotation. Shopify's documented behavior:
  - both secrets stay active until the old one is revoked;
  - webhooks are signed with the app's **oldest unrevoked** secret;
  - it "can take up to an hour" for HMACs to use the new secret;
  - access tokens are pinned to the secret that minted them;
  - "Don't revoke your old secret until you've updated all stored
    access tokens" (F11, F14).

  ReviewFlow's procedure, written into `Shopify.md`:
  1. Rotate in the Shopify Dev Dashboard.
  2. Deploy with `SHOPIFY_CLIENT_SECRET=<new>` and
    `SHOPIFY_CLIENT_SECRET_PREVIOUS=<old>`. Verification and the OAuth
    callback HMAC then accept both.
  3. Re-pin stored tokens by refreshing them. In Phase 06 the stored
    token is used only during installation (see "Token lifetime").
    Tokens whose refresh token has lapsed cannot be re-pinned; they
    simply become invalid at revocation, and webhook delivery does not
    use them.
  4. Revoke the old secret in the Dev Dashboard.
  5. Deploy with `SHOPIFY_CLIENT_SECRET_PREVIOUS` unset.

  *Whether existing webhook subscriptions keep delivering across a
  revocation is UNRESOLVED (U5).*

## Verification status (V1–V10) and open items (U1–U10)

- **Verification status of V1–V10:**

| # | Item | Status |
|---|---|---|
| V1 | Distribution for a multi-merchant SaaS | **Verified:** public distribution (F18) with limited App Store visibility (F21). It is still an App Store listing, so F19 and F15 apply (U7 resolved) |
| V2 | Install/OAuth for outside merchants | **Verified:** a standalone/API-only app using the authorization code grant (F8, F22); not embedded (U2 resolved). **Install initiation and account linking: RESOLVED (U1).** Initiation from Shopify, immediate OAuth and the post-OAuth app-UI redirect are Shopify requirements (F19). Linking the store to an existing ReviewFlow merchant is a ReviewFlow decision that follows F24's exception path; whether ReviewFlow qualifies for that exception is U8, which is open. The `state` storage and the linking mechanics are ReviewFlow decisions (Decision 3, merchant flow). The exact App URL parameters are VERIFY (U1a) |
| V3 | The per-merchant credential | **Verified:** an expiring offline access token plus a refresh token, with their expiries and the granted scope (F9, F10) |
| V4 | Webhook registration | **Verified:** shop-specific `webhookSubscriptionCreate` through the GraphQL Admin API, with the `X-Shopify-Access-Token` header (F12, F16) |
| V5 | The `orders/paid` identifier | **Verified:** GraphQL enum `ORDERS_PAID`; the topic string is `orders/paid`; `read_orders` is required (F13). That the `X-Shopify-Topic` header value is exactly `orders/paid` is implied by F14's format, and it is confirmed against the first real development-store delivery |
| V6 | The HMAC rule | **Verified** (F1, F14) |
| V7 | `X-Shopify-Webhook-Id` | **Verified:** the delivery-level dedupe id, different per subscription (F2, F14). Whether it is stable across retries: **UNRESOLVED (U3)**. Harmless either way, because the transaction uniqueness backstops it |
| V8 | API version | **Verified (U4 resolved, F20):** ReviewFlow's shop-specific subscriptions are created with `webhookSubscriptionCreate` against a request URL carrying the pinned `SHOPIFY_API_VERSION`, and that URL version determines the payload version. The app-configured `webhooks.api_version` does **not** control these subscriptions. `X-Shopify-API-Version` (F14, F20) is the runtime field for verifying and observing each delivery's serialization version |
| V9 | Client-secret rotation | **Verified** (F11, F14). Subscription behavior across a revocation: **UNRESOLVED (U5)** |
| V10 | Uninstall | **Verified:** the `app/uninstalled` topic exists (F13) and tokens are revoked (F10). Whether shop-specific subscriptions are removed on uninstall: **UNRESOLVED (U5)**. The ReviewFlow handling is defined above |

- **Open and resolved items.**

| # | Item | Status | Blocks Shopify code? |
|---|---|---|---|
| U1 | Shopify-initiated install, and linking it to an existing ReviewFlow merchant account | **RESOLVED.** Shopify-required (F8, F19): the install starts on Shopify, OAuth comes first, and there is a redirect to the app UI. ReviewFlow decisions, following F24's exception path (U8 open): the session-held single-use 10-minute `state`, the encrypted 15-minute pending installation, and the explicit authenticated OWNER/ADMIN `POST /integrations/shopify/link`. There is no merchant-entered shop domain | No |
| U1a | Exact query parameters Shopify sends to the App URL (`GET /integrations/shopify/install`) on a Shopify-initiated install | **VERIFY** on a development store. The fetched docs do not state them (F8), and ReviewFlow relies only on `shop` | No, but it must be confirmed before `06-shopify-app` merges |
| U8 | Whether ReviewFlow's commercial and onboarding model qualifies for F24's exception (non-self-service access, so link to existing credentials), or whether App Store review will require a Shopify-credential sign-up path that can create a new ReviewFlow merchant | **OPEN**, a product decision | No. It is an **App Store submission prerequisite**, like the compliance webhooks |
| U9 | A live `app/uninstalled` delivery: its body is a Shop resource whose integer `id` equals the numeric part of `shop { id }` fetched at install, and it has none of the listed Order keys. Also, whether the `shop` query needs an extra access scope | **VERIFY** on a development store (F25 shows only a sample payload; F27 states no scope) | No, but it must be confirmed before `06-shopify-app` merges |
| U10 | Replay of a captured, genuine signed body (`app/uninstalled` or `orders/paid`) against a later or different `Integration` of the same app | **ACCEPTED as a V1 residual risk** (Decision 3). Shopify documents no in-body binding that would prevent it, and no timestamp or nonce validation is invented | No |
| U2 | Embedded or standalone | **RESOLVED / VERIFIED:** standalone/API-only, authorization code grant (F22). Locked by the user | No |
| U3 | `X-Shopify-Webhook-Id` stability across retries | Unresolved in the docs. Harmless, because `UNIQUE(location_id, external_transaction_id)` backstops it | No |
| U4 | Payload API version for shop-specific subscriptions | **RESOLVED / VERIFIED:** the request URL's version, i.e. the pinned `SHOPIFY_API_VERSION` (F20). `X-Shopify-API-Version` is observed per delivery | No |
| U5 | Whether shop-specific subscriptions survive an uninstall or a client-secret revocation | Unresolved in the docs. Nothing in this design depends on it: uninstall disconnects the `Integration` whatever happens to the subscription, and a revoked-then-orphaned subscription gets `401` | No, unless a later implementation comes to depend on it |
| U6 | Level-2 protected customer data approval (F17) | Unresolved; an operational item. Development stores skip review | No. It is a **production prerequisite**, not a development blocker |
| U7 | Limited-visibility public distribution | **RESOLVED / VERIFIED:** it exists (F21). It is still an App Store listing, so F19 and F15 apply | No |

- **Stop rule (unchanged).** If a required Shopify mechanism cannot be
established from authoritative Shopify documentation, implementation
stops and asks the user. Do not improvise.
- **No blocker remains for Shopify implementation** (U1 is
resolved). U1a and U9 are confirmed on a development store before
`06-shopify-app` merges. U10 is an accepted V1 residual, and it
does not block.
- U8 and the compliance webhooks gate App Store submission.
- U6 must be resolved before production launch.
- The compliance webhooks (Phase 15, proposed) must ship before App
  Store submission.

## Re-evaluation rule

The current-topic discriminator (`uninstall_payload_matches`) is valid
only for the Phase 06 subscription set: `ORDERS_PAID` and
`APP_UNINSTALLED`. **Before any Shopify topic is added** to what
ReviewFlow subscribes to — a new shop-specific subscription, an
app-specific one, or a future compliance topic — the discriminator must
be re-evaluated against that topic's documented payload shape. Topics
carrying a Shop resource (for example `shop/update`, noted per the
2026-09-28 review as an unconfirmed example, not an F-finding) are the
obvious risk, but not the only one. The implementation must never assume
"not an order" means "uninstall" once the subscription set changes.

## U10: accepted V1 residual risk

Replay of a captured, genuine signed body (`app/uninstalled` or
`orders/paid`) against a later or different `Integration` of the same
app is **accepted as a V1 residual risk**. Shopify documents no in-body
binding (timestamp, nonce) that would prevent it. The user accepted this
for V1; no replay validation is added.

## Gate status for `06-shopify-app`

- No blocker remains for Shopify implementation (U1 resolved; U2, U4, U7
  resolved).
- **U1a and U9 are still unverified.** In the original Phase 06 plan they
  were development-store validation gates, required before
  `06-shopify-app` merged. Phase 06 has since merged (PR #11) without
  them: the development-store validation was not performed, because no
  Shopify development store or app credentials were available, and it
  is now deferred. It must still be completed when a Shopify
  development store and app become available. See "Deferred
  Development-Store Validation" below.
- **The E.164 finding above is now resolved** (2026-09-29): Decision 17
  stays unchanged; no adapter normalization is added.
- U8 and the compliance webhooks gate **App Store submission**, not this
  branch.
- U6 (level-2 protected customer data approval) gates **production
  value**, not development.

## Deferred Development-Store Validation

**Status (2026-09-30): DEFERRED, not performed.** No Shopify development
store and no Shopify app credentials are currently available, so none of
the checks below has been run. `06-shopify-app` merged (PR #11) without
them. They are **not** covered by the automated tests, and no observed
Shopify value is recorded in this document yet.

Still to verify on a real development store, with the real ReviewFlow
Shopify app installed:

- **U1a:** the exact query parameters Shopify sends to the App URL,
  `/integrations/shopify/install`. (The OAuth callback's parameters are
  already specified, F8; U1a is about the App URL only.)
- **U9:** a real `app/uninstalled` payload passes the uninstall check
  (`uninstall_payload_matches`), and whether the `shop` query needs an
  extra access scope.
- **Webhook subscriptions:** both `orders/paid` and `app/uninstalled`
  appear after a real install.
- **First real `orders/paid` delivery:** the `X-Shopify-Topic` value
  (V5) and the `X-Shopify-API-Version` value against the pinned
  `SHOPIFY_API_VERSION` (V8).

When these are run, record the observed results here, with the
verification date and the development-store context, keeping observed
Shopify behavior separate from documented or assumed behavior. If an
observation differs from the implementation, report the mismatch. Do not
edit this document to hide it.
