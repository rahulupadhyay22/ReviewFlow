# Spec: Priority Integrations

## Overview
Phase 06 puts the first real sale sources on top of the Phase 04 ingestion
core. It adds four of the nine V1 integrations:
- **Shopify**, as the ReviewFlow Shopify app (Decision 3, locked):
  - an OAuth install;
  - app-registered `orders/paid` webhooks, verified with the platform app
    client secret;
  - uninstall handling.

  It is proposed as the separate `06-shopify-app` spec (see Roadmap
  Phase).
  - U1 is resolved, and no implementation blocker remains.
  - U1a and U9 are development-store verifications required before
    `06-shopify-app` merges.
  - U8 and the compliance webhooks are App Store submission
    prerequisites.
- **Generic Webhook**: any system, with a merchant-defined field mapping in
  `Integration.config_json` and a per-integration signing secret.
- **CSV Import**: the file is uploaded, staged in Cloudflare R2, turned into
  one inbox event per row, then purged.
- **Generic REST API**: `POST /sales` and `GET /sales`, authenticated by a
  Phase 05 API key with a named scope.

This plane is **ingestion**. Every source ends in the same Phase 04
pipeline:

`Integration identified → verify → IntegrationEvent (merchant_id set) →
UNIQUE(integration_id, external_event_id) → parse/normalize → SaleCreated →
location resolution via IntegrationLocationMapping → Customer (if phone) →
Transaction`

Nothing downstream of `normalize()` changes. The spec also writes the
deferred per-provider docs (`Shopify.md`, `Generic-Webhook.md`,
`CSV-Import.md`).

It exists now because Phase 04 (ingestion core) and Phase 05 (API keys) are
Done, and Phase 10 (campaigns) needs real transactions to act on.

### The nine V1 integrations, and which ship here
`Integration-Architecture.md` §"Product Scope vs. Implementation Phase"
lists nine integrations. Generic REST API and Generic Webhook are **two
separate rows**. The request that produced this spec merged them into one
item ("Generic REST/Webhook") and left a ninth slot open. The ninth is
therefore the **Generic REST API**, not a new integration.

| Integration | Phase | In this spec |
|---|---|---|
| Shopify (as the ReviewFlow Shopify app, Decision 3) | 06 | Yes. No longer blocked: U1 is resolved (Decision 3). It stays a separate `06-shopify-app` spec unless the user chooses otherwise |
| Generic Webhook | 06 | Yes |
| CSV Import | 06 | Yes |
| Generic REST API | 06 | Yes |
| WooCommerce, Petpooja, GoFrugal, Zapier, Make | 17 (`ROADMAP.md`) | **No.** They are V1 scope with a later implementation phase. They are **not** V2 |

### Where Phase 06 stops
| Not in Phase 06 | Owning phase |
|---|---|
| WooCommerce, Petpooja, GoFrugal, Zapier, Make adapters | 17 |
| Refund/void ingestion (`Transaction.status` → `REFUNDED`/`VOIDED`) | 11 (Decision 12) |
| Eligibility, `CampaignExecution`, the `Transaction` row lock | 10 |
| Invalid-signature spike alerting | 18 |
| 90-day purge of `IntegrationEvent.payload` | 15 |
| Dead-letter review in Django Admin | 16 |
| Shopify mandatory compliance webhooks (`customers/data_request`, `customers/redact`, `shop/redact`). They are mandatory before App Store submission and review, including for a limited-visibility listing | Proposed: 15, plus a new lookup-policy sign-off (Decision 3). **Phase 15 (or its replacement owner) is a prerequisite for App Store submission** |
| Shopify token refresh (refresh-before-use or a job) | The first later phase that calls the Shopify Admin API after install (Decision 3) |
| Shopify level-2 protected customer data approval | Operations, outside the code (Decision 3, U6) |

### Decisions this spec makes where the docs are silent
Each decision is written into the named doc in the same PR (see Files to
change). Decision 1 is a LOCKED DECISION CHANGE and needs sign-off. Decisions
2, 4, 5 and 12 change or extend documented text and are flagged for the
user in the report.

1. **Pre-tenant `Integration` lookup: `integration_lookup` (LOCKED DECISION
   CHANGE).** This resolves the open item Phase 04 carried forward. A
   webhook request carries no merchant, and `integrations_integration` is
   `FORCE` RLS. The design mirrors the signed-off `self_membership` and
   `api_key_lookup` policies:
   - **Policy.** A SELECT-only `integration_lookup` policy on
     `integrations_integration`:
     `USING (id = NULLIF(current_setting('app.current_integration_id', true), '')::uuid)`.
   - **Setting the id.** `core.tenancy.integration_lookup_atomic(integration_id)`
     is the only thing that sets it:
     - It validates the value as a UUID first. `uuid.UUID(str(...))`
       output is canonical hex, so the `SET LOCAL` literal is
       injection-safe.
     - It uses `transaction.atomic(durable=True)` + `SET LOCAL`.
     - It refuses to run inside a merchant context
       (`TenantContextError`).
   - **Reading the row.** `Integration.objects.for_lookup_id(id)` is the
     only merchant-unscoped `Integration` read. It works only inside
     `integration_lookup_atomic` for that same id.
   - **Why a non-secret key is acceptable.** Unlike the API-key hash, the
     id appears in the webhook URL, so it is not a secret. The lookup only
     reads that one row, and only to get `merchant_id`, `provider`,
     `status`, `config_json` and the encrypted secret that `verify()`
     needs. Nothing is returned to the caller and nothing is written until
     the signature verifies. An unknown id, a malformed id, a wrong
     provider, a `DISCONNECTED` integration, a missing signature and a bad
     signature all get the same `401 invalid_signature`. Nothing is
     stored for any of them.
     - There is one exception, which comes only *after* full
       verification *and* the signed-body shop binding: a Shopify
       `app/uninstalled` delivery for an already-`DISCONNECTED`
       integration returns `200` as an idempotent duplicate, and writes
       nothing (Decision 3, uninstall).
     - The lookup policy, the lookup order and every pre-verification
       rejection are unchanged.
   - **What does not change.** There is no write policy keyed on
     `app.current_integration_id`. No other table gets a lookup policy:
     mappings and events are read and written only after
     `tenant_context(integration.merchant_id)` is entered. There is no
     `SECURITY DEFINER` and no `BYPASSRLS`.
2. **The Shopify endpoint is `POST /webhooks/shopify/{integration_id}`**,
   not `POST /webhooks/shopify`. **It remains valid under the Shopify-app
   architecture (Decision 3).**
   - This URL is **application-created**. ReviewFlow registers it as a
     **shop-specific** subscription `uri` through
     `webhookSubscriptionCreate` during installation. Shopify documents
     that shop-specific subscription "configuration can differ per shop"
     (F12, F16).
   - It is **no longer** a workaround for manually created merchant
     webhooks; that approach is rejected (Decision 3).
   - The `{integration_id}` identifies the ReviewFlow `Integration` before
     any tenant context exists, through the Decision 1 lookup. The
     receiver also requires `X-Shopify-Shop-Domain` to equal the stored
     `shop_domain` (Decision 3).
   - **Trade-offs to note:**
     - Shopify recommends app-specific subscriptions unless delivery URIs
       must vary between shops, which they do here.
     - Failing **shop-specific** subscriptions are deleted by Shopify
       (F12). The receiver must therefore return `200` fast and never
       fail on business errors. The inbox design already does this.
     - The alternative, an app-specific single URI plus shop-domain
       identification, needs a second RLS lookup policy. It stays
       rejected for Phase 06. It would be revisited only for the
       compliance webhooks (Decision 3, out of scope).
   - `Webhook-Specification.md` §"Inbound Webhook Endpoints" is updated.
3. **Shopify integration uses the ReviewFlow Shopify app. Shopify webhook
   HMAC verification uses the ReviewFlow app client secret documented by
   Shopify.** This is a LOCKED DECISION, made by the user on 2026-09-24.
   - **Rejected approach (removed).** An earlier draft assumed the
     following, and it is **rejected**:
     - the merchant creates an `orders/paid` webhook manually in Shopify
       admin;
     - the merchant copies a signing secret from Shopify admin;
     - ReviewFlow stores it as `credentials.webhook_secret`.

     Shopify's authoritative documentation does not establish a
     merchant-copyable signing secret for admin-created webhooks. The
     documented HMAC key is the app's client secret (F1). The Help Center
     mentions only a store-unique signing ID, and does not say it is
     visible, copyable or the HMAC key (F5). Nothing in this spec accepts,
     stores or validates a merchant-supplied webhook secret.
   - **Credentials. The platform and merchant credentials never mix.**
     - **Platform (environment only, never in the database):**
       `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`.
       - Rotation needs a transition period (F11), so there is one more
         optional platform secret, `SHOPIFY_CLIENT_SECRET_PREVIOUS`. It is
         set only during a rotation (see "Client-secret rotation"). This
         is an addition, and it is flagged for the user.
       - All of them are read once at settings load, like every other
         platform secret (`Security-Controls.md` §Encryption & Secrets
         is updated).
     - **Per merchant**, in the merchant's `Integration`:
       - `credentials_encrypted` holds the Fernet-encrypted JSON of the
         installation credential that Shopify's token exchange returns
         (F9):
         `{ access_token, access_token_expires_at, refresh_token, refresh_token_expires_at, scope }`.
         The two `*_expires_at` values are computed at receipt from
         `expires_in` and `refresh_token_expires_in`.
       - `config_json` holds the non-secret identifiers:
         `{ shop_domain, shop_id, webhook_subscription_ids }`.
         - `shop_id` is the shop's numeric id (a digit string), obtained
           during installation (Decision 3, merchant flow step 5a).
         - All three keys are **server-managed**. `PATCH
           /integrations/{id}` on a `shopify` integration is rejected with
           `422` (Decision 6), so the stored shop identity cannot be
           edited.
     - **The app client secret is NEVER stored per merchant.** It is
       never in `Integration.credentials_encrypted`, `config_json`, any
       other table, a log or an audit row.
   - **App type (LOCKED by the user, 2026-09-24).** ReviewFlow's Shopify
     integration is a **standalone/API-only Shopify app, not an embedded
     Shopify Admin app** (F22).
     - It uses the OAuth authorization code grant (F8) and ReviewFlow's
       own hosted UI.
     - There is no App Bridge, no ID-token/token exchange and no embedded
       architecture.
     - F19's embedded-only rule (1.1.1, third-party cookies) does not
       apply.
   - **Distribution (verified).** ReviewFlow uses public distribution
     (F18) with **limited App Store visibility** (F21). Merchants install
     from the app's Shopify App Store listing URL. A limited-visibility
     app is still an App Store listing, so ReviewFlow treats the App Store
     requirements (F19) and the compliance-webhook requirement (F15) as
     applying in full. Nothing here depends on an "unlisted public app"
     mechanism.
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
            page. This is the app UI that F19 2.3.3 requires after OAuth.
        - The callback **never** reads `request.merchant_id`, the session
          merchant or any callback parameter to decide a merchant, and it
          creates **no** `Integration`. `shop` identifies the Shopify
          installation only. It never determines `merchant_id`.
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
     `customers/redact` and `shop/redact` (F15).
     - **Mandatory before Shopify App Store submission and review.** A
       limited-visibility listing does not bypass this (F15, F21).
     - **Not implemented in this branch, or in `06-shopify-app`.**
     - They can only be subscribed in the app configuration file, which
       means one URI for all shops. That needs a shop-domain →
       `Integration` lookup, a future RLS lookup-policy decision with
       sign-off.
     - Their data-deletion semantics belong with Phase 15, which is the
       proposed owner.
     - **Therefore Phase 15 (or whichever phase ownership is moved to) is
       a prerequisite for submitting the ReviewFlow Shopify app to the App
       Store.** Development-store installs are unaffected.
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
   - **Token lifetime.**
     - Public apps must use **expiring** offline tokens (F10). The access
       token lasts 1 hour, and the refresh token 90 days.
     - Phase 06 uses the access token only inside the install callback,
       to register the subscriptions, so it builds **no refresh job**.
     - `# ponytail:` stored tokens lapse after 90 days without a refresh.
       Any later Admin API use, such as re-registering or cleaning up
       subscriptions, must add refresh-before-use or re-authorize. Owner:
       the first later phase that calls the Admin API after install.
   - **Required scope:** `read_orders`, which is needed for `ORDERS_PAID`
     (F13). The callback rejects an install whose granted `scope` lacks
     it (F9).
   - **Protected customer data.**
     - Order name and phone are level-2 protected customer data. A public
       app without approval receives those fields as `null` (F17).
     - The pipeline still records the sale, as a phoneless `Transaction`
       (Phase 04 Decision 17), but no review request is possible for it.
     - Level-2 approval is therefore an **operational prerequisite for
       production value**. It is not code (U6).
     - Development stores skip review (F17).
   - **Authoritative findings.** From shopify.dev and help.shopify.com.
     F1–F23 were retrieved on 2026-09-24, except where a row states
     otherwise. F8 was re-read, and F24–F27 retrieved, on 2026-09-28, as
     their rows state. Each is a close paraphrase or a short quote, and
     each is copied into `Shopify.md`.

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
4. **The Generic REST API gets its own provider value, `api`.**
   - Every `IntegrationEvent` needs a non-null `integration_id`, but the
     `Data-Dictionary.md` provider enum has only 8 values, with no REST
     API entry. Adding `api` closes that gap. `Data-Dictionary.md`
     §Integration is updated.
   - A merchant connects it with `POST /integrations/api/connect` (no
     credentials) and maps locations through the existing
     `/integrations/{id}/locations` endpoints.
   - `UniqueConstraint(fields=["merchant"], condition=Q(provider="api",
     status="CONNECTED"))` allows at most one connected `api` integration
     per merchant. `POST /sales` uses that one.
   - The adapter lives in `integrations/api/`. The directory is added to
     the CLAUDE.md tree, the `SAD.md` §3 tree and the
     `Integration-Architecture.md` tree.
5. **Generic Webhook signing: HMAC over the raw body, with a
   server-generated secret.**
   - `connect` for `webhook` generates `whsec_` +
     `secrets.token_urlsafe(32)`, stores it Fernet-encrypted, and returns
     it **once**, as `webhook_secret` in the `201` connect response only.
     It is never shown again. Rotating it means connecting a new
     integration.
   - The sender signs every request with
     `X-ReviewFlow-Signature: sha256=<hex HMAC-SHA256(secret, raw body)>`.
   - A server-generated secret avoids weak, merchant-chosen secrets, and
     an HMAC authenticates the body as well as the sender.
   - Written into `Authentication.md` §3 and `Generic-Webhook.md`.
   - *Alternative (for the user to choose instead):* a static
     `X-ReviewFlow-Webhook-Secret` header token, compared with
     `hmac.compare_digest`. It is simpler for senders that cannot compute
     an HMAC, but it gives no body integrity.
6. **Adapter contract additions.** These are additions, not changes:
   `verify`/`parse`/`normalize` stay exactly as they are.
   `Integration-Architecture.md` §"The Adapter Contract" is updated.
   - `get_external_event_id(self, request, payload: dict) -> str`:
     receivers need the idempotency key at receipt time, before `parse()`
     runs in the task (Phase 04 deferred this hook to Phase 06). Raises
     `PayloadValidationError`.
   - `@classmethod validate_connection(cls, credentials: dict | None,
     config_json: dict | None) -> None`: provider-specific connect
     validation. Raises `django.core.exceptions.ValidationError` (→
     `422`). The default is a no-op. It runs on `connect` **and** on
     `PATCH /integrations/{id}` (with `credentials=None`, validating
     `config_json` only).
   - `@classmethod issue_credentials(cls) -> dict | None`:
     server-generated credentials. The default is `None`. Only `webhook`
     returns `{ "webhook_secret": ... }`.
   - `is_sale_event(self, request) -> bool`: the default is `True`.
     Shopify returns `request.headers["X-Shopify-Topic"] == "orders/paid"`.
   - `is_uninstall_event(self, request) -> bool`: the default is `False`.
     Shopify returns `X-Shopify-Topic == "app/uninstalled"`
     (Decision 3, uninstall).
   - The Shopify `connect` does not use `validate_connection` or
     `issue_credentials` with client-supplied credentials. Its
     credentials come only from the verified OAuth token exchange
     (Decision 3). Posting `credentials` to Shopify `connect` returns
     `422`.
   - A `shopify` integration's `config_json` (`shop_domain`, `shop_id`,
     `webhook_subscription_ids`) is entirely server-managed.
     `ShopifyAdapter.validate_connection(None, config_json)` rejects every
     `PATCH /integrations/{id}` for `shopify` with `422`, so the stored
     shop identity that the uninstall binding depends on can never be
     edited.
   - `is_uninstall_event(request)` only **routes** on the unsigned topic
     header. The destructive decision additionally requires
     `ShopifyAdapter.uninstall_payload_matches(integration, payload) ->
     bool`. That is the Phase 06 current-topic discriminator (Decision 3,
     step 2b), and it does not authenticate the topic header.
   - `verify()` is `return False` for `api` and `csv`, which have no
     webhook endpoint (fail closed). A receiver also rejects an
     integration whose `provider` differs from the URL's provider.
7. **Receipt-time idempotency key for sources without a native event id**
   (Generic Webhook, CSV, REST API):
   `external_event_id = sha256(f"{external_location_id or ''}\x1f{external_transaction_id}").hexdigest()`.
   - It is deterministic, so a re-delivery or re-upload of the same sale
     is a no-op at the inbox.
   - It is distinct per provider location, so two locations that share a
     transaction id do not collide at the event level while
     `Transaction` stays unique per location.
   - It always fits `CharField(255)`; a raw prefix plus a 255-character
     transaction id would not.
   - Shopify uses its dedupe header (Decision 3).
8. **`POST /sales`: synchronous pre-validation, then the unchanged Phase
   04 pipeline, inline.**
   - **Pre-validation is allowed, but it has no normalization of its
     own.**
     - `/sales` may validate synchronously and resolve the location before
       it creates the event, so a bad request gets `422` and no inbox row.
     - It calls **exactly the functions `process_event()` calls**:
       `get_adapter(integration)` (the registered `ApiAdapter`), then
       `adapter.parse(payload)`, then `adapter.normalize(...)`, then
       `integrations.services.resolve_location(integration, sale)`.
     - Field validation lives only in `ApiAdapter`, `SaleCreated` and
       `integrations/core/schemas.py`. The `/sales` serializer or view
       checks only that the body is a JSON object. It must not validate,
       coerce or default any sale field, and there is no second
       `SaleCreated` builder.
   - **`process_event()` remains the authoritative path.**
     - The pre-validation result (`SaleCreated`, `Location`) is used only
       to decide `422` and to compute the Decision 7 key. It is then
       discarded.
     - The `Transaction` is created only by `process_event(event.id)`,
       re-running `parse → normalize → resolve_location → record_sale` on
       the **stored** `IntegrationEvent.payload`, exactly as the Celery
       task and the Beat retry do.
     - The Phase 04 event state machine
       (`RECEIVED → PROCESSED | FAILED → … → DEAD_LETTER`,
       `RECEIVED/FAILED → CANCELLED`) is **unchanged**. `/sales` adds no
       status, no field and no transition.
   - **Request sequence.** Everything runs inside the request-wide
     `tenant_atomic()` that `TenantMiddleware` opens from the API key.
     1. Check that the body is a JSON object, else `422`.
     2. Find the merchant's `CONNECTED` `api` integration, else
        `422 integration_not_connected`.
     3. Run the pre-validation above. `PayloadValidationError` returns
        `422` with the safe error code; `LocationUnresolved` returns
        `422 location_unresolved`. **No inbox row is written for a `422`.**
     4. `record_event(integration, external_event_id=<Decision 7 key>,
        payload=body)` returns `(event, created)`.
     5. Only if `created`: `process_event(event.id)` runs inline. It opens
        its own savepoint through `tenant_atomic()`.
     6. Re-read `event.status`. If it is `PROCESSED`, look the
        `Transaction` up by `(event.location, sale.external_transaction_id)`.
     7. Respond as in the table below.
   - **Response codes**, decided only by `created` and the event's status
     after step 5:

     | Code | When | Body |
     |---|---|---|
     | `201` | `created = True` and the event is `PROCESSED` | `{ transaction_id, event_id }` |
     | `200` | `created = False` (replay: same key, same integration) and the stored event is `PROCESSED` | `{ transaction_id, event_id }` for the existing event and transaction |
     | `202` | The event is `FAILED` or `RECEIVED`, whether just created or replayed | `{ event_id, event_status, transaction_id: null }` |
     | `409 sale_not_processed` | The event is `DEAD_LETTER` or `CANCELLED`, which are terminal | `{ error: { code, message }, event_id, event_status }` |

     - **What `201` means.** `201` means this request's event was accepted
       and processed. Under the Phase 04 duplicate rule, `transaction_id`
       can reference a `Transaction` that another source (for example a
       CSV row) first recorded at the same location with the same
       `external_transaction_id`. The event is still `PROCESSED`, and no
       second `Transaction` exists.
     - **The replay code differs from the doc.** `API-Specification.md`
       wrote `201` for a replay. This spec uses `200`, so the caller can
       tell that nothing new was accepted. `API-Specification.md` is
       updated.
     - **What `202` means: the event is persisted, but the transaction is
       not yet available.**
       - The `IntegrationEvent` row is committed and visible in the inbox,
         with `event_status` `FAILED` or `RECEIVED`.
       - No `Transaction` exists for it yet.
       - The Phase 04 Beat job `retry_failed_events` (every 5 minutes)
         will process it with no client action:
         - a `FAILED` event is retried after its backoff,
           `5 min × 2^(attempt_count − 1)`;
         - a `RECEIVED` event is swept once it is more than 10 minutes
           old.
       - The client may replay the identical body later. It gets `200`
         with the `transaction_id` once the event is `PROCESSED`, `409` if
         the event became terminal, or `202` again while it is pending.
     - **How an inline failure becomes `FAILED`.**
       - Pre-validation passed, so an inline failure is either
         unexpected (for example a database error, giving
         `PROCESSING_ERROR`) or a race.
       - An example race: a concurrent `PUT /integrations/{id}/locations`
         deactivates the mapping between step 3 and step 5, giving
         `LOCATION_UNRESOLVED`.
       - `process_event` rolls back its inner savepoint, so no partial
         `Customer` or `Transaction` survives. It then runs the unchanged
         `_record_failure`: `attempt_count = 1`, the safe `error_code` and
         `error_message`, and `status = FAILED`.
       - It returns normally without raising, so the view returns `202`.
         `TenantMiddleware`'s `tenant_atomic()` commits on a normal
         response.
       - `ATOMIC_REQUESTS` is not enabled, so DRF's `set_rollback` does
         not roll it back.
       - The `FAILED` event is therefore durable and retryable.
       - A freshly created event cannot reach `DEAD_LETTER` inline,
         because that needs `MAX_EVENT_ATTEMPTS = 5` failures.
     - **When a `RECEIVED` event can remain pending.** The `/sales` path
       never *leaves* a new event `RECEIVED` on success or failure, because
       step 5 always moves it to `PROCESSED`, `FAILED` or `CANCELLED`.
       `RECEIVED` is observed only on a **replay** (`created = False`) of
       an event whose processing never ran. For example, an exception
       escaped `process_event` outside its `try` (such as a lock or
       connection error on the initial `select_for_update`). That aborts
       the whole request transaction, so the event itself is also rolled
       back, and a later replay creates it afresh. `202` + `RECEIVED` is
       therefore defensive handling for any event that is stored but
       unprocessed. It does not reflect a normal outcome.
     - **`409`.** `CANCELLED` arises inline only through the Phase 04
       race guard: the `api` integration was disconnected between step 2
       and step 5. `DEAD_LETTER` arises only on a replay of an event that
       exhausted its retries. Neither will ever produce a `Transaction`,
       so returning `202` would falsely promise processing.
     - An unexpected exception that escapes the view is a `500`. The
       request transaction rolls back, and nothing (including the event)
       is persisted.
   - **Ignored body fields.** A ReviewFlow `location_id`, `merchant_id`,
     `source` or `event` in the body is ignored. The location comes only
     from `IntegrationLocationMapping`, through `external_location_id`.
9. **Robust enqueue.** `record_event` registers its enqueue with
   `transaction.on_commit(..., robust=True)`. A broker outage then logs
   (exception class name only) instead of raising after commit, so a
   committed sale or webhook never returns `500`. The Phase 04
   stale-`RECEIVED` sweep (10 minutes) re-enqueues the event. The inline
   `/sales` path makes the later queued task a no-op on `PROCESSED`.
10. **CSV Import pipeline.**
    - **Upload.** `POST /integrations/{id}/csv-imports` (multipart
      `file`) is validated synchronously: size, UTF-8 encoding, header and
      **every row** through the `csv` adapter's `parse`/`normalize`. Any
      invalid row rejects the whole file with `422`, and nothing is stored
      or uploaded.
    - **Staging.** A valid file goes to R2 under
      `csv-imports/{merchant_id}/{uuid4}.csv`. The key is built
      server-side and never contains a client-supplied string.
    - **Processing.** The upload enqueues
      `import_csv(merchant_id, integration_id, object_key)` on queue
      `events`, on commit and robust. The task reads the object and calls
      `record_event` once per row (payload = the row dict, key =
      Decision 7). It deletes the object **only after every row is
      recorded**.
    - **Purge.**
      - Deleting in `finally` would lose the rest of a file whose task
        crashed midway, so the delete is not in `finally`.
      - A task that fails leaves the object in place and logs the error
        (exception class and object key only). The merchant re-uploads,
        which is idempotent through Decision 7.
      - An R2 bucket lifecycle rule (expire `csv-imports/` after 1 day) is
        the documented backstop. It is written into `CSV-Import.md` and
        `.env.example`.
      - No Celery autoretry and no `countdown`.
      - The comment is marked `# ponytail:`, naming the ceiling (a failed
        import needs a manual re-upload) and the upgrade path (an import
        status model).
    - **Location resolution** is **not** pre-validated at upload, because
      mappings can change. A row that cannot be resolved becomes a
      `FAILED` event and is retried by the Phase 04 Beat sweep, exactly
      like a webhook.
    - **Limits.** 5 MB, 10,000 data rows and UTF-8 (a BOM is allowed).
      The environment can override them (`CSV_IMPORT_MAX_BYTES`,
      `CSV_IMPORT_MAX_ROWS`).
11. **Pre-tenant receivers bypass session tenant wrapping.** If a request
    to a webhook receiver carried a dashboard session cookie,
    `SessionMerchantMiddleware` would set `request.merchant_id`,
    `TenantMiddleware` would wrap the request, and
    `integration_lookup_atomic` would then raise.
    - `SessionMerchantMiddleware` therefore also skips requests whose
      resolved `url_name` is `webhook-shopify` or `webhook-generic`. This
      extends its existing pre-tenant skip list, which currently matches
      by path.
    - The receivers do not opt into API keys, so `ApiKeyMiddleware` never
      acts on them.
    - Receiver views are `csrf_exempt`, with `authentication_classes = []`
      and `permission_classes = [AllowAny]`. The signature is the only
      authentication.
12. **Refund/void ingestion is deferred to Phase 11**, its first consumer.
    Phase 04 Decision 16 required refunds to be designed "with the first
    adapter that emits refunds (Phase 06), before Phase 11 depends on it".
    This spec names the owner explicitly instead: a `ROADMAP.md` §11
    bullet is added, "Refund/void ingestion: normalized refund event,
    `Transaction.status` update path, Shopify `refunds/create`, `/sales`
    refunds". Phase 06 writes only `COMPLETED` transactions. Shopify
    `refunds/create` and `orders/cancelled` are, like any non-`orders/paid`
    topic, acknowledged `200` and not stored.
13. **`GET /sales` needs the `transactions:read` scope.** The API spec
    names no scope; `transactions:read` is the matching one from the
    allowlist. The listing is merchant-wide: a key is a merchant principal
    and has no MANAGER location scope. It uses the same filters, body and
    cursor pagination as `GET /transactions`, through a key-scoped service
    that shares the filter code with `list_transactions`.
14. **`/sales` never accepts a session.** `SalesView` opts into keys with
    `ApiKeyOptInMixin` (`api_key_methods = {"GET", "POST"}`). Its
    permissions are **only** `HasApiKeyScope(...)`, with no member
    fallback. A session request, with no Bearer header, gets `403`
    (`Authentication.md`: never mix). `ApiKeyRateThrottle` applies.
15. **Webhook per-IP throttle.** Receivers are unauthenticated until
    verified, so they get `WebhookIpRateThrottle` (scope `webhook_ip`,
    env `WEBHOOK_IP_RATE`, default `1200/min`, keyed on DRF `get_ident()`
    under `NUM_PROXIES`). It runs before the lookup and returns `429` with
    `Retry-After`.
16. **Generic Webhook field mapping format** (`config_json`, validated by
    `validate_connection` on connect and on PATCH):
    ```json
    {
      "field_map": {
        "external_transaction_id": "order.id",
        "amount": "order.total",
        "currency": "order.currency",
        "occurred_at": "order.completed_at",
        "customer_phone": "customer.phone",
        "customer_name": "customer.name",
        "payment_method": "payment.method",
        "external_location_id": "store.id"
      },
      "default_currency": "INR"
    }
    ```
    - **Paths** are dot-separated keys, and a numeric segment indexes a
      list. There is no JSONPath library.
    - **Required mappings:** `external_transaction_id`, `amount` and
      `occurred_at`, plus `currency` or `default_currency`. Every other
      field is optional.
    - **Value handling:**
      - A path that resolves to nothing gives `None`. For a required
        field, that raises the field's `PayloadValidationError` code.
      - Scalars are converted with `str()`, except `amount`, which uses
        `Decimal(str(v))`.
      - `occurred_at` is parsed as ISO 8601 and must carry a timezone.
    - **Receipt-time id.** `get_external_event_id` resolves the transaction
      and location paths at receipt. If the transaction id is missing,
      the verified request is rejected with `422` and not stored, because
      it has no idempotency key.
    - **CSV and `/sales`** use fixed field names, the normalized ones
      above (`/sales` nests them as `customer: { name, phone }`, per the
      `SaleCreated` doc shape).
17. **Shopify fixture and field contract. VERIFY BEFORE IMPLEMENTATION.**
    The API version is settled (Decision 3: V8, U4 resolved), so the
    fixture is not blocked by it. The field table's VERIFY column still
    applies.
    - **Fixture provenance.**
      - The contract-test fixture is an anonymized copy of Shopify's own
        published `orders/paid` sample payload for the pinned
        `SHOPIFY_API_VERSION`. That is the version in the
        `webhookSubscriptionCreate` request URL, which determines the
        payload version of ReviewFlow's shop-specific subscriptions (F20).
      - `X-Shopify-API-Version` (F14, F20) on a development-store delivery
        is checked to equal it.
      - Without level-2 protected customer data approval (F17), a real
        delivery has `null` name and phone fields. The contract test uses
        Shopify's sample, where they are populated. A separate test
        covers the `null` case, which gives a phoneless sale.
      - The file name carries the version:
        `integrations/tests/fixtures/shopify_orders_paid_<api_version>.json`
        (for example `…_2025-07.json`; the real value comes from V8).
      - `Shopify.md` records the API version, the exact source URL, the
        retrieval date, and every anonymization edit. Names, phone,
        email and addresses are replaced with synthetic values; ids and
        amounts are kept or replaced consistently.
      - The fixture must not be hand-written from memory.
      - If Shopify publishes no sample payload for that topic and
        version, stop and ask the user.
    - **The contract-test assertion** is against a hand-written expected
      `SaleCreated` derived from that fixture. It is not produced by the
      adapter.
    - **Field table.**
      - The "Adapter rule" column is this spec's decision: what
        `ShopifyAdapter` does with the field.
      - The "Shopify guarantee" column must be filled in `Shopify.md` from
        the pinned version's documentation. It states whether the field
        is always present, nullable or conditional, its type and format,
        and any deprecation or version change.
      - The spec does not assume provider behavior. Every row is
        **VERIFY**.

      | Shopify field (`orders/paid` payload) | `SaleCreated` field | Adapter rule | Shopify guarantee |
      |---|---|---|---|
      | `id` | `external_transaction_id` (`str(id)`) | **Required.** Missing or empty gives `INVALID_EXTERNAL_TRANSACTION_ID` | VERIFY: type (integer or string), always present |
      | `total_price` | `amount` | **Required.** Parsed by `schemas.parse_amount`; missing or invalid gives `INVALID_AMOUNT` | VERIFY: string decimal format, and whether it is the right "sale total" (versus `current_total_price` or `total_price_set`) for the pinned version |
      | `currency` | `currency` | **Required.** Invalid gives `INVALID_CURRENCY` | VERIFY: ISO 4217, shop versus presentment currency semantics |
      | `processed_at` | `occurred_at` | **Primary.** It must be timezone-aware ISO 8601 | VERIFY: presence and nullability on `orders/paid`, timezone offset format |
      | `created_at` | `occurred_at` | **Fallback,** used only when `processed_at` is null or absent. If neither is usable: `INVALID_OCCURRED_AT` | VERIFY: always present, format |
      | `phone` (order level) | `customer_phone` | **Optional, primary.** Blank means `None` (Phase 04 Decision 17) | VERIFY: presence, whether Shopify guarantees E.164, deprecation status |
      | `customer.phone` | `customer_phone` | **Optional, fallback** when the order `phone` is blank. Blank means `None` | VERIFY: presence, whether `customer` can be null, E.164 guarantee, deprecation or version dependence |
      | `customer.first_name`, `customer.last_name` | `customer_name` | **Optional.** Joined with a space and trimmed; empty means `None`; truncated to 255. Ignored when there is no phone (Phase 04) | VERIFY: presence and nullability, deprecation or version dependence |
      | `payment_gateway_names` | `payment_method` | **Optional.** The first element truncated to 32 characters; an empty list or missing field means `None` | VERIFY: presence, list type, deprecation status for the pinned version |
      | `location_id` | `external_location_id` (`str(...)`) | **Optional.** Null or absent means `None`, and then single-mapping resolution applies (Phase 04 Decision 5) | VERIFY: when it is populated (POS versus online orders) and its deprecation status for the pinned version |

    - **Address phones are never used**: `billing_address.phone`,
      `shipping_address.phone` and similar. The adapter reads no field
      outside this table.
    - **E.164 stop rule.** A supplied phone that is not E.164 fails the
      whole sale (`INVALID_PHONE`, Phase 04 Decision 17, unchanged). If
      verification shows Shopify does **not** guarantee E.164 for `phone`
      or `customer.phone`, stop and ask the user. Do not add phone
      reformatting, and do not silently drop the phone.
    - **Other stop rules.** Stop and ask the user if verification shows
      any of the following:
      - a "Required" field can be absent on `orders/paid`;
      - a field in the table is deprecated or removed for the pinned
        version;
      - a fallback's semantics differ from the primary. For example, if
        `created_at` versus `processed_at` changes which time counts as
        the sale time, confirm it before implementation.
    - **Version drift.** A webhook arriving with a different API version
      is still processed by the same rules. `Shopify.md` notes that the
      fixture must be refreshed and re-verified whenever the pinned
      version changes.

## Source docs
- `docs/ROADMAP.md`: §06 (scope), §11 (refund owner, Decision 12), §17
  (phased adapters)
- `docs/FINAL-ARCHITECTURE-REVIEW.md`: §1 (integration scope and
  ownership), §5 (refunds, boundary only), §8 (RLS), §9 (retention);
  "Remaining work" (provider payload specs, verifying provider
  requirements)
- `docs/02-architecture/Architecture.md`: §"Foundational Decisions"
  (Cloudflare R2 for blobs)
- `docs/02-architecture/SAD.md`: §3 (app tree), §4 (adapter), §5
  (queues), R2 ("never business records")
- `docs/02-architecture/Multi-Tenancy.md`: §Layer 1, §Layer 2
  (`self_membership`, `api_key_lookup` precedents), §No Standing
  Privileged Role, §"`IntegrationEvent` — merchant identified before
  creation", §Required Tests 1–5
- `docs/02-architecture/Security-Architecture.md`: webhook verification
  per provider inside `verify()`
- `docs/03-database/Data-Dictionary.md`: §Integration (provider enum),
  §IntegrationLocationMapping, §IntegrationEvent, §Transaction, §ApiKey
  (scope allowlist)
- `docs/03-database/Database-Design.md`: §Integration, §IntegrationEvent
- `docs/04-api/API-Specification.md`: preamble, §Sales, §Transactions,
  §Integrations, §API Keys, §"Public-API authentication errors"
- `docs/04-api/Webhook-Specification.md`: pipeline, §Inbound Webhook
  Endpoints, §Fail-closed rule, §Idempotency, §Event Statuses, §Retries,
  §SaleCreated, §Security Notes
- `docs/04-api/Authentication.md`: §2 (opt-in, scopes, never mix,
  throttles), §3 (Shopify HMAC, generic shared secret, fail closed)
- `docs/05-integrations/Integration-Architecture.md`: all sections
- `docs/06-automation/Event-Processing.md`: §Pipeline, §Failure Handling,
  §Idempotency Guarantees
- `docs/01-product/Feature-Scope.md`: the nine V1 integrations, and
  scope vs. phase
- `docs/01-product/Business-Rules.md`: §2 rule 1 (E.164), §4 (refunds;
  deferred per Decision 12)
- `docs/09-security/Security-Controls.md`: §Encryption & Secrets,
  §Permissions & Access, §Rate Limiting (`schemas.py` validation),
  §Webhook Security, §File / Object Storage (CSV purge), §Row-Level
  Security
- `docs/09-security/Privacy-Data-Retention.md`: raw payloads 90 days;
  §Logging Rules
- `docs/09-security/Audit-Logging.md`: integration connected /
  disconnected
- `docs/10-development/Testing-Strategy.md`: §Tenant Isolation,
  §Duplicate Webhook Event, §Cross-Merchant Event Isolation, §Duplicate
  External Event ID Across Integrations, §Contract Tests for Adapters,
  §Final Consistency Tests (Multi-location integrations, RLS)
- `docs/10-development/Coding-Standards.md`: §1–§7
- `.claude/specs/04-event-inbox-ingestion-core.md`: Decisions 2, 4, 5, 7,
  9, 11, 16–20; "Open item carried to Phase 06"
- `.claude/specs/05-public-api-keys.md`: opt-in mechanism,
  `HasApiKeyScope`, throttles, Decisions 9 and 13

## Depends on
- Phase 04, Event inbox & ingestion core (Done): `Integration`,
  `IntegrationLocationMapping`, `IntegrationEvent`, `record_event`,
  `process_event`, `resolve_location`, `record_sale`,
  `retry_failed_events`, `BaseAdapter`, `SaleCreated`, `schemas.py`,
  `registry.py`, `/integrations`, `/transactions`
- Phase 05, Public API keys (Done): `ApiKey`, `ApiKeyOptInMixin`,
  `ApiKeyMiddleware`, `HasApiKeyScope`, `ApiKeyRateThrottle`, and the
  `api_key_lookup` precedent
- Phases 00–03 (Done): Celery `events` queue, tenancy/RLS helpers,
  `core.crypto`, roles, `AuditLog`, `Location`, `CursorPagination`
- The ROADMAP dependencies are "04 (+05, REST API)". Both are Done.

## Roadmap Phase
- Phase: 06 — Priority integrations
- Completes entire phase: No (**flagged for the user**; this was
  previously Yes)
- If No: remaining phase work: the **Shopify app integration**. It
  covers:
  - the install flow;
  - the OAuth callback;
  - the token exchange;
  - shop-specific webhook registration;
  - `orders/paid` ingestion;
  - `app/uninstalled` handling;
  - the Shopify fixture and contract test;
  - `Shopify.md`.

  It is proposed as its own spec, `06-shopify-app`, on its own branch,
  **No blocker remains:** U1, U2, U4 and U7 are resolved. It stays
  separate because the user has kept that separation; merging it into
  this branch is the user's choice. U1a and U9 are confirmed on a
  development store before it merges. U10 is an accepted V1 residual.
- **Why the answer changed (scope impact):**
  - ROADMAP §06 describes Shopify as a "Shopify adapter (HMAC
    signature)". The locked Shopify-app decision (Decision 3) makes it
    materially larger:
    - a merchant-facing OAuth install and callback, with state, HMAC and
      shop validation;
    - a token exchange, and storage of expiring tokens;
    - Admin GraphQL calls to register subscriptions (new outbound HTTP to
      Shopify);
    - uninstall handling;
    - two new platform secrets, plus a rotation procedure.
  - U1 (the Shopify-initiated install plus account linking) is now
    resolved, and it adds further scope: an install entry point, a
    pending installation, and a link confirmation step.
  - Keeping it separate keeps the Generic Webhook, CSV and `/sales`
    work reviewable on its own.
  - Nothing is silently dropped. Everything about Shopify stays specified
    here (Decisions 2, 3 and 17) as the input to `06-shopify-app`.
  - **If the user chooses to keep Shopify in this branch, this line
    becomes Yes.** That is a user decision, not the implementer's.
  - **Submission prerequisite.** Shipping `06-shopify-app` makes the app
    installable on development stores. Submitting it to the App Store
    also needs the compliance webhooks (proposed owner: Phase 15) and
    level-2 protected customer data approval (U6).
- This branch covers every other §06 bullet:
  - the Generic Webhook (field mapping in `config_json`);
  - CSV Import (R2 upload, purge after processing);
  - the Generic REST API (`POST /sales`, `GET /sales`);
  - fixture-based contract tests for those adapters;
  - `Generic-Webhook.md` and `CSV-Import.md`;
  - the shared receiver infrastructure: the `integration_lookup` policy,
    the receiver ordering and the throttle, which Shopify will reuse.
- `/ship-feature` must **not** mark Phase 06 Done after this branch.

## Locked decisions touched
- `Integration` merchant-level + `IntegrationLocationMapping` as the only
  integration↔location source (`FINAL-ARCHITECTURE-REVIEW.md` §1;
  `Integration-Architecture.md`) — DEPENDS ON
- `IntegrationEvent.merchant_id` resolved from `Integration` before
  creation; `UNIQUE(integration_id, external_event_id)`
  (`FINAL-ARCHITECTURE-REVIEW.md` §1; `Multi-Tenancy.md`) — DEPENDS ON
- `UNIQUE(location_id, external_transaction_id)` (`Event-Processing.md`)
  — DEPENDS ON
- Shared schema, `merchant_id` scoping, `TenantScopedManager`
  (`Multi-Tenancy.md` §Layer 1) — DEPENDS ON
- Transaction-local `SET LOCAL`, RLS on every tenant table
  (`FINAL-ARCHITECTURE-REVIEW.md` §8) — DEPENDS ON
- RLS as the final boundary; pre-tenant reads only via a signed-off
  SELECT-only policy (`Multi-Tenancy.md` §Layer 2) — LOCKED DECISION
  CHANGE
- No standing privileged role, no `BYPASSRLS`, no `SECURITY DEFINER`
  (`Multi-Tenancy.md` §No Standing Privileged Role) — NO CHANGE
- Adapter pattern: `verify`/`parse`/`normalize` + registry keyed on
  `Integration.provider` (`Integration-Architecture.md`; `SAD.md` §4) —
  DEPENDS ON (additive hooks only, Decision 6)
- Three auth mechanisms, never mixed; webhook signatures verified in the
  adapter, fail closed `401` (`Authentication.md`) — DEPENDS ON
- API-key opt-in per view/method; scopes named per endpoint (Phase 05;
  `Authentication.md` §2) — DEPENDS ON
- Celery merchant context: explicit `merchant_id` (`Multi-Tenancy.md`) —
  DEPENDS ON (`import_csv`)
- Queues `events`/`whatsapp`/`google_sync`/`default`; no broker
  `eta`/`countdown` (`SAD.md` §5; `Architecture.md` §4) — NO CHANGE
- Shopify integrates as the ReviewFlow Shopify app, with public
  distribution. Webhook HMAC is keyed with the platform
  `SHOPIFY_CLIENT_SECRET`, and the per-merchant OAuth credential is
  Fernet-encrypted in `Integration` (Decision 3) — **LOCKED by the user,
  2026-09-24**. It is recorded here as a new locked decision, not a
  change to an existing one. `Authentication.md` §3 ("verified against
  the integration's stored webhook secret") is updated to match.
- Fernet encryption of integration credentials (`Security-Controls.md`)
  — DEPENDS ON
- R2 holds blobs only; CSV uploads purged after processing (`SAD.md`;
  `Security-Controls.md` §File / Object Storage) — DEPENDS ON
- Retention: raw payloads 90 days (`FINAL-ARCHITECTURE-REVIEW.md` §9) —
  NO CHANGE (Phase 15)
- Refund handling (`FINAL-ARCHITECTURE-REVIEW.md` §5) — NO CHANGE
  (ingestion deferred to Phase 11, Decision 12)
- One review request per transaction, quota, WhatsApp, Google, billing —
  NO CHANGE
- Django Admin, no bespoke admin app (`Architecture.md` §6) — NO CHANGE

LOCKED DECISION CHANGE — USER SIGN-OFF REQUIRED

**What changes:** `integrations_integration` gets a second RLS policy next
to `tenant_isolation`. It is a SELECT-only `integration_lookup` policy,
`USING (id = NULLIF(current_setting('app.current_integration_id', true), '')::uuid)`.
The policy is set only through `core.tenancy.integration_lookup_atomic()`
(`SET LOCAL`, durable, refused inside a merchant context), and it is read
only through `Integration.objects.for_lookup_id()`. It is the third
instance of the signed-off `self_membership` / `api_key_lookup` pattern.

**Why:** a provider webhook identifies its `Integration` from the URL
before any merchant is known. `FORCE ROW LEVEL SECURITY` blocks that
read. `Multi-Tenancy.md` §Layer 2 allows pre-tenant reads only through a
signed-off policy. This is the "Open item carried to Phase 06" from spec
04.

**How it differs from `api_key_lookup`:** the key (an integration id) is
not secret, because it is in the webhook URL. The row is used only to
fetch the secret for `verify()`. No data from it is returned, and nothing
is written, before the signature verifies. Every failure mode returns the
identical `401` and stores nothing (Decision 1).

**Alternatives rejected:**
- A `SECURITY DEFINER` lookup function is a standing privileged path.
- A merchant id in the URL is request-supplied tenant data (CLAUDE.md:
  "never from request data").
- A shop-domain policy on `config_json` has a squatting risk
  (Decision 2).

**Invariants that must not be weakened:** SELECT-only; `SET LOCAL` only;
no write policy keyed on `app.current_integration_id`; `FORCE` stays on;
`for_lookup_id` is the only unscoped `Integration` read; no other table
gets a lookup policy. On sign-off, `Multi-Tenancy.md` §Layer 2 gains the
subsection "Integration lookup — `integration_lookup`".

## Django apps
- `integrations`: **touched**. Model enum + constraint, the lookup
  manager method, services (connect/PATCH validation hooks,
  `receive_webhook`, `ingest_api_sale`, `start_csv_import`,
  `import_csv_rows`), receiver views, `/sales` view, CSV upload view,
  tasks, throttles, and the new plain subpackages `shopify/`, `webhook/`,
  `csv_import/` and `api/` (each `__init__.py` + `adapter.py`, with no
  `apps.py` and no models).
- `events`: **touched**. `record_event` enqueue becomes `robust=True`
  (Decision 9).
- `transactions`: **touched**. `list_sales(...)`, the key-scoped listing
  that shares the filter helper with `list_transactions` (Decision 13).
- `core`: **touched**. `integration_lookup_atomic` +
  `get_current_lookup_integration_id` (`core/tenancy.py`),
  `rls_select_by_integration_id` (`core/rls.py`), and a new
  `core/storage.py` (a thin R2 client: `put_object`, `get_object`,
  `delete_object`).
- `accounts`: **touched**. `SessionMerchantMiddleware` skips the webhook
  receivers (Decision 11).
- `apikeys`: used (`ApiKeyOptInMixin`, `HasApiKeyScope`,
  `ApiKeyRateThrottle`); code unchanged.
- `customers`, `locations`, `auditlog`: used only.
- `config`: **touched**. URL includes, throttle rates, R2 and CSV
  settings.

## Models & database changes

### `integrations.Integration` (changed)
- `Provider` gains `API = "api", "Generic REST API"`. It stays lowercase,
  per Phase 04 Decision 15. `Data-Dictionary.md` §Integration lists `api`.
- New constraint (idempotent connect for the one REST integration):
  `UniqueConstraint(fields=["merchant"], condition=Q(provider="api", status="CONNECTED"), name="uniq_integration_merchant_connected_api")`.
  `connect_integration` catches `IntegrityError` in a savepoint and
  raises `IntegrationExists` (`409 integration_exists`). This is not
  check-then-insert.
- `IntegrationManager(TenantScopedManager)` becomes `objects` and adds
  `for_lookup_id(integration_id)`.
  - It raises `TenantContextError` unless
    `get_current_lookup_integration_id() == integration_id`.
  - It then returns
    `super(TenantScopedManager, self).get_queryset().filter(pk=integration_id)`.
    This is exactly the `for_lookup_hash` pattern.

### RLS
- `integration_lookup`: SELECT-only on `integrations_integration`
  (LOCKED DECISION CHANGE above), via
  `core.rls.rls_select_by_integration_id("integrations_integration")`,
  which is reversible.
- `tenant_isolation` on every existing table is unchanged. No new tables.

### Migrations (one logical change each; nothing dropped)
1. `integrations/0003_integration_provider_api.py`: the `provider`
   choices gain `api`.
2. `integrations/0004_integration_uniq_connected_api.py`: the partial
   unique constraint.
3. `integrations/0005_integration_lookup_rls.py`: the `integration_lookup`
   policy.

### Idempotency
- Shopify: `UNIQUE(integration_id, external_event_id)` with the Shopify
  dedupe header value.
- Webhook, CSV and `/sales`: the same constraint with the Decision 7
  sha256 key.
- `UNIQUE(location_id, external_transaction_id)` still backstops every
  path. Re-uploading a CSV and re-posting `/sales` are no-ops.
- One connected `api` integration per merchant: the partial unique
  constraint above.

## API endpoints
Base `/api/v1/`. Error shape `{ error: { code, message, field_errors? } }`.

**Provider webhooks** (signature auth only, CSRF-exempt, no session and no
API key, `WebhookIpRateThrottle`):
- `POST /webhooks/shopify/{integration_id}`: the application-created
  shop-specific subscription URI for `orders/paid` and `app/uninstalled`
  (Decisions 2 and 3). It is part of the `06-shopify-app` scope (see
  Roadmap Phase).
  - Auth: `X-Shopify-Hmac-Sha256`, which is
    `base64(HMAC-SHA256(SHOPIFY_CLIENT_SECRET, raw body))`, also accepting
    `SHOPIFY_CLIENT_SECRET_PREVIOUS` during a rotation. The comparison is
    constant-time. After that, `X-Shopify-Shop-Domain` must equal the
    stored `shop_domain`.
  - `200 {}` when stored or when a duplicate. A duplicate is a no-op:
    `record_event` returns `created=False` and nothing is enqueued.
  - `200 {}` for a verified `app/uninstalled` whose **signed body**
    passes the Phase 06 current-topic discriminator: an integer `id`, no
    Order keys, and `str(id) == config_json.shop_id` (Decision 3, step
    2b). This separates it from a signed `orders/paid` body. It does not
    authenticate the topic header.
    - On a `CONNECTED` integration it is a one-time system disconnect
      with one audit row.
    - On an already-`DISCONNECTED` integration it is an idempotent
      duplicate that changes nothing and writes no audit row.
    - A failed binding returns `401`, and nothing changes.
  - `200 {}` for any other verified topic that is not `orders/paid`. It is
    not stored.
  - `401 invalid_signature`, identical for all of the following, with
    nothing stored: an unknown or malformed id, a non-`shopify`
    integration, a missing or invalid HMAC, a shop-domain mismatch, a
    missing `X-Shopify-Webhook-Id`, an `app/uninstalled` whose body fails
    the current-topic discriminator, or a `DISCONNECTED` integration for any
    topic **other than** a verified, bound `app/uninstalled`.
  - `400 invalid_payload` for a verified non-JSON body or a body that is
    not a JSON object. Nothing is stored.
  - `429` with `Retry-After` when throttled.
- `POST /webhooks/generic/{integration_id}`: Generic Webhook.
  - Auth: `X-ReviewFlow-Signature: sha256=<hex>` (Decision 5).
  - `200 {}` when stored or when a duplicate.
  - `401` exactly as for Shopify.
  - `400 invalid_payload` for a verified body that is not a JSON object.
  - `422` with the safe error code when the mapped
    `external_transaction_id` is missing at receipt. Nothing is stored.
  - `429` when throttled.

**Generic REST API** (`Authorization: Bearer rf_live_...` only, per
Decision 14; `ApiKeyRateThrottle`):
- `POST /sales`: scope `sales:write`.
  - Request: `{ external_transaction_id, amount, currency, occurred_at,
    customer?: { name?, phone? }, payment_method?, external_location_id? }`
    (the `SaleCreated` shape). `merchant_id`, `location_id`, `source` and
    `event` are ignored.
  - Decision 8's response table is normative:
    - `201 { transaction_id, event_id }`: new event, `PROCESSED`.
    - `200 { transaction_id, event_id }`: replay, `PROCESSED`.
    - `202 { event_id, event_status, transaction_id: null }`: event
      `FAILED` or `RECEIVED`. It is persisted, and the transaction is not
      yet available.
    - `409 sale_not_processed` with `event_id` and `event_status`: event
      `DEAD_LETTER` or `CANCELLED`.
  - `422` for a body that is not a JSON object, a payload validation error
    (`invalid_phone`, `invalid_amount`, … mapped from the safe error
    codes), `location_unresolved`, or `integration_not_connected` (no
    `CONNECTED` `api` integration).
  - `401 invalid_api_key`.
  - `403` for a key without the scope, or a session request.
  - `429` when throttled.
- `GET /sales`: scope `transactions:read`.
  - Filters, body and cursor pagination as `GET /transactions`
    (`location_id`, `date_from`, `date_to`, `status`; `limit` max 100),
    merchant-wide.
  - `200 { results, next_cursor }`.
  - `401`, `403` and `429` as above.

**Dashboard** (session + CSRF, OWNER/ADMIN; MANAGER/VIEWER get `403`):
- `POST /integrations/{provider}/connect` (**changed**): provider-specific
  validation (Decision 6).
  - `shopify`: `credentials.webhook_secret` is **rejected and removed**.
    The Shopify flow is below. Until `06-shopify-app` ships, `shopify` is
    not registered, so `connect` returns `422` as for any unregistered
    provider.
  - `webhook` requires a valid `config_json.field_map` and **rejects**
    client-supplied `credentials`: the secret is server-issued.
  - `csv` and `api` reject any `credentials`.
  - `api` returns `409 integration_exists` if one is already `CONNECTED`.
  - The `201` body for `webhook` alone adds `webhook_secret`, shown once.
    No other response ever includes it.
  - Every other error is unchanged.
- `PATCH /integrations/{id}` (**changed**): `config_json` is validated by
  the provider's `validate_connection` (`422`). A `shopify` integration's
  `config_json` is server-managed, so its `PATCH` always returns `422`.
- `POST /integrations/{id}/csv-imports` (**new**): multipart `file`.
  - The integration must be `csv` and `CONNECTED`.
  - `202 { rows }` once the file is staged and the import task is queued.
  - `404` for a cross-merchant or missing id.
  - `422` for any of:
    - the wrong provider, or a `DISCONNECTED` integration;
    - no file, an empty file, over 5 MB, or over 10,000 rows;
    - a file that is not UTF-8;
    - a missing required column (`external_transaction_id`, `amount`,
      `currency`, `occurred_at`);
    - any invalid row.

    For invalid rows, `field_errors` holds `{ "row_<n>": [<safe code>] }`
    for the first 50 bad rows. Row numbers are 1-based data rows, and the
    safe codes are the same ones the pipeline stores.
  - Optional columns: `customer_phone`, `customer_name`,
    `payment_method`, `external_location_id`. Unknown columns are
    ignored.

**Shopify app installation** (the `06-shopify-app` scope; a standalone
app using the authorization code grant, per Decision 3). The endpoint
names are ReviewFlow's own; the Shopify URLs and parameters are the
verified ones (F8, F9, F16, F20, F23).
- **No merchant-entered shop domain anywhere.** The
  `POST /integrations/shopify/connect { shop }` from an earlier draft is
  withdrawn (F19 2.3.1). The sequence, state handling and security
  argument are normative in Decision 3, merchant flow, steps 1–4.
- `GET /integrations/shopify/install`: the App URL.
  - Unauthenticated, pre-tenant, and per-IP throttled
    (`WebhookIpRateThrottle`).
  - `shop` must match the regex, else `400 shopify_install_invalid`.
  - Stores `shopify_oauth = { state, shop, issued_at }` in the session.
  - Returns `302` to Shopify's `/admin/oauth/authorize` with
    `client_id`, `scope=read_orders`, `redirect_uri` and `state`.
- `GET /integrations/shopify/callback`: Shopify's redirect.
  - The `hmac` check (with a dual secret during a rotation), then
    `state` against the **popped** session nonce. The nonce must be
    unexpired (10 minutes) and its `shop` must match.
  - Then the `shop` regex, then the token exchange (`expiring=1`) and
    the `read_orders` scope check.
  - On success it stores the encrypted `shopify_pending` (15 minutes)
    and returns `302` to the ReviewFlow link page.
  - It **never creates an `Integration`**, and never reads a merchant.
  - Failures: `400 shopify_install_invalid` or `502
    shopify_unavailable`. Nothing is stored.
- `GET /integrations/shopify/pending`: session, OWNER/ADMIN.
  - Returns `200 { shop }`, or `404` if nothing is pending or it has
    expired.
- `POST /integrations/shopify/link`: session + CSRF, OWNER/ADMIN, empty
  body.
  - Pops `shopify_pending` and runs the lifecycle below under the
    **session** merchant.
  - Returns `201` with the Integration body, `409
    shopify_install_expired`, or `502 shopify_unavailable`.
  - MANAGER and VIEWER get `403` without consuming the pending
    installation.
- **Lifecycle** (inside `POST /integrations/shopify/link`; Decision 3,
  merchant-flow steps 5–8):
  0. **Shop identity.** Run GraphQL `{ shop { id myshopifyDomain } }`
     with the access token.
     - `myshopifyDomain` must equal the pending `shop`, else `400
       shopify_install_invalid`.
     - The digits of `gid://shopify/Shop/<digits>` become `shop_id`.
     - A failure returns `400 shopify_install_invalid` or `502
       shopify_unavailable`, and nothing is stored (Decision 3, step
       5a).
  1. **Provisional.** Open a savepoint inside the request transaction.
     Insert the `Integration` for the authenticated merchant:
     - `provider=shopify`, `status=CONNECTED`;
     - `credentials_encrypted` =
       `encrypt({ access_token, access_token_expires_at, refresh_token, refresh_token_expires_at, scope })`;
     - `config_json = { shop_domain, shop_id }`.

     The row stays uncommitted, so the webhook receiver's lookup cannot
     see it yet (`401`).
  2. **Register**, using that `Integration` id, through
     `POST https://{shop}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/graphql.json`
     with `X-Shopify-Access-Token` (F16, F20):
     - `webhookSubscriptionCreate(topic: ORDERS_PAID, webhookSubscription: { uri: <public base>/api/v1/webhooks/shopify/{integration.id} })`;
     - the same for `APP_UNINSTALLED`.
  3. **Finalize.** Save `config_json.webhook_subscription_ids`. Write the
     `integration.connected` audit row (metadata: provider and
     `shop_domain` only). Release the savepoint. The `Integration` is
     visible and `CONNECTED` once the request commits.
  4. **On any failure in steps 2–3:**
     - Best-effort `webhookSubscriptionDelete(id)` for each subscription
       already created (F23).
     - **Roll back the savepoint**, so neither the provisional
       `Integration` nor its audit row remains.
     - Return `502 shopify_unavailable`.
     - A delete that itself fails is logged (subscription id and
       exception class only). Shopify's deletion of failing shop-specific
       subscriptions (F12) is the backstop, not the design.
- Then redirect to the dashboard integration page. Location mappings use
  the existing `/integrations/{id}/locations` endpoints: one mapping
  without `external_location_id` for online-only, or POS `location_id`s
  (Decision 17).

Environment: `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`,
`SHOPIFY_CLIENT_SECRET_PREVIOUS` (optional), `SHOPIFY_API_VERSION` (the
version pinned in the GraphQL request URL, which determines the
subscriptions' payload version, per F20) and `SHOPIFY_REDIRECT_URI`.
Outbound calls use stdlib `urllib.request` with explicit timeouts, so
there is no new dependency.
The `/transactions` endpoints are unchanged.

## Services & background tasks

### `core/tenancy.py`
- `get_current_lookup_integration_id() -> uuid.UUID | None`
- `integration_lookup_atomic(integration_id)`: a context manager.
  - It validates with `uuid.UUID(str(...))` (`ValueError` otherwise).
  - It refuses to run inside a merchant context (`TenantContextError`).
  - It opens `transaction.atomic(durable=True)` with
    `SET LOCAL app.current_integration_id = '<uuid>'`.
  - It resets the contextvar on exit. It mirrors `api_key_lookup_atomic`.

### `core/rls.py`
- `rls_select_by_integration_id(table, column="id")`: the SELECT-only
  `integration_lookup` policy. Never add a write policy keyed on it.

### `core/storage.py`
- `put_object(key: str, data: bytes) -> None`,
  `get_object(key: str) -> bytes` and `delete_object(key: str) -> None`,
  over a lazily built `boto3` S3 client.
- The client uses `endpoint_url=R2_ENDPOINT_URL`, R2 credentials from the
  environment and `R2_BUCKET`.
- It holds blobs only. Nothing is logged except the key and the
  exception class.
- Tests replace it with an in-memory fake fixture and never touch the
  network.

### `integrations/core/adapters.py` (additive)
`get_external_event_id`, `validate_connection`, `issue_credentials` and
`is_sale_event`, with the defaults described in Decision 6.
`get_external_event_id` has no default: it is abstract for new adapters.
The existing test stubs, which subclass `BaseAdapter`, gain a trivial
implementation. That is a stub update, not a weakened test.

### `integrations/core/schemas.py` (additive)
- `sale_event_key(external_location_id, external_transaction_id) -> str`:
  Decision 7.
- `parse_amount(value) -> Decimal`: raises `INVALID_AMOUNT`.
- `parse_occurred_at(value) -> datetime`: ISO 8601, timezone-aware, else
  `INVALID_OCCURRED_AT`.
- `resolve_path(obj, dotted_path)`: Decision 16. It returns `None` when
  the path is missing.
- `verify_hmac_sha256(secret: str, body: bytes, signature: str, *,
  encoding: "base64" | "hex") -> bool`: uses `hmac.compare_digest`, and
  returns `False` for a missing or malformed signature (fail closed).

### Adapters (each `integrations/<provider>/adapter.py`, registered in `registry.ADAPTERS`)
- **`ShopifyAdapter`** (`shopify`):
  - **Scope and gate.** This is `06-shopify-app` scope (see Roadmap
    Phase). It is not written until Decision 17's field table and the
    Decision 3 findings are complete in `Shopify.md`. U1 is resolved,
    and no blocker remains.
  - `verify`: `base64(HMAC-SHA256(settings.SHOPIFY_CLIENT_SECRET,
    request.body))` over the **raw** body, exactly as received, compared
    with `X-Shopify-Hmac-Sha256` using `hmac.compare_digest`. It also
    accepts `SHOPIFY_CLIENT_SECRET_PREVIOUS` when that is set (F11). A
    missing header returns `False`.
    - The adapter never reads a key from
      `Integration.credentials_encrypted`. The merchant credential there
      is an OAuth token, never an HMAC key.
    - After the HMAC, it requires
      `X-Shopify-Shop-Domain == integration.config_json["shop_domain"]`.
      This is defense in depth only, because the header is not covered
      by the HMAC (F1).
  - `is_sale_event`: `X-Shopify-Topic == "orders/paid"` (F13; the header
    value is confirmed on a development-store delivery, V5).
  - `is_uninstall_event`: `X-Shopify-Topic == "app/uninstalled"` (F13).
    It **routes only**, and never authorizes on its own.
  - `uninstall_payload_matches(integration, payload: dict) -> bool`:
    - `payload` is a dict with an integer top-level `id`;
    - none of `line_items`, `order_number`, `financial_status` or
      `total_price` is present;
    - `str(payload["id"]) == integration.config_json["shop_id"]`.

    It is the Phase 06 **current-topic discriminator** that gates the
    destructive uninstall (Decision 3, step 2b; F25, F26).
    - It separates the subscribed `orders/paid` Order payload from the
      `app/uninstalled` Shop payload, and binds the result to `shop_id`.
    - It does **not** authenticate `X-Shopify-Topic`, and it is not a
      general proof of an `app/uninstalled` body.
    - It must be re-evaluated before any Shopify topic is added
      (Decision 3, "Re-evaluation rule").
  - `validate_connection(None, config_json)`: every `PATCH` for
    `shopify` returns `422`, because its `config_json` is server-managed.
  - `get_external_event_id`: `X-Shopify-Webhook-Id` (F2, F14: the
    delivery-level dedupe id; never `X-Shopify-Event-Id`). Missing means
    `401`. Its stability across retries is U3; it is non-blocking because
    of the transaction backstop.
  - `validate_connection`: rejects any client `credentials` (`422`). The
    Shopify credential comes only from the OAuth token exchange.
  - `parse`/`normalize` follow **exactly** the Decision 17 field table,
    with its Required/Optional/Fallback rules. The adapter reads no other
    field, and never an address phone. The field list here is not
    repeated, so the two cannot drift.
- **`GenericWebhookAdapter`** (`webhook`):
  - `verify`: hex HMAC in `X-ReviewFlow-Signature`, with the `sha256=`
    prefix required.
  - `issue_credentials`: `{ "webhook_secret": "whsec_" + token_urlsafe(32) }`.
  - `validate_connection`: the Decision 16 `field_map` rules. It rejects
    client credentials.
  - `get_external_event_id`: `sale_event_key` over the mapped paths.
  - `parse`/`normalize`: the field map.
- **`CsvImportAdapter`** (`csv`):
  - `verify` returns `False`.
  - `validate_connection` rejects credentials.
  - `parse`/`normalize` take the row dict: blanks become `None`, then the
    shared `schemas` parsers.
  - `get_external_event_id` is `sale_event_key` over the row.
- **`ApiAdapter`** (`api`):
  - `verify` returns `False`.
  - `validate_connection` rejects credentials.
  - `parse`/`normalize` take the `/sales` JSON object (`customer` is
    nested).
  - `get_external_event_id` is `sale_event_key`.

### `integrations/exceptions.py` (additive)
- `WebhookRejected`: `401 invalid_signature`
- `InvalidWebhookPayload`: `400 invalid_payload`
- `IntegrationExists`: `409 integration_exists`
- `IntegrationNotConnected`: `422 integration_not_connected`
- `06-shopify-app` scope:
  - `ShopifyInstallInvalid`: `400 shopify_install_invalid`;
  - `ShopifyInstallExpired`: `409 shopify_install_expired`;
  - `ShopifyUnavailable`: `502 shopify_unavailable`.
- `SaleNotProcessed`: `409 sale_not_processed`. It carries `event_id` and
  `event_status`, which are rendered next to the standard `error` object
  (Decision 8).
- `LocationUnresolved` gains the HTTP mapping `422 location_unresolved`.
  It is used only by `/sales`; the pipeline behavior is unchanged.

### `integrations/services.py`
- `connect_integration(...)` (**changed**):
  - Calls `adapter_cls.validate_connection(credentials, config_json)`.
  - Takes `credentials = adapter_cls.issue_credentials() or credentials`.
  - Inserts in a savepoint. `IntegrityError` on the `api` constraint
    raises `IntegrationExists`.
  - Returns `tuple[Integration, dict | None]`. The second item is the
    issued plaintext credentials, which the view returns once.
  - Audit metadata still holds only the provider.
- `update_integration_config(...)` (**changed**): calls
  `validate_connection(None, config_json)` first.
- `disconnect_integration(*, actor: TeamMember | None, integration, reason:
  str | None = None)` (**changed**, `06-shopify-app` scope):
  - `actor=None` means a system disconnect. The audit row has no actor
    user; `auditlog.record()` already accepts `actor=None`. Metadata
    gains `reason` when one is given.
  - The existing behavior (lock, idempotence, cancelling pending events,
    clearing credentials) is unchanged.
- **Shopify install services** (`integrations/shopify/services.py`,
  `06-shopify-app` scope), which implement the "Shopify app
  installation" steps under API endpoints:
  - Session access happens only in these services. Views pass
    `request.session` in and never interpret its contents.
  - `begin_shopify_install(*, session, shop) -> str`:
    - validates the `shop` regex;
    - stores `shopify_oauth = { state, shop, issued_at }`;
    - returns the `authorize_url`.

    It never takes a merchant, and it touches no tenant data.
  - `complete_shopify_oauth(*, session, query: dict) -> None`:
    - pops `shopify_oauth`;
    - verifies the query `hmac`, the `state` (`compare_digest`), the
      10-minute expiry, the `shop` match and the regex;
    - exchanges the code and checks the scope;
    - stores the Fernet-encrypted `shopify_pending = { shop,
      credentials_fernet, issued_at }`.

    It creates no `Integration`, and it never reads a merchant.
  - `pending_shopify_shop(*, session) -> str | None`: returns the
    unexpired pending `shop`, or `None`.
  - `link_shopify_installation(*, actor: TeamMember, session) -> Integration`:
    - pops `shopify_pending`, or raises `ShopifyInstallExpired`
      (`409`);
    - decrypts the bundle;
    - runs the provisional → register → finalize lifecycle under
      `get_current_merchant_id()` (the session merchant). On failure it
      rolls the savepoint back after best-effort deletion of the
      subscriptions (Decision 3, steps 5–8).
  - None of these services accepts a merchant-entered shop domain or a
    merchant id from Shopify parameters.
  - `verify_shopify_query_hmac(query: dict) -> bool`
  - `exchange_code(shop, code) -> dict`
  - `register_webhooks(shop, access_token, integration_id) -> list[str]`
  - `delete_webhooks(shop, access_token, subscription_ids) -> None`:
    best-effort cleanup (F23), used only on a registration failure.

  Every outbound HTTP call goes through one small `_shopify_post(url,
  body, headers)` helper (stdlib `urllib.request`, explicit timeout). It
  never logs the token, the code, the client secret or a response body.
  Tests replace it with a fake.
- `receive_webhook(*, provider: str, integration_id: str, request) -> None`.
  It must be called with **no** tenant context, and it runs these steps:
  1. `with integration_lookup_atomic(id)`: read `for_lookup_id(id).first()`.
     A malformed id raises `WebhookRejected`. The lookup transaction
     commits here.
  2. The row is missing, or `provider != provider`: raises
     `WebhookRejected`.
  3. `adapter.verify(request)` is false: raises `WebhookRejected`, and a
     warning is logged with the integration id only.
     - For Shopify this is the platform-secret HMAC plus the shop-domain
       binding, and it runs whatever the `Integration` status.
     - A `DISCONNECTED` Generic Webhook has no stored secret (credentials
       are cleared on disconnect), so its `verify()` fails closed here.
  4. `adapter.is_uninstall_event(request)` is true (Shopify only; the
     header only routes here):
     - decode the JSON body. If it is not a dict, or
       `adapter.uninstall_payload_matches(integration, payload)` is
       false, raise `WebhookRejected` (`401`). Nothing is read or
       changed;
     - if `status == DISCONNECTED`, return without any change: an
       idempotent duplicate with no audit row;
     - otherwise, within `tenant_context(integration.merchant_id)`, call
       `disconnect_integration(actor=None, integration=...,
       reason="app_uninstalled")`, which no-ops under its row lock if a
       concurrent delivery won. Then return.
  5. `status == DISCONNECTED` for any other topic: raises
     `WebhookRejected` (`401`).
     `adapter.is_sale_event(request)` is false: returns without storing.
  6. `json.loads(request.body)` must be a `dict`, else
     `InvalidWebhookPayload`.
  7. `external_event_id = adapter.get_external_event_id(request, payload)`.
  8. `with tenant_context(integration.merchant_id)`:
     `record_event(integration=..., external_event_id=..., payload=...)`.
     `IntegrationDisconnected`, from a disconnect racing this request,
     raises `WebhookRejected`.
- `ingest_api_sale(*, payload) -> tuple[IntegrationEvent, Transaction | None, bool]`.
  It runs in the key's tenant context, following the Decision 8 request
  sequence exactly:
  - Gets the merchant's `CONNECTED` `api` integration, or raises
    `IntegrationNotConnected`.
  - Pre-validates through `get_adapter(integration)`: `.parse`, then
    `.normalize`, then `resolve_location`. These are the same objects
    and functions `process_event` uses. There is no other validation or
    normalization code, and the result is discarded after the `422`
    decision and the key computation.
  - Calls `record_event`, then `process_event(event.id)` only when the
    event was created. `process_event` is the only code path that
    creates the `Transaction`.
  - When the event is `PROCESSED`, looks the `Transaction` up by
    `(event.location, external_transaction_id)`.
  - Returns `(event, transaction_or_None, created)`.
  - The view maps the result to `201`/`200`/`202`/`409` purely by
    `created` and `event.status`, per the Decision 8 table. The view
    holds no status logic beyond that mapping.
- `start_csv_import(*, integration, file) -> int`:
  - Validates the provider and status, the size, UTF-8, the header and
    every row (Decision 10).
  - Calls `storage.put_object(csv-imports/{merchant_id}/{uuid4}.csv)`.
  - Registers the robust on-commit `import_csv.delay(merchant_id,
    integration.id, key)`.
  - Returns the row count.
- `import_csv_rows(*, integration_id, object_key) -> None`: reads the
  object, then calls `record_event` per row, then `delete_object`. A
  missing object (already purged) is a no-op. A disconnected integration
  deletes the object and records nothing.

### `integrations/tasks.py`
- `import_csv(merchant_id, integration_id, object_key)`:
  `@shared_task(queue="events")` + `@tenant_task`, a thin wrapper around
  `import_csv_rows`. It has no autoretry, no `eta` and no `countdown`.

### `events/services.py`
- `record_event`: `transaction.on_commit(..., robust=True)` (Decision 9).
  No other change.

### `transactions/services.py`
- `list_sales(*, location_id=None, date_from=None, date_to=None, status=None)`:
  a merchant-wide tenant-scoped queryset. It uses the same filter helper
  as `list_transactions`, which is extracted, not duplicated.

### Throttling (`integrations/throttling.py`)
- `WebhookIpRateThrottle(SimpleRateThrottle)`: scope `webhook_ip`, keyed
  on `get_ident()`.

### Celery Beat
No new schedule. The Phase 04 `retry-failed-events` job covers every
source.

## Admin
No admin changes. This follows the Phase 04/05 precedent: tenant models
are not registered until the Phase 16 audited privileged path exists.

## Files to change
- `integrations/models.py`: the `API` provider, the partial constraint and
  `IntegrationManager.for_lookup_id`
- `integrations/core/adapters.py`, `integrations/core/schemas.py`,
  `integrations/core/registry.py` (register the four adapters)
- `integrations/services.py`, `integrations/exceptions.py`,
  `integrations/serializers.py` (the connect response's one-time
  `webhook_secret`), `integrations/views.py`, `integrations/urls.py`
- `events/services.py`: `robust=True`
- `transactions/services.py`: `list_sales` + the shared filter helper
- `core/tenancy.py`, `core/rls.py`
- `accounts/middleware.py`: skip the webhook receivers by resolved
  `url_name` (Decision 11)
- `config/settings.py`:
  - the `webhook_ip` throttle rate (`WEBHOOK_IP_RATE`);
  - `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
    `R2_BUCKET`;
  - `CSV_IMPORT_MAX_BYTES` (default 5 MB) and `CSV_IMPORT_MAX_ROWS`
    (default 10000).
- `config/urls.py`: include the webhook and `/sales` routes, if they are
  not under `integrations.urls`
- `requirements.txt`: add `boto3`
- `.env.example`: the new env vars and the R2 lifecycle-rule note
- **Existing tests whose expectations legitimately change**, because real
  adapters are now registered or the contract grew. None of these is a
  weakened test:
  - `integrations/tests/test_core.py:148–154`: the "unregistered" example
    provider changes from `shopify` to `woocommerce` (Phase 17).
  - `integrations/tests/test_api.py:76`
    (`test_connect_rejects_unregistered_provider`): `webhook` becomes
    `woocommerce`.
  - `apikeys/tests/test_bearer_auth.py:275`: the opt-in set becomes
    `{(MerchantView, "GET"), (SalesView, "GET"), (SalesView, "POST")}`.
  - Test stub adapters in `events/tests/conftest.py`,
    `integrations/tests/conftest.py`, `integrations/tests/test_core.py`
    and `transactions/tests/conftest.py` gain `get_external_event_id`.
- Docs:
  - `docs/05-integrations/Integration-Architecture.md`: the contract
    additions, the `api/` directory, and the lookup reference.
  - `docs/04-api/Webhook-Specification.md`: the Shopify and generic
    endpoint paths and response codes, and the unsupported-topic rule.
  - `docs/04-api/API-Specification.md`: §Sales (the full contract,
    replay `200`, `202`, scopes), §Integrations (per-provider connect,
    one-time `webhook_secret`, `409`, the `csv-imports` endpoint).
  - `docs/04-api/Authentication.md` §3: the generic-webhook HMAC scheme
    and the per-integration Shopify URL.
  - `docs/03-database/Data-Dictionary.md` §Integration: the `api`
    provider and the partial constraint.
  - `docs/02-architecture/Multi-Tenancy.md` §Layer 2: the
    `integration_lookup` subsection, **only after sign-off**.
  - `docs/02-architecture/SAD.md` §3 and `CLAUDE.md` Project Structure:
    `integrations/api/`.
  - `docs/ROADMAP.md` §11: the refund/void ingestion bullet
    (Decision 12).
- **`06-shopify-app` scope** (Decision 3; not in this branch unless the
  user decides otherwise):
  - `config/settings.py`: `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`,
    `SHOPIFY_CLIENT_SECRET_PREVIOUS` (optional), `SHOPIFY_API_VERSION`
    and `SHOPIFY_REDIRECT_URI`, read from the environment only.
  - `.env.example`: the same names, with no values.
  - `integrations/services.py`: `disconnect_integration` gains
    `actor: TeamMember | None` and `reason`.
  - `docs/04-api/Authentication.md` §3: replace "verified against the
    integration's stored webhook secret" with the app client-secret rule
    and the rotation dual-accept.
  - `docs/09-security/Security-Controls.md` §Encryption & Secrets: add
    the `SHOPIFY_CLIENT_*` platform secrets.
  - `docs/04-api/API-Specification.md` §Integrations: the Shopify install
    and callback endpoints.

## Files to create
- `integrations/shopify/__init__.py`, `integrations/shopify/adapter.py`,
  `integrations/shopify/services.py` (`06-shopify-app` scope)
- `integrations/webhook/__init__.py`, `integrations/webhook/adapter.py`
- `integrations/csv_import/__init__.py`,
  `integrations/csv_import/adapter.py`
- `integrations/api/__init__.py`, `integrations/api/adapter.py`
- `integrations/receivers.py`: `ShopifyWebhookView` and
  `GenericWebhookView`, thin views calling `receive_webhook`
- `integrations/tasks.py`, `integrations/throttling.py`
- `integrations/migrations/0003_integration_provider_api.py`,
  `0004_integration_uniq_connected_api.py`,
  `0005_integration_lookup_rls.py`
- `core/storage.py`
- `integrations/tests/fixtures/shopify_orders_paid_<api_version>.json`:
  Shopify's published `orders/paid` sample for the pinned API version
  (the `SHOPIFY_API_VERSION` in the GraphQL request URL, per Decision 3
  V8 and F20), anonymized. Its provenance (version, source URL,
  retrieval date, anonymization edits) is recorded in `Shopify.md`
  (Decision 17). This is `06-shopify-app` scope.
- `docs/05-integrations/Shopify.md` must open with the following, all
  **before** the Shopify code is written:
  - Decision 3's findings F1–F27 (the complete set, including F24);
  - the V1–V10 status and the U1–U10 table (including U1a);
  - the install, OAuth `state` and account-linking sequence, with each
    step marked as Shopify-required or a ReviewFlow decision (Decision
    3, merchant flow);
  - the rotation procedure;
  - the Decision 17 field table.
- `integrations/tests/fixtures/generic_webhook.json`,
  `integrations/tests/fixtures/sales.csv`
- `integrations/tests/test_lookup_rls.py`, `test_shopify.py`,
  `test_generic_webhook.py`, `test_csv_import.py`, `test_sales_api.py`,
  `test_connect_providers.py`
- `docs/05-integrations/Shopify.md`, `Generic-Webhook.md`, `CSV-Import.md`

## New dependencies
- **`boto3`**: the S3-compatible client for Cloudflare R2 (CSV staging;
  reused by QR images in Phase 13). This is the one new package. The
  standard library has no S3 client, and `SAD.md` fixes R2 as blob
  storage. It is pinned in `requirements.txt`.
- Everything else is stdlib: `hmac`, `hashlib`, `base64`, `csv`, `io`,
  `json`, `secrets`.

## Rules for implementation
- Django + DRF monolith. Business logic lives only in `services.py`.
  Views, receivers and tasks are thin.
- Every tenant-owned model uses `core.TenantScopedManager`, and RLS stays
  enabled and forced. `for_lookup_id` is the only unscoped `Integration`
  read, and it works only inside `integration_lookup_atomic` for that id.
- `integration_lookup_atomic`:
  - never runs inside a merchant context;
  - is always `durable=True`;
  - uses `SET LOCAL` only;
  - never has a write policy keyed on `app.current_integration_id`.
- Never trust a client-supplied `merchant_id`/`location_id`:
  - for webhooks, the merchant comes from the looked-up `Integration`;
  - for `/sales`, it comes from the API key;
  - the location always comes from `IntegrationLocationMapping`.
- **Receiver order is fixed, and it is exactly `receive_webhook()`:**
  1. lookup (Decision 1);
  2. provider check;
  3. `verify()` over the raw `request.body`. For Shopify this also covers
     the `X-Shopify-Shop-Domain` header check, whatever the status;
  4. **Shopify `app/uninstalled` path** (routed by the unsigned topic
     header):
     - the signed-body current-topic discriminator
       (`uninstall_payload_matches`, valid for the Phase 06 topic set only)
       must pass, else `401`;
     - `DISCONNECTED` returns `200` with no change;
     - `CONNECTED` performs one system disconnect and returns `200`;
  5. `DISCONNECTED` rejection, `401`, for every other topic;
  6. sale-topic filter (a non-sale topic returns `200` and is not
     stored);
  7. JSON decode;
  8. event id;
  9. `tenant_context`;
  10. `record_event`.

  Nothing is stored before verification, and every rejection before
  verification is the identical `401`. A destructive action is never
  authorized by an unsigned header alone.
- Signature comparison always uses `hmac.compare_digest`, and a missing
  signature fails closed.
- Celery tasks take `merchant_id` explicitly as the first argument
  (`tenant_task`). There is no `eta`, no `countdown` and no autoretry.
- Idempotency comes from database unique constraints plus a savepoint and
  `IntegrityError`. There is no check-then-insert.
- Role checks use DRF permission classes: `IsOwnerOrAdmin` for
  `/integrations/*`. For `/sales`, the only permission is `HasApiKeyScope`,
  with no session fallback.
- External IDs are UUIDs.
- Secrets:
  - The Shopify and generic webhook secrets are Fernet-encrypted in
    `credentials_encrypted`.
  - The generic secret is returned only in the connect `201`.
  - They are never logged, audited or listed.
  - R2 credentials come from env vars only.
- Never log phone numbers, secrets, signatures, raw payloads or CSV
  content. Log identifiers, safe codes and exception class names only.
- Status enums are `UPPER_SNAKE_CASE`, timestamps end in `_at`, and
  provider ids are lowercase.
- No V2 features and no bespoke admin app. Do not implement WooCommerce,
  Petpooja, GoFrugal, Zapier, Make or refund ingestion.
- Every new service function has a unit test. Every adapter has a
  fixture-based contract test. Every receiver has signature,
  duplicate-delivery and tenant tests.
- **The Shopify verification gate (Decisions 3 and 17) is a hard
  precondition.**
  - The architecture is locked: the ReviewFlow Shopify app, with the app
    client secret as the HMAC key.
  - No Shopify code or tests are written until:
    - Decision 3 is complete. U1, U2, U4 and U7 are resolved. U3, U5
      and U6 are non-blocking. U1a and U9 are confirmed before merge.
      U10 is an accepted V1 residual. U8 and the compliance webhooks
      gate App Store submission only;
    - `Shopify.md` holds F1–F27 (the complete set, including F24), the
      V1–V10 status and the Decision 17 field table.
  - Never accept a merchant-entered `myshopify.com` URL or shop domain
    (F19 2.3.1).
  - Shopify is `06-shopify-app` scope unless the user moves it back
    (Roadmap Phase).
  - Never accept, store or validate a merchant-supplied webhook secret.
  - Never store `SHOPIFY_CLIENT_SECRET` (or `_PREVIOUS`) in the
    database. That includes `Integration.credentials_encrypted`,
    `config_json`, logs and audit rows.
  - `credentials_encrypted` holds only the per-merchant OAuth credential.
  - The Shopify `Integration`'s merchant always comes from the
    session of the OWNER/ADMIN who confirms `POST
    /integrations/shopify/link`, never from Shopify's `shop` or any
    other callback parameter. The callback never creates an
    `Integration`.
  - OAuth `state` is session-held, single-use (popped) and expires
    after 10 minutes. The pending installation is session-held,
    single-use, expires after 15 minutes, and its token bundle is
    Fernet-encrypted.
  - Installation follows the Decision 3 lifecycle: a provisional
    savepoint insert, registration with that id, finalize, or a
    savepoint rollback plus best-effort `webhookSubscriptionDelete`
    on failure. No new `Integration` status is added, and no
    `CONNECTED` row survives a failed install.
  - Never log OAuth codes, access tokens, refresh tokens, the client
    secret, HMACs or Shopify response bodies.
  - Never answer a verification item from memory. If any stop rule
    fires, stop and ask the user; do not improvise.
  - The Generic Webhook, CSV, `/sales` and the lookup policy proceed
    independently of Shopify.
- **There is one normalization path per provider**: its adapter's
  `parse`/`normalize` plus `SaleCreated`/`schemas.py`. `/sales`
  pre-validation and CSV upload validation call the registered adapter
  through `get_adapter`. They never re-implement field parsing.
  `process_event()` is the only code that creates a `Transaction` from an
  event.
- The Phase 04 `IntegrationEvent` state machine is unchanged: no new
  status, field or transition.

## Definition of done
All verified by `pytest` against real PostgreSQL, with Celery eager,
unless noted. R2 is replaced by an in-memory fake.

**Migrations and RLS**
- [ ] `migrate` applies `integrations` 0003–0005. `migrate integrations
      0002` reverses them cleanly. `makemigrations --check` is clean.
- [ ] `integrations_integration` has exactly `tenant_isolation` and the
      SELECT-only `integration_lookup` policy (`pg_policies`). ENABLE and
      FORCE are still on.
- [ ] Inside `integration_lookup_atomic(id_A)`:
  - a raw `SELECT *` returns only row A, with no other integration or
    merchant visible;
  - a raw `UPDATE`/`DELETE` affects 0 rows;
  - `integrations_integrationlocationmapping` and
    `events_integrationevent` return 0 rows.
- [ ] `integration_lookup_atomic` raises `TenantContextError` inside a
      tenant context, `ValueError` for a non-UUID, and `RuntimeError`
      when nested (durable).
- [ ] `for_lookup_id` raises `TenantContextError` outside the lookup
      context or for a different id.
- [ ] After the lookup block, a new transaction on the same connection
      sees no integration rows (pooled-connection test).
- [ ] The partial constraint means a second `CONNECTED` `api` integration
      for the same merchant returns `409`. After a disconnect, a
      reconnect succeeds. Two merchants can each have one.

**Webhook receivers (Shopify and Generic)**

Items naming Shopify are **`06-shopify-app` scope**. In those items, "a
valid signed" delivery means one HMAC-signed with the test
`SHOPIFY_CLIENT_SECRET` (Decision 3), carrying the integration's
`X-Shopify-Shop-Domain`. The "Shopify app" block further down adds the
Shopify-specific requirements. The Generic Webhook items belong to this
branch.
- [ ] A valid signed Shopify `orders/paid` fixture returns `200`. It
      creates one `IntegrationEvent` (`merchant_id ==
      integration.merchant_id`, `source="shopify"`, `external_event_id` =
      the dedupe header). Processing it yields one `COMPLETED`
      `Transaction` at the mapped location.
- [ ] **Fail closed:** each of the following returns the identical `401`
      body, and no `IntegrationEvent` is created:
  - a missing HMAC, a wrong HMAC, or an HMAC over a modified body;
  - an unknown or malformed `integration_id`;
  - a `webhook` integration id on the Shopify URL (and the reverse);
  - a `DISCONNECTED` integration, for any delivery other than a verified
    Shopify `app/uninstalled` (see the Shopify app block);
  - a Shopify request missing the dedupe header.
- [ ] A verified non-`orders/paid` topic (for example `refunds/create`)
      returns `200` and stores nothing.
- [ ] A verified non-JSON or non-object body returns `400` and stores
      nothing.
- [ ] **Duplicate Webhook Event (priority scenario):** the same Shopify
      delivery posted 5 times gives one event and one `Transaction`, and
      deliveries 2–5 enqueue nothing. The same generic payload posted
      twice gives one event.
- [ ] **Cross-Merchant Event Isolation (priority scenario):** merchants A
      and B each receive a Shopify delivery with the same dedupe id. That
      creates two events, each with its own merchant.
- [ ] **Duplicate External Event ID Across Integrations:** two Shopify
      integrations under one merchant, with the same dedupe id, create two
      events.
- [ ] **Tenant attribution (generic webhook):** a delivery signed with
      merchant B's secret but sent to merchant A's integration URL returns
      `401`. A valid delivery to A's URL never creates rows under B, even
      when the payload contains B's ids. (The Shopify form is in the
      "Shopify app" block below.)
- [ ] A webhook request that also carries a logged-in dashboard session
      cookie (and a Bearer header) returns the normal `200`/`401`, never
      `500`. It is not tenant-wrapped (Decision 11).
- [ ] The generic webhook with a valid `sha256=` HMAC and a nested
      `field_map` produces the mapped `Transaction`. A missing mapped
      transaction id returns `422` and stores nothing. A payload whose
      mapped phone is invalid is stored and then ends `FAILED`
      `INVALID_PHONE` (pipeline behavior unchanged).
- [ ] The multi-location Shopify fixture (POS `location_id`) resolves to
      the mapping whose `external_location_id` matches. An unmapped
      location ends `FAILED` `LOCATION_UNRESOLVED`.
- [ ] `WebhookIpRateThrottle`: with a low override, the (N+1)th request
      returns `429` with `Retry-After` before any lookup runs.

**Adapter contract tests** (`Testing-Strategy.md` §Contract Tests)
- [ ] **Shopify gate (manual review of the doc; this happens before the
      Shopify code, not at the end):**
  - Decision 3 records the U1 resolution, with Shopify-required and
    ReviewFlow-decided steps distinguished.
  - `Shopify.md` holds findings F1–F27 (the complete set, including
    F24), the V1–V10 status, the rotation
    procedure and the Decision 17 field table.
  - Every row cites an authoritative Shopify source URL and its date or
    API version.
  - No stop rule fired, or the user resolved it in writing.
- [ ] The fixture file name carries the pinned API version, and
      `Shopify.md` records its source URL, retrieval date and
      anonymization edits.
- [ ] The Shopify fixture is normalized to an exact, hand-written
      expected `SaleCreated`: id, amount, currency, `occurred_at`
      (tz-aware), phone (E.164 or `None`), name, payment method and
      `external_location_id`.
- [ ] Field-rule tests, one per row of the Decision 17 table, each
      starting from the fixture with that field changed:
  - a missing `id`, `total_price` or `currency` gives the table's safe
    code;
  - a null `processed_at` falls back to `created_at`, and both null give
    `INVALID_OCCURRED_AT`;
  - a blank order `phone` falls back to `customer.phone`;
  - a null `customer` gives no phone and no name;
  - an empty `payment_gateway_names` gives `payment_method = None`;
  - a missing `location_id` gives `external_location_id = None`;
  - a phone present only in `billing_address` or `shipping_address`
    gives `customer_phone = None`.
- [ ] Generic Webhook, CSV and `ApiAdapter` fixtures each normalize to an
      exact expected `SaleCreated`.
- [ ] `verify()` returns `False` for `csv` and `api`.
- [ ] `sale_event_key` is deterministic, 64 hex characters long, and
      differs for the same transaction id at two `external_location_id`s.

**Connect / PATCH**
- [ ] Connecting `webhook` returns `201` with `webhook_secret` matching
      `^whsec_`. The stored credentials decrypt to it. `GET /integrations`
      and `PATCH` responses never contain it. The audit metadata holds
      only the provider.
- [ ] Connecting `webhook` with client `credentials`, or with an
      invalid/missing `field_map`, returns `422`. `PATCH` with an invalid
      `field_map` returns `422` and changes nothing.
- [ ] In this branch, `shopify` is unregistered, so its `connect`
      returns `422`. The Shopify connect checks are in the "Shopify app"
      block.
- [ ] Connecting `csv` or `api` with any `credentials` returns `422`.
      Connecting `woocommerce` still returns `422` (unregistered, Phase
      17).

**Shopify app (`06-shopify-app` scope; a precondition is the Shopify gate
above).** Shopify HTTP calls are replaced by a fake `_shopify_post`, and
installs are checked by hand on a Shopify development store where noted.
- [ ] **Verified app installation.**
  - `GET /integrations/shopify/install?shop=<valid>` returns `302` to
    `https://<shop>/admin/oauth/authorize` with exactly `client_id`,
    `scope=read_orders`, the configured `redirect_uri` and a fresh
    `state`. The session holds `shopify_oauth` with that `state`. An
    invalid `shop` returns `400`.
  - No endpoint accepts a merchant-entered shop domain, and client
    `credentials` for `shopify` return `422`.
  - The callback rejects each of the following with `400
    shopify_install_invalid`, storing nothing:
    - a bad query `hmac`;
    - a missing, wrong or reused `state` (a second callback with the
      same `state` fails);
    - a `state` older than 10 minutes;
    - a callback `shop` different from the nonce's `shop`, or one that
      fails the regex;
    - a callback arriving in a different browser session from the
      install;
    - a granted scope missing `read_orders`.
  - A successful callback creates **no** `Integration`, even when the
    browser is logged in as an OWNER. It stores only an encrypted
    `shopify_pending`, whose token is not readable in the session
    store, and redirects to the link page.
  - **U1a:** a development-store install started from the Shopify App
    Store listing or admin reaches `GET /integrations/shopify/install`
    with `shop`. The observed parameters are recorded in `Shopify.md`.
    The install then completes end to end through linking.
- [ ] **Account linking is explicit and authenticated.**
  - `POST /integrations/shopify/link` without a session returns `403`.
    A MANAGER or VIEWER gets `403`, and the pending installation
    remains. A missing CSRF token returns `403`.
  - An anonymous callback followed by login (password, and TOTP when
    enrolled) keeps `shopify_pending`. The OWNER's `GET
    /integrations/shopify/pending` returns the `shop`, and `POST
    /integrations/shopify/link` returns `201` for the session
    merchant.
  - A second `POST /integrations/shopify/link` returns `409
    shopify_install_expired` (single-use). A pending installation
    older than 15 minutes returns `409`.
  - `shop`, `merchant_id` or `credentials` in the link body are
    ignored.
  - **Attach-to-arbitrary-merchant attempts fail:**
    - merchant B's OWNER, logged in with a different browser session,
      cannot link a pending installation created in merchant A's
      browser (`409`, nothing created);
    - a pending installation is never visible to `GET
      /integrations/shopify/pending` from another session.
- [ ] **Verified merchant credential storage.** After a successful
      link:
  - `credentials_encrypted` decrypts to exactly `{ access_token,
    access_token_expires_at, refresh_token, refresh_token_expires_at,
    scope }`;
  - `config_json` holds `shop_domain`, `shop_id` and
    `webhook_subscription_ids`.
    - `shop_id` equals the digits of the `shop { id }` GID returned by
      the faked GraphQL call.
    - A `myshopifyDomain` that differs from the pending `shop` returns
      `400`, as does a malformed GID. Both store nothing and create no
      subscription.
    - `PATCH /integrations/{id}` on the Shopify integration returns
      `422`, and `config_json` is unchanged;
  - the `Integration` belongs to the linking session's merchant, never
    one derived from Shopify parameters;
  - no token appears in any API response, log or audit row.
- [ ] **Verified webhook registration.** The fake records exactly two
      `webhookSubscriptionCreate` calls, `ORDERS_PAID` and
      `APP_UNINSTALLED`, each with
      `uri = …/api/v1/webhooks/shopify/{integration.id}` and the
      `X-Shopify-Access-Token` header, against the pinned
      `SHOPIFY_API_VERSION` request URL. A development-store install
      shows both subscriptions, and the first delivery's
      `X-Shopify-API-Version` equals the pinned version.
- [ ] **Registration failure leaves nothing behind** (Decision 3,
      step 8):
  - If the second `webhookSubscriptionCreate` fails (`userErrors`,
    then separately a transport error or timeout), the response is
    `502 shopify_unavailable`.
  - The fake records one `webhookSubscriptionDelete` for the first
    subscription's id.
  - After the request, **no** `Integration` row exists (none
    `CONNECTED`, none provisional), and no `integration.connected`
    audit row exists.
  - With the delete also forced to fail, the result is the same `502`
    with no `Integration` row, and one log record carries only the
    subscription id and the exception class.
  - A delivery to the would-be `uri` then gets `401`.
- [ ] **The provisional row is invisible.** A receiver lookup of the
      `Integration` id, run on a second connection while the install
      transaction is still open, returns `401`. After commit, the same
      delivery returns `200`.
- [ ] **Valid `orders/paid` delivery.** A delivery HMAC-signed with the
      test `SHOPIFY_CLIENT_SECRET` over the raw fixture body, with the
      matching `X-Shopify-Shop-Domain`, returns `200` and produces one
      `IntegrationEvent` and one `Transaction`.
- [ ] **HMAC uses the platform secret only.**
  - A delivery signed with any other key returns `401`, including a
    value placed in the integration's credentials.
  - Re-serializing the JSON (whitespace or key order changed) after
    signing returns `401`, which proves raw-body verification.
  - With `SHOPIFY_CLIENT_SECRET_PREVIOUS` set, deliveries signed with
    either secret pass. With it unset, the old secret fails.
  - The OAuth callback `hmac` follows the same rule.
- [ ] **Shop binding.** A correctly signed delivery whose
      `X-Shopify-Shop-Domain` is another shop (for example merchant B's)
      returns `401` on merchant A's URL, and no rows are created.
- [ ] **Duplicate delivery.** The same `X-Shopify-Webhook-Id` posted 5
      times gives one event and one `Transaction`. Two different webhook
      ids for the same order give two events and one `Transaction`
      (Phase 04 backstop). `X-Shopify-Event-Id` is never used as the key.
- [ ] **Uninstall (documented boundary, idempotent).**
  - A validly signed `app/uninstalled` delivery with the matching shop
    domain returns `200` for a `CONNECTED` integration. The integration
    becomes `DISCONNECTED`, with credentials cleared, mappings inactive,
    pending events `CANCELLED` and exactly one audit row (`actor` null,
    `reason: app_uninstalled`).
  - **Repeat:** the same valid `app/uninstalled` sent again (same or new
    `X-Shopify-Webhook-Id`) returns `200`. It changes nothing, and the
    audit row count stays **1**.
  - **Concurrent:** two valid `app/uninstalled` deliveries in parallel
    threads both return `200`, and exactly one audit row exists.
  - **Still fail-closed on a `DISCONNECTED` integration:**
    - an `app/uninstalled` with an invalid HMAC returns `401`;
    - one with a mismatched `X-Shopify-Shop-Domain` returns `401`;
    - an unknown or malformed id returns `401`;
    - a `webhook` integration id on the Shopify URL returns `401`.
  - A validly signed `orders/paid` to a `DISCONNECTED` integration returns
    `401`, and no `IntegrationEvent` is created.
  - **Regression: topic relabelling cannot uninstall.** Take the Shopify
    `orders/paid` fixture body, validly HMAC-signed with the test
    `SHOPIFY_CLIENT_SECRET` and carrying the matching
    `X-Shopify-Shop-Domain`. Send it to a `CONNECTED` integration with
    `X-Shopify-Topic: app/uninstalled`. The response is `401`. The
    integration stays `CONNECTED`, its credentials and mappings are
    unchanged, and **no** audit row or `IntegrationEvent` is written.
  - The regression above is the property that this check proves: the
    Phase 06 current-topic discriminator separates `orders/paid` from
    `app/uninstalled`. No test may claim that the topic header is
    authenticated.
  - **Binding failures** each return `401` with no state change and no
    audit row:
    - a validly signed Shop payload whose `id` is another shop's id;
    - a Shop payload whose `id` is a string, or is missing;
    - a Shop payload that also carries `line_items`;
    - a body that is not a JSON object.
  - A validly signed Shop payload with the stored `shop_id`, but
    `X-Shopify-Shop-Domain` set to another shop, returns `401`: the
    defense-in-depth header check still applies.
  - **U9 (required before `06-shopify-app` merges):** on a development
    store, a real uninstall delivers a body that passes
    `uninstall_payload_matches`, and the observed fields are recorded in
    `Shopify.md`.
  - `Shopify.md` records the re-evaluation rule: the discriminator must be
    re-evaluated before any Shopify topic is added.
  - The compliance webhooks are **not** present (out of scope,
    Decision 3).
- [ ] **Tenant isolation.** Merchant A's install never creates or updates
      rows under merchant B, even when A's session is used with a
      callback `shop` previously connected by B. A delivery to A's URL
      never writes under B. RLS is unchanged.
- [ ] **No merchant-supplied webhook secret.** No code path, serializer
      field or test accepts `webhook_secret` for `shopify`. A grep for
      `webhook_secret` in `integrations/shopify/` is empty.
- [ ] **No app client secret in the database.** After install, uninstall
      and rotation tests, no `integrations_integration` row (in
      `credentials_encrypted` decrypted, or in `config_json`), audit
      metadata or `caplog` record contains the `SHOPIFY_CLIENT_SECRET` or
      `_PREVIOUS` value.

**CSV Import**
- [ ] A valid 3-row CSV upload as OWNER or ADMIN returns `202 {rows: 3}`.
      The object is staged under `csv-imports/{merchant_id}/…`. The task
      records 3 events, which produce 3 `Transaction`s, and the object is
      deleted afterwards.
- [ ] Re-uploading the same file creates no new events or `Transaction`s.
- [ ] Each of the following returns `422` and stores or uploads nothing:
  - over the size or row limit;
  - a file that is not UTF-8;
  - a missing required column;
  - a bad row (`field_errors` names the row and the safe code);
  - a `DISCONNECTED` or non-`csv` integration.
- [ ] MANAGER and VIEWER get `403`. A cross-merchant integration id
      returns `404`. A missing CSRF token returns `403`.
- [ ] If the task fails midway (a forced exception after N rows), the
      object is **not** deleted and the recorded rows are kept. Re-running
      the task completes the file with no duplicates, then deletes the
      object. Running the task against an already-deleted object is a
      no-op.
- [ ] A row with a blank phone creates a `Transaction` with
      `customer = NULL`. A row with an unmapped `external_location_id`
      ends `FAILED` `LOCATION_UNRESOLVED`.
- [ ] **Celery task tenant context:**
      `import_csv(merchant_A, integration_of_B, key)` reads and writes
      none of B's rows.

**Generic REST API**
- [ ] `POST /sales` with a `sales:write` key returns `201 {transaction_id,
      event_id}`. The `Transaction` has the key's merchant and the mapped
      location.
- [ ] Replaying the same body returns `200` with the same ids and no new
      rows.
- [ ] The same `external_transaction_id` at two different
      `external_location_id`s creates two `Transaction`s.
- [ ] A body `location_id`/`merchant_id` pointing at another merchant's
      location is ignored. The sale lands only at the key merchant's
      mapped location, or fails `422 location_unresolved`.
- [ ] An invalid phone, a bad amount/currency/`occurred_at`, a missing
      `external_transaction_id`, `location_unresolved`, or no `CONNECTED`
      `api` integration each return `422` and **create no
      `IntegrationEvent`**. A missing or blank phone returns `201` with a
      customer-less `Transaction`.
- [ ] **`202`, inline failure → `FAILED`.** With `record_sale` forced to
      raise inside `process_event` on the first attempt:
  - the response is `202 {event_id, event_status: "FAILED",
    transaction_id: null}`;
  - after the request, the event is committed with `status = FAILED`,
    `attempt_count = 1` and `error_code = PROCESSING_ERROR`;
  - no `Customer` or `Transaction` exists.

  Then, with the force removed and the clock moved past the 5-minute
  backoff, `retry_failed_events` processes the event to `PROCESSED`, and
  a replay of the same body returns `200` with the new `transaction_id`.
- [ ] **`202` for a replay of a pending event.** A stored `api` event in
      `RECEIVED` (created directly in the test) is replayed with the
      matching body. The response is `202` with
      `event_status: "RECEIVED"`, and no processing runs inline (only
      `created = True` processes inline).
- [ ] **`409`.**
  - Replaying the body of a `DEAD_LETTER` event, and of a `CANCELLED`
    event, each returns `409 sale_not_processed` with `event_id` and
    `event_status`.
  - A disconnect racing the request, simulated by setting the `api`
    integration to `DISCONNECTED` between pre-validation and
    `process_event`, gives `CANCELLED` and `409`.
  - No `Transaction` is created in either case.
- [ ] **No duplicate normalization.** With `ApiAdapter.normalize` wrapped
      by a spy, one successful `POST /sales` calls it exactly twice (once
      in pre-validation, once in `process_event`), both through
      `get_adapter`. The `Transaction` fields equal the output of the
      `process_event` call on the stored payload. A code-level check
      confirms that the `/sales` serializer and view contain no field
      parsing (they accept any JSON object and pass it through).
- [ ] **Event state machine unchanged.** No migration or code in this
      phase adds an `IntegrationEvent` status, field or transition.
      `IntegrationEvent.Status` values are exactly the Phase 04 set.
- [ ] With the Celery broker enqueue forced to raise:
  - `POST /sales` still returns `201`, because the event was processed
    inline.
  - A Generic Webhook delivery still returns `200`. Its committed
    `RECEIVED` event is picked up by the stale-`RECEIVED` sweep
    (Decision 9).
- [ ] A key without `sales:write` gets `403`. A revoked or unknown key
      gets `401` with `WWW-Authenticate: Bearer`. A session-only request
      (a logged-in OWNER, no Bearer) gets `403` on both `POST` and
      `GET /sales`.
- [ ] `GET /sales` with `transactions:read` returns only the key
      merchant's transactions, merchant-wide, with the `/transactions`
      body, filters and cursor pagination. A `sales:write`-only key gets
      `403`.
- [ ] **Cross-tenant (priority scenario):** merchant A's key never reads
      or writes merchant B's transactions through `/sales`.
- [ ] With a low `api_key` rate, the (N+1)th `/sales` request returns
      `429`.
- [ ] The opt-in registry test asserts exactly
      `{(MerchantView, "GET"), (SalesView, "GET"), (SalesView, "POST")}`.

**Hygiene and docs**
- [ ] No log record from the receivers, adapters, `/sales`, the CSV task
      or storage contains a phone, a secret, a signature, a payload value
      or CSV content (`caplog`, on both the success and the failure
      paths).
- [ ] No task uses `eta`, `countdown` or autoretry.
- [ ] `Shopify.md`, `Generic-Webhook.md` and `CSV-Import.md` exist and
      match the implementation. `Shopify.md` cites the shopify.dev
      sources for the Decision 3 facts. Every doc listed in Files to
      change is updated.
- [ ] The full `pytest` suite passes. Existing tests pass unchanged,
      except the listed legitimate expectation updates. Nothing is
      skipped, `xfail`ed or weakened.

**Explicitly out of scope** (owned elsewhere): refund/void ingestion and
the Refund Race scenario (11), Duplicate Campaign Execution and
Concurrent Workers (10–11), Opt-Out Enforcement (08), invalid-signature
alerting (18), and the Phase 17 adapters and their contract tests.
Also out of scope:
- the Shopify compliance webhooks (proposed owner Phase 15, which
  becomes a prerequisite for App Store submission);
- Shopify token refresh (the first later Admin API consumer);
- level-2 protected customer data approval (operations; a production
  prerequisite, not a development blocker).
