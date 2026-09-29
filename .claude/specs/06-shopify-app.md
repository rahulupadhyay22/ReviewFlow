# Spec: Shopify App

## Overview
This spec covers the ReviewFlow Shopify app integration. It is the only
Phase 06 work still open. Phase 06 Workstream A shipped in PR #10: the
Generic Webhook, CSV Import, the Generic REST API and the shared
receiver infrastructure (the `integration_lookup` policy,
`receive_webhook`, `WebhookIpRateThrottle`, and the additive adapter
hooks).

This spec adds the following:
- the Shopify-initiated OAuth install;
- explicit OWNER/ADMIN account linking;
- shop-identity resolution;
- app-registered shop-specific `orders/paid` and `app/uninstalled`
webhooks, verified with the platform app client secret;
- `orders/paid` ingestion through the unchanged Phase 04 pipeline;
- uninstall handling through the existing `disconnect_integration`;
- the Shopify fixture and contract tests;
- `docs/05-integrations/Shopify.md`.

The plane is **ingestion**.

**Nature of this spec.** It **extracts** the Shopify decisions already
approved in `.claude/specs/06-priority-integrations.md` (the "parent
spec"). It is not a redesign.
- Every normative block below is copied from the parent spec
**verbatim**. The only text altered is marked **[User decision
2026-09-29, O1]** or **[User decision 2026-09-29, O2]** (see below).
Wherever the copied text says "`06-shopify-app` scope", read it as
this branch.
- "Decision N" always means the parent spec's Decision N. This spec
keeps that numbering so that references do not drift.
- If this spec and the parent spec disagree on a Shopify decision, the
parent spec wins, and the disagreement is a defect in this spec. The
exceptions are the R1–R3 reconciliations with the shipped code, and
the O1/O2 user decisions of 2026-09-29, all described below.
- Where the parent spec is silent, or where the code shipped in PR #10
differs from the parent's wording, the gap is recorded below under
"Reconciliations and open items". It is not resolved here.

### Reconciliations and open items (not in the parent spec)
These items come from comparing the parent spec with the code shipped
in PR #10. R1–R3 change no approved decision. O1 and O2 are user
decisions made on 2026-09-29. O1 fills a gap in the parent spec, and O2
supersedes the parent's `422` for credentials posted to Shopify
`connect`.

- **R1: discriminator signature (reconciliation, not a decision
change).**
- The parent spec writes the hook as
  `ShopifyAdapter.uninstall_payload_matches(integration, payload)`.
- The shipped base hook in `integrations/core/adapters.py` is
  `uninstall_payload_matches(self, payload: dict) -> bool`. It
  defaults to fail-closed `False`.
- The adapter already holds the integration as `self.integration`.
- `ShopifyAdapter` overrides the shipped signature and compares
  against `self.integration.config_json["shop_id"]`. The semantics
  are exactly Decision 3, step 2b.
- Wherever the verbatim blocks below say
  `uninstall_payload_matches(integration, payload)`, read it as this
  override.
- **R2: the receiver order must gain the uninstall step.**
- The shipped `receive_webhook` has this order: lookup → provider
  check → `verify()` → `DISCONNECTED` rejection → sale-topic filter
  → JSON decode → event id → `tenant_context` → `record_event`.
- Its docstring says this order is "unchanged by any concrete
  provider".
- The parent spec's normative order (Rules for implementation, and
  `receive_webhook` steps 4–5) puts the Shopify `app/uninstalled`
  path **after `verify()` and before the `DISCONNECTED` rejection**.
- This spec inserts that step there and updates the docstring.
- Every other step and its position is unchanged.
- **R3: `disconnect_integration` needs `actor: TeamMember | None` and
`reason`.**
- The shipped signature is `disconnect_integration(*, actor:
  TeamMember, integration)`. It dereferences `actor.user`.
- The parent spec's Decision 3 uninstall changes it to
  `actor: TeamMember | None` and `reason: str | None = None`.
- `auditlog.services.record()` already accepts `actor=None` (checked
  in `auditlog/services.py`).
- This is a change to the existing service, not a new disconnect
  mechanism.
- **O1: `X-Shopify-Webhook-Id` is required. RESOLVED by the user on
2026-09-29.**
- For **both** Phase 06 Shopify topics, `orders/paid` and
  `app/uninstalled`, the receiver **must** require
  `X-Shopify-Webhook-Id`.
- A missing header returns the identical generic
  `401 invalid_signature`, and nothing is stored or changed.
- For `app/uninstalled`:
  - the check runs **before** the current-topic discriminator, the
    status read, and any disconnect or state mutation;
  - it runs whatever the `Integration` status;
  - a verified `app/uninstalled` for an already-`DISCONNECTED`
    integration returns `200` with no write **only after** every
    authentication and validation requirement has passed: the lookup,
    the provider check, the HMAC, the shop-domain check, the webhook
    id and the discriminator. The uninstall fast path never bypasses
    the webhook-id check.
- For `orders/paid`:
  - a missing header gives `401`, not the `422` (`PayloadRejected`)
    that the shipped `receive_webhook` returns today for a
    `PayloadValidationError` from `get_external_event_id`;
  - the generic webhook's `422` for a missing mapped transaction id is
    unchanged.
- Other verified topics (`200`, not stored) are unchanged by this
  decision.
- Where this applies: Decision 3 uninstall steps 2a and 3, the
  `receive_webhook` steps 4 and 7, the Rules receiver order, the
  endpoint `401` list, and the DoD.
- The implementation plan states the mechanism. Any mechanism must
  produce exactly this order and outcome.
- **O2: the generic credentials-based connect for `shopify`. RESOLVED by
the user on 2026-09-29.**
- **The Shopify OAuth install/link flow (`GET
  /integrations/shopify/install` → callback → `POST
  /integrations/shopify/link`) is the only way to create a Shopify
  `Integration`.**
- `POST /integrations/shopify/connect`, the generic credentials-based
  connection endpoint, behaves as follows for `provider=shopify`,
  **whatever the body** (credentials, `config_json`, a `shop`, tokens,
  or empty):
  - It returns **`400 Bad Request`** in the existing standard error
    shape, `{ "error": { "code", "message", "field_errors"? } }`
    (`API-Specification.md` §General Conventions; `core/api.py`). This
    is the same provider-rejection shape the generic connect already
    uses: `code: "validation_error"`, with the message under
    `field_errors.provider`. The message states that Shopify must be
    connected through the Shopify OAuth installation/linking flow. No
    new error schema is introduced.
  - It never creates an `Integration`, and never writes an audit row.
  - It never accepts or stores Shopify OAuth access or refresh tokens,
    or app credentials, from the request body. The rejection happens
    before any credential is read, validated, issued, encrypted or
    inserted.
- This **supersedes** the parent's Decision 6 wording "Posting
  `credentials` to Shopify `connect` returns `422`". Every copied
  block that carried that `422` is marked
  **[User decision 2026-09-29, O2]**.
- It does **not** change `PATCH /integrations/{id}` on a `shopify`
  integration, which still returns `422` (Decision 6, server-managed
  `config_json`).
- Implementation note: the shipped exception handler renders every
  `ValidationError` as `422`. The implementation plan states how the
  `400` is produced while keeping the existing error shape.

### Approved Shopify decisions (verbatim from the parent spec)

#### Decision 2: the endpoint (verbatim)
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

#### Decision 3: the Shopify app (verbatim, including steps 5a and 2b, the re-evaluation rule, U10, F1–F27, V1–V10 and U1–U10)

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

#### Decision 6: the Shopify adapter hooks (verbatim; the base hooks shipped in PR #10, and the Shopify overrides are this spec's work; see R1)

  - `is_sale_event(self, request) -> bool`: the default is `True`.
    Shopify returns `request.headers["X-Shopify-Topic"] == "orders/paid"`.
  - `is_uninstall_event(self, request) -> bool`: the default is `False`.
    Shopify returns `X-Shopify-Topic == "app/uninstalled"`
    (Decision 3, uninstall).
  - The Shopify `connect` does not use `validate_connection` or
    `issue_credentials` with client-supplied credentials. Its
    credentials come only from the verified OAuth token exchange
    (Decision 3). **[User decision 2026-09-29, O2]** Any `POST
    /integrations/shopify/connect` returns `400`, whatever the body.
    This supersedes the earlier `422`. The OAuth install/link flow is
    the only way to create a Shopify `Integration`.
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

#### Decision 11: pre-tenant receivers (verbatim; the `webhook-generic` half shipped, and this spec adds `webhook-shopify`)

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

#### Decision 15: webhook per-IP throttle (verbatim; shipped, reused)

15. **Webhook per-IP throttle.** Receivers are unauthenticated until
  verified, so they get `WebhookIpRateThrottle` (scope `webhook_ip`,
  env `WEBHOOK_IP_RATE`, default `1200/min`, keyed on DRF `get_ident()`
  under `NUM_PROXIES`). It runs before the lookup and returns `429` with
  `Retry-After`.

#### Decision 17: the Shopify fixture and field contract (verbatim)

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

### Out of scope for this spec (from the parent spec's "Where Phase 06 stops")
| Not in `06-shopify-app` | Owner (per the parent spec) |
|---|---|
| WooCommerce, Petpooja, GoFrugal, Zapier, Make adapters | 17 (V1 scope, later phase; **not** V2) |
| Refund/void ingestion (`Transaction.status` → `REFUNDED`/`VOIDED`), including Shopify `refunds/create` | 11 (Decision 12). Until then `refunds/create` and `orders/cancelled` are, like any non-`orders/paid` topic, acknowledged `200` and not stored |
| Eligibility, `CampaignExecution`, the `Transaction` row lock | 10 |
| Invalid-signature spike alerting | 18 |
| 90-day purge of `IntegrationEvent.payload` | 15 |
| Dead-letter review in Django Admin | 16 |
| Advanced analytics, AI/intelligence | Not in Phase 06 (excluded by the request for this spec; no owner stated in the parent spec) |
| Shopify mandatory compliance webhooks (`customers/data_request`, `customers/redact`, `shop/redact`). They are mandatory before App Store submission and review, including for a limited-visibility listing | Proposed: 15, plus a new lookup-policy sign-off (Decision 3). **Phase 15 (or its replacement owner) is a prerequisite for App Store submission.** Not implemented in this branch (Decision 3, "Compliance webhooks") |
| Shopify token refresh (refresh-before-use or a job) | The first later phase that calls the Shopify Admin API after install (Decision 3, "Token lifetime"). No refresh subsystem here |
| Shopify level-2 protected customer data approval | Operations, outside the code (Decision 3, U6) |
| Any Shopify-credential sign-up path that creates a **new** ReviewFlow merchant | Not designed (U8, OPEN) |
| Any Shopify topic beyond `ORDERS_PAID` and `APP_UNINSTALLED` | Not subscribed. Adding one first triggers the Decision 3 re-evaluation rule |

## Source docs
- `.claude/specs/06-priority-integrations.md`: Overview; "Where Phase 06
stops"; Decisions 1, 2, 3 (entire, including F1–F27, V1–V10,
U1–U10), 6, 11, 12, 15, 17; API endpoints (Shopify webhook and
"Shopify app installation"); Services (`ShopifyAdapter`,
`disconnect_integration`, Shopify install services,
`receive_webhook`); Rules for implementation; Definition of done
(Shopify items, "Shopify app" block)
- `docs/ROADMAP.md`: §06 ("Shopify adapter (HMAC signature)",
"Fixture-based contract test per adapter", "Write the deferred docs
`Shopify.md` …")
- `docs/FINAL-ARCHITECTURE-REVIEW.md`: §1 (integration scope and
ownership), §8 (RLS)
- `docs/02-architecture/Multi-Tenancy.md`: §Layer 2, "Integration lookup
— `integration_lookup` (signed off, Phase 06 spec Decision 1)"; §No
Standing Privileged Role; §"`IntegrationEvent` — merchant identified
before creation"
- `docs/02-architecture/SAD.md`: §3 (the `integrations/shopify/` tree),
§4 (adapter), §5 (queues)
- `docs/05-integrations/Integration-Architecture.md`: the adapter
contract, `IntegrationLocationMapping`, the Shopify row
- `docs/04-api/Webhook-Specification.md`: §Inbound Webhook Endpoints,
§Fail-closed rule, §Idempotency
- `docs/04-api/Authentication.md`: §3 (provider webhooks)
- `docs/04-api/API-Specification.md`: §Integrations
- `docs/09-security/Security-Controls.md`: §Encryption & Secrets,
§Webhook Security, §Logging Rules
- `docs/09-security/Audit-Logging.md`: integration connected /
disconnected
- `docs/10-development/Testing-Strategy.md`: §Duplicate Webhook Event,
§Cross-Merchant Event Isolation, §Duplicate External Event ID Across
Integrations, §Contract Tests for Adapters, §Tenant Isolation
- Shipped code (PR #10), reused as is: `core/tenancy.py`
(`integration_lookup_atomic`, `tenant_context`, `tenant_atomic`,
`get_current_merchant_id`); `integrations/models.py`
(`Integration.objects.for_lookup_id`); `integrations/services.py`
(`receive_webhook`, `disconnect_integration`,
`update_integration_config`, `connect_integration`);
`integrations/core/adapters.py` (`BaseAdapter` hooks);
`integrations/core/schemas.py` (`verify_hmac_sha256`, `parse_amount`,
`parse_occurred_at`, `validate_e164`, `validate_currency`);
`integrations/throttling.py` (`WebhookIpRateThrottle`);
`events/services.py` (`record_event`, robust on-commit enqueue);
`accounts/middleware.py` (`_PRE_TENANT_URL_NAMES`); `core/crypto.py`;
`auditlog/services.py` (`record`)

## Depends on
- Phase 06 Workstream A (`06-priority-integrations`, merged in PR #10):
- the `integration_lookup` RLS policy (migration
  `integrations/0005_integration_lookup_rls.py`),
  `integration_lookup_atomic` and `for_lookup_id`;
- `receive_webhook`, `WebhookIpRateThrottle` and the receiver posture
  (`csrf_exempt`, no auth classes, `AllowAny`);
- the `BaseAdapter` hooks `get_external_event_id`,
  `validate_connection`, `issue_credentials`, `is_sale_event`,
  `is_uninstall_event` and `uninstall_payload_matches`;
- `schemas.verify_hmac_sha256`, `parse_amount` and
  `parse_occurred_at`;
- `record_event` with the robust on-commit enqueue.
- Phase 04 (Done): `Integration`, `IntegrationLocationMapping`,
`IntegrationEvent`, `process_event`, `resolve_location`,
`record_sale`, `retry_failed_events`, `SaleCreated`, `registry.py`,
`/integrations`.
- Phases 00–03 (Done): tenancy/RLS helpers, `core.crypto` (Fernet),
session auth + CSRF + TOTP, roles (`IsOwnerOrAdmin`), `AuditLog`,
`Location`.
- External prerequisites (outside the code; see "Implementation
gates"):
- a ReviewFlow Shopify app in the Shopify Dev Dashboard (public
  distribution, standalone, `read_orders`), with its App URL set to
  `GET /integrations/shopify/install` and its redirect URL set to
  `SHOPIFY_REDIRECT_URI`;
- its client id and secret supplied through the environment;
- the Next.js link-page route deployed at the dashboard/frontend
  origin, with `SHOPIFY_LINK_PAGE_URL` (server-side configuration only;
  see Decision 3, "Link page URL") pointed at it;
- a Shopify development store for U1a, U9, V5 and the V8 delivery
  check.

## Roadmap Phase
- Phase: 06 — Priority integrations
- Completes entire phase: Yes
- If No: n/a.
- The parent spec's Roadmap Phase section names the Shopify app
  integration as the **only** remaining Phase 06 work: "the install
  flow; the OAuth callback; the token exchange; shop-specific webhook
  registration; `orders/paid` ingestion; `app/uninstalled` handling;
  the Shopify fixture and contract test; `Shopify.md`".
- This spec covers exactly that list. It also covers the remaining
  ROADMAP §06 bullets for Shopify: the "Shopify adapter (HMAC
  signature)", its "Fixture-based contract test", and `Shopify.md`.
- Every other §06 bullet shipped in PR #10.
- `/ship-feature` still verifies completion itself, including the
Implementation gates below (U1a and U9 confirmed), before it marks
Phase 06 Done.
- **Not implied by "Yes":** App Store submission readiness.
- Submission additionally needs the compliance webhooks (proposed
  owner Phase 15), level-2 protected customer data approval (U6), and
  an answer to U8.
- Shipping this spec makes the app installable on development stores
  (parent spec, "Submission prerequisite").

## Locked decisions touched
- Shopify integrates as the ReviewFlow Shopify app, with public
distribution (limited visibility). It is a standalone/API-only app
using the authorization code grant. Webhook HMAC is keyed with the
platform `SHOPIFY_CLIENT_SECRET`, and the per-merchant OAuth
credential is Fernet-encrypted in `Integration` (parent Decision 3,
**LOCKED by the user, 2026-09-24**) — DEPENDS ON
- `integration_lookup`: the SELECT-only pre-tenant lookup policy
(`Multi-Tenancy.md` §Layer 2, signed off in Phase 06 Decision 1) —
DEPENDS ON. It is reused unchanged: no new policy, and no shop-domain
lookup.
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
- No standing privileged role, no `BYPASSRLS`, no `SECURITY DEFINER`
(`Multi-Tenancy.md` §No Standing Privileged Role) — NO CHANGE
- Adapter pattern: `verify`/`parse`/`normalize` + registry keyed on
`Integration.provider` (`Integration-Architecture.md`; `SAD.md` §4) —
DEPENDS ON (the Decision 6 hooks, already shipped)
- Three auth mechanisms, never mixed; webhook signatures verified in the
adapter, fail closed `401` (`Authentication.md`) — DEPENDS ON
- Fernet encryption of integration credentials (`Security-Controls.md`)
— DEPENDS ON
- Queues `events`/`whatsapp`/`google_sync`/`default`; no broker
`eta`/`countdown` (`SAD.md` §5; `Architecture.md` §4) — NO CHANGE
- Refund handling (`FINAL-ARCHITECTURE-REVIEW.md` §5) — NO CHANGE
(ingestion deferred to Phase 11, Decision 12)
- Retention, raw payloads 90 days (`FINAL-ARCHITECTURE-REVIEW.md` §9) —
NO CHANGE (Phase 15)
- One review request per transaction, quota, WhatsApp, Google, billing —
NO CHANGE
- Django Admin, no bespoke admin app (`Architecture.md` §6) — NO CHANGE

None. This spec proposes no LOCKED DECISION CHANGE. The parent's
`integration_lookup` change is already signed off (`Multi-Tenancy.md`
§Layer 2), and Decision 3 was locked by the user.

## Django apps
- `integrations`: **touched**. It gets:
- the new plain subpackage `integrations/shopify/` (`__init__.py`,
  `adapter.py`, `services.py`; no `apps.py` and no models);
- the `ShopifyWebhookView` receiver;
- the Shopify install views;
- URL routes;
- the Shopify exceptions;
- registration of `ShopifyAdapter`;
- the `receive_webhook` uninstall step (R2);
- `disconnect_integration(actor=None, reason=...)` (R3).
- `accounts`: **touched**. `SessionMerchantMiddleware`'s
`_PRE_TENANT_URL_NAMES` gains `webhook-shopify` (Decision 11). No other
route is added to it.
- `config`: **touched**. It gets the `SHOPIFY_*` settings read from the
environment.
- `core`, `events`, `transactions`, `customers`, `locations` and
`auditlog` are used only. Their code is unchanged.

## Models & database changes
No database changes.
- `Integration.Provider.SHOPIFY` already exists (`integrations/models.py`).
- The Shopify credential and config live in the existing
`credentials_encrypted` and `config_json` fields (Decision 3,
"Credentials").
- No new `Integration` status is introduced. The provisional row is an
uncommitted savepoint insert (Decision 3, step 5).
- No new RLS policy is added. `integration_lookup` and
`tenant_isolation` are reused unchanged.
- Idempotency:
- `UNIQUE(integration_id, external_event_id)` with
  `external_event_id = X-Shopify-Webhook-Id`;
- `UNIQUE(location_id, external_transaction_id)` backstops duplicate
  deliveries of the same order across subscriptions (Decision 3,
  webhook verification).
- Stored shapes (Decision 3), for reference:
- `credentials_encrypted`: Fernet JSON
  `{ access_token, access_token_expires_at, refresh_token,
  refresh_token_expires_at, scope }`;
- `config_json`: `{ shop_domain, shop_id, webhook_subscription_ids }`,
  where `shop_id` is the numeric id as a digit string. All three keys
  are server-managed.

## API endpoints
Base `/api/v1/`. Error shape `{ error: { code, message, field_errors? } }`.
The blocks below are verbatim from the parent spec.

### Provider webhook (verbatim; signature auth only, CSRF-exempt, no session and no API key, `WebhookIpRateThrottle`)
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
  missing `X-Shopify-Webhook-Id` on `orders/paid` **or**
  `app/uninstalled` (**[User decision 2026-09-29, O1]**: checked
  before any uninstall state read or change, whatever the status), an
  `app/uninstalled` whose body fails
  the current-topic discriminator, or a `DISCONNECTED` integration for any
  topic **other than** a verified, bound `app/uninstalled`.
- `400 invalid_payload` for a verified non-JSON body or a body that is
  not a JSON object. Nothing is stored.
- `429` with `Retry-After` when throttled.

### Shopify app installation (verbatim)

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
subscriptions' payload version, per F20), `SHOPIFY_REDIRECT_URI` and
`SHOPIFY_LINK_PAGE_URL` (the callback's post-OAuth redirect target on
the dashboard/frontend origin; **[User decision 2026-09-29, spec
amendment]**; see Decision 3, "Link page URL").
Outbound calls use stdlib `urllib.request` with explicit timeouts, so
there is no new dependency.

### Existing dashboard endpoints (behavior once `shopify` is registered)
- `PATCH /integrations/{id}`: a `shopify` integration's `config_json`
is server-managed, so its `PATCH` always returns `422` and changes
nothing (Decision 6).
- `POST /integrations/shopify/connect` (**[User decision 2026-09-29,
O2]**):
- It always returns `400` in the existing standard error shape
  (`code: "validation_error"`, message under `field_errors.provider`,
  stating that Shopify must be connected through the Shopify OAuth
  installation/linking flow).
- This holds whatever the body: credentials, tokens, `config_json`, a
  `shop`, or empty.
- It never creates an `Integration` or an audit row, and never
  accepts or stores OAuth tokens or app credentials from the body.
- The `{ shop }` form stays withdrawn.
- Location mappings use the existing `/integrations/{id}/locations`
endpoints, unchanged.

## Services & background tasks
The shared infrastructure is reused and not redefined:
- `core.tenancy.integration_lookup_atomic`, `tenant_context` and
`tenant_atomic`;
- `Integration.objects.for_lookup_id`;
- `schemas.verify_hmac_sha256(secret, body, signature,
encoding="base64")`;
- `events.services.record_event`;
- `process_event`;
- `WebhookIpRateThrottle`.

### `ShopifyAdapter` (`integrations/shopify/adapter.py`, registered in `registry.ADAPTERS`), verbatim
Read `uninstall_payload_matches(integration, payload)` as the shipped
override signature `uninstall_payload_matches(self, payload)` (R1).

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
  - **[User decision 2026-09-29, O1]** The header is required on both
    `orders/paid` and `app/uninstalled`. On the uninstall path it is
    checked before the discriminator and any state read or change.
- `validate_connection`: **[User decision 2026-09-29, O2]** the
  generic `connect` for `shopify` is rejected with `400` before any
  credential is read, whatever the body. The Shopify credential comes
  only from the OAuth token exchange.
- `parse`/`normalize` follow **exactly** the Decision 17 field table,
  with its Required/Optional/Fallback rules. The adapter reads no other
  field, and never an address phone. The field list here is not
  repeated, so the two cannot drift.

### Services (verbatim from the parent spec; `disconnect_integration` change, Shopify install services, and `receive_webhook` with the uninstall step, per R2 and R3)

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
    - **[User decision 2026-09-29, O1]** If `X-Shopify-Webhook-Id` is
      missing, raise `WebhookRejected` (`401`). This runs first, before
      the discriminator, the status read and any disconnect. It applies
      whatever the `Integration` status;
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
    **[User decision 2026-09-29, O1]** For Shopify `orders/paid`, a
    missing `X-Shopify-Webhook-Id` gives the identical `WebhookRejected`
    (`401`), not `PayloadRejected` (`422`). The generic webhook's `422`
    for a missing mapped transaction id is unchanged.
8. `with tenant_context(integration.merchant_id)`:
    `record_event(integration=..., external_event_id=..., payload=...)`.
    `IntegrationDisconnected`, from a disconnect racing this request,
    raises `WebhookRejected`.

### Celery tasks and Beat
- No new task and no new Beat schedule.
- Ingestion uses the existing `events`-queue task, enqueued by
`record_event`, and the Phase 04 `retry-failed-events` job.
- Token refresh is **not** built here (Decision 3, "Token lifetime").

## Admin
No admin changes. This follows the Phase 04/05/06 precedent: tenant
models are not registered until the Phase 16 audited privileged path
exists.

## Files to change
- `integrations/core/registry.py`: register `ShopifyAdapter`, and
update the module docstring.
- `integrations/services.py`:
- `receive_webhook`: insert the Shopify `app/uninstalled` step after
  `verify()` and before the `DISCONNECTED` rejection, and update the
  docstring order (R2);
- the missing-`X-Shopify-Webhook-Id` → `401` on both Shopify topics, checked before any uninstall state read or change (O1);
- `connect_integration`: reject `provider=shopify` with `400` before any credential handling (O2);
- `disconnect_integration(*, actor: TeamMember | None, integration,
  reason: str | None = None)` (R3).
- `integrations/exceptions.py`: `ShopifyInstallInvalid` (`400
shopify_install_invalid`), `ShopifyInstallExpired` (`409
shopify_install_expired`) and `ShopifyUnavailable` (`502
shopify_unavailable`).
- `integrations/receivers.py`: `ShopifyWebhookView`, a thin view calling
`receive_webhook(provider="shopify", ...)`, with the same posture as
`GenericWebhookView`.
- `integrations/views.py`: thin Shopify install/callback/pending/link
views that call `integrations/shopify/services.py`. Views pass
`request.session` in and never interpret its contents.
- `integrations/urls.py`:
- `webhooks/shopify/<str:integration_id>` (name `webhook-shopify`;
  `<str:>` so that a malformed id gets the identical `401`);
- `integrations/shopify/install`, `.../callback`, `.../pending` and
  `.../link`.

These must not be shadowed by the existing
`integrations/<str:provider>/connect` route.
- `accounts/middleware.py`: `_PRE_TENANT_URL_NAMES` gains
`webhook-shopify` (Decision 11).
- `config/settings.py`: `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`,
`SHOPIFY_CLIENT_SECRET_PREVIOUS` (optional), `SHOPIFY_API_VERSION`,
`SHOPIFY_REDIRECT_URI` and `SHOPIFY_LINK_PAGE_URL`, read from the
environment only.
- `.env.example`: the same names, with no values.
- **Existing tests whose expectations legitimately change**, because
`shopify` becomes registered. These are not weakened tests:
- `integrations/tests/test_connect_providers.py::test_phase17_and_shopify_providers_are_unregistered`:
  remove `shopify` from the parametrize list. The Phase 17 providers
  stay.
- `integrations/tests/test_connect_providers.py::test_shopify_patch_always_returns_422_once_registered_is_not_applicable_yet`:
  replace it with the real "`PATCH` on `shopify` returns `422`" test
  (Decision 6).
- Existing `disconnect_integration` callers and tests pass
  `actor=<TeamMember>` unchanged.
- Docs:
- `docs/04-api/Webhook-Specification.md` §Inbound Webhook Endpoints:
  `POST /webhooks/shopify` becomes `POST
  /webhooks/shopify/{integration_id}`, with the response codes and
  the unsupported-topic rule (Decision 2). The parent spec assigned
  this update, and it was not made in PR #10 (line 39 still reads
  `POST /webhooks/shopify`).
- `docs/04-api/Authentication.md` §3: replace "verified against the
  integration's stored webhook secret. (Shopify app workstream, not
  yet implemented.)" with the app client-secret rule and the rotation
  dual-accept.
- `docs/09-security/Security-Controls.md` §Encryption & Secrets: add
  the `SHOPIFY_CLIENT_*` platform secrets.
- `docs/04-api/API-Specification.md` §Integrations:
  - add the Shopify install, callback, pending and link endpoints;
  - add the Shopify `PATCH` `422`;
  - remove `shopify` from the "unregistered in this phase" line (line
    273).
- `docs/05-integrations/Integration-Architecture.md`: point the
  Shopify row to `Shopify.md`.

## Files to create
- `integrations/shopify/__init__.py`, `integrations/shopify/adapter.py`,
`integrations/shopify/services.py`
- `docs/05-integrations/Shopify.md`. It must open with the following,
all **before** the Shopify code is written:
- Decision 3's findings F1–F27 (the complete set, including F24);
- the V1–V10 status and the U1–U10 table (including U1a);
- the install, OAuth `state` and account-linking sequence, with each
  step marked as Shopify-required or a ReviewFlow decision
  (Decision 3, merchant flow);
- the rotation procedure;
- the Decision 17 field table.

It also records:
- the re-evaluation rule;
- U10 as the accepted V1 residual;
- the observed U1a parameters and U9 payload, once verified;
- the fixture provenance.
- `integrations/tests/fixtures/shopify_orders_paid_<api_version>.json`:
Shopify's published `orders/paid` sample for the pinned
`SHOPIFY_API_VERSION`, anonymized (Decision 17).
- `integrations/tests/test_shopify.py`, and additional Shopify test
modules as the test writer decides (for example
`test_shopify_install.py`).

## New dependencies
No new dependencies. Outbound calls use stdlib `urllib.request` with
explicit timeouts. HMAC and the rest use `hmac`, `hashlib`, `base64`,
`json`, `secrets` and `re` (parent spec, API endpoints / New
dependencies).

## Rules for implementation
- Django + DRF monolith. Business logic lives only in `services.py`.
Views, receivers and tasks are thin.
- Every tenant-owned model uses `core.TenantScopedManager`, and RLS stays
enabled and forced. `for_lookup_id` is the only unscoped `Integration`
read, and it works only inside `integration_lookup_atomic` for that id.
No new lookup policy is added.
- Never trust a client-supplied `merchant_id`/`location_id`:
- for webhooks, the merchant comes from the looked-up `Integration`;
- for linking, it comes from the session;
- the location always comes from `IntegrationLocationMapping`.
- Celery tasks take `merchant_id` explicitly. There is no `eta`, no
`countdown` and no autoretry.
- Idempotency comes from database unique constraints. There is no
check-then-insert.
- Role checks use DRF permission classes. `IsOwnerOrAdmin` applies to
`/integrations/shopify/pending` and `/link`.
- External IDs are UUIDs.
- Status enums are `UPPER_SNAKE_CASE`, timestamps end in `_at`, and the
provider id is lowercase `shopify`.
- No V2 features and no bespoke admin app.
- Every new service function has a unit test. `ShopifyAdapter` has a
fixture-based contract test. The receiver has signature,
duplicate-delivery and tenant tests.
- **The only way to create a Shopify `Integration` is the Shopify OAuth
install/link flow (O2, user decision 2026-09-29).**
- `connect_integration` and `POST /integrations/shopify/connect`
  reject `provider=shopify` with `400`, in the existing standard error
  shape (`code: "validation_error"`, a `field_errors.provider` message
  pointing to the OAuth installation/linking flow).
- This happens before any credential or config is read, validated,
  issued, encrypted or inserted.
- No `Integration` or audit row is created, and no Shopify token or
  app credential from a request body is ever accepted or stored.
- This rule has a required regression test (see the Definition of
  done).
- **`X-Shopify-Webhook-Id` is required on both Phase 06 Shopify topics
(O1, user decision 2026-09-29).**
- A missing header returns the identical `401 invalid_signature`.
- On `app/uninstalled` the header check runs before the discriminator,
  the status read and any disconnect, whatever the status. The
  `DISCONNECTED` `200` fast path is reached only after every
  authentication and validation check has passed.
- This rule has a required regression test (see the Definition of
  done).
- The terms are fixed. The body check is the **Phase 06 current-topic
discriminator**. Do not describe it as authenticating
`X-Shopify-Topic`, and do not describe it as a general proof of an
uninstall payload.

### Receiver order (verbatim from the parent spec)
- **Receiver order is fixed, and it is exactly `receive_webhook()`:**
1. lookup (Decision 1);
2. provider check;
3. `verify()` over the raw `request.body`. For Shopify this also covers
    the `X-Shopify-Shop-Domain` header check, whatever the status;
4. **Shopify `app/uninstalled` path** (routed by the unsigned topic
    header):
    - **[User decision 2026-09-29, O1]** `X-Shopify-Webhook-Id` must be
      present, else the identical `401`. This check comes first and
      applies whatever the status, so the `DISCONNECTED` fast path below
      cannot bypass it;
    - the signed-body current-topic discriminator
      (`uninstall_payload_matches`, valid for the Phase 06 topic set only)
      must pass, else `401`;
    - `DISCONNECTED` returns `200` with no change;
    - `CONNECTED` performs one system disconnect and returns `200`;
5. `DISCONNECTED` rejection, `401`, for every other topic;
6. sale-topic filter (a non-sale topic returns `200` and is not
    stored);
7. JSON decode;
8. event id (for Shopify `orders/paid`, a missing
    `X-Shopify-Webhook-Id` is the identical `401`; O1);
9. `tenant_context`;
10. `record_event`.

Nothing is stored before verification, and every rejection before
verification is the identical `401`. A destructive action is never
authorized by an unsigned header alone.

### Shopify verification gate (verbatim)

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

## Definition of done
Everything is verified by `pytest` against real PostgreSQL, with Celery
eager, unless noted. Shopify HTTP calls are replaced by a fake
`_shopify_post`. The items are taken from the parent spec's Shopify DoD.
No test is written for an unresolved question (U1a, U8 and U9 are
checked by hand on a development store). O1 and O2 are resolved, and
their regression tests are required below.

**Webhook receiver (Shopify items from the parent spec's receiver
block).** "A valid signed" delivery means one HMAC-signed with the test
`SHOPIFY_CLIENT_SECRET` (Decision 3), carrying the integration's
`X-Shopify-Shop-Domain`.
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
- a Shopify `orders/paid` request missing `X-Shopify-Webhook-Id` (O1).
- [ ] **O1, uninstall webhook-id regression.** Take a validly signed,
    correctly bound `app/uninstalled` delivery (matching shop domain,
    matching `shop_id`) with **no** `X-Shopify-Webhook-Id`:
- on a `CONNECTED` integration it returns the identical `401`. The
  integration stays `CONNECTED`, with credentials, mappings and
  events unchanged, and no audit row is written;
- on an already-`DISCONNECTED` integration it also returns `401`,
  **not** the `200` fast path, and nothing is written.

The same delivery **with** the header gives the normal `200`
outcomes of the Shopify app block.
- [ ] A verified non-`orders/paid` topic (for example `refunds/create`)
    returns `200` and stores nothing.
- [ ] A verified non-JSON or non-object body returns `400` and stores
    nothing.
- [ ] **Duplicate Webhook Event (priority scenario):** the same Shopify
    delivery posted 5 times gives one event and one `Transaction`, and
    deliveries 2–5 enqueue nothing.
- [ ] **Cross-Merchant Event Isolation (priority scenario):** merchants A
    and B each receive a Shopify delivery with the same dedupe id. That
    creates two events, each with its own merchant.
- [ ] **Duplicate External Event ID Across Integrations:** two Shopify
    integrations under one merchant, with the same dedupe id, create two
    events.
- [ ] A Shopify webhook request that also carries a logged-in dashboard
    session cookie (and a Bearer header) returns the normal `200`/`401`,
    never `500`. It is not tenant-wrapped (Decision 11).
- [ ] The multi-location Shopify fixture (POS `location_id`) resolves to
    the mapping whose `external_location_id` matches. An unmapped
    location ends `FAILED` `LOCATION_UNRESOLVED`.
- [ ] `WebhookIpRateThrottle` applies to the Shopify receiver and to
    `GET /integrations/shopify/install`. With a low override, the
    (N+1)th request returns `429` with `Retry-After` before any lookup
    runs.

**Adapter contract tests (verbatim from the parent spec)**
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
  `credentials` for `shopify` return `400` (**[User decision
  2026-09-29, O2]**; this was `422`).
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

**Connect / PATCH**
- [ ] `PATCH /integrations/{id}` on a `shopify` integration returns
    `422`, and `config_json` is unchanged (also covered in the Shopify
    app block).
- [ ] **O2 regression: the generic connect cannot create a Shopify
    integration.** `POST /integrations/shopify/connect` as an OWNER or
    ADMIN, with each of these bodies:
- empty;
- `credentials: { access_token, refresh_token, … }`;
- `credentials: { webhook_secret }`;
- `config_json: { shop_domain, shop_id }`;
- `{ shop }`.

Every one returns `400` in the existing standard error shape
(`code: "validation_error"`, a `field_errors.provider` message
directing the merchant to the Shopify OAuth installation/linking
flow). After each request:
- no `Integration` row exists for the merchant;
- no `integration.connected` audit row exists;
- nothing from the body appears in the database, the audit metadata
  or `caplog`.
- [ ] The Shopify OAuth install/link flow is the only path that creates
    a `shopify` `Integration`. This is covered by the Shopify app
    block's linking tests.
- [ ] `woocommerce`, `petpooja`, `gofrugal`, `zapier` and `make` still
    return `422` on connect (unregistered, Phase 17).
- [ ] Existing `disconnect_integration` behavior with a `TeamMember`
    actor is unchanged. The existing tests pass as they are.

**Hygiene and docs**
- [ ] No log record from the Shopify receiver, adapter or install
    services contains a phone, the client secret, an OAuth code, an
    access or refresh token, an HMAC, a payload value or a Shopify
    response body (`caplog`, on both the success and the failure
    paths).
- [ ] No task uses `eta`, `countdown` or autoretry.
- [ ] `Shopify.md` exists and matches the implementation. It cites the
    shopify.dev sources for the Decision 3 facts, and it records the
    re-evaluation rule, U10 and the U1a/U9 observations. Every doc
    listed in Files to change is updated.
- [ ] No test, doc or code comment claims that the topic header is
    authenticated (parent spec, uninstall DoD).
- [ ] The full `pytest` suite passes. Existing tests pass unchanged,
    except the listed legitimate expectation updates. Nothing is
    skipped, `xfail`ed or weakened.

**Explicitly out of scope** (see "Out of scope for this spec"):
- the compliance webhooks;
- token refresh;
- refund/void ingestion;
- the Phase 17 adapters;
- campaign/eligibility;
- invalid-signature alerting;
- the payload purge;
- the dead-letter admin UI;
- analytics and AI;
- level-2 approval;
- a Shopify sign-up path that creates a new merchant (U8).

## Implementation gates
**Before implementation starts (the parent spec's "Shopify verification
gate", a hard precondition):**
- [ ] `docs/05-integrations/Shopify.md` holds:
- F1–F27 (the complete set, including F24);
- the V1–V10 status;
- the U1–U10 table (including U1a);
- the merchant flow, with each step marked Shopify-required or a
  ReviewFlow decision;
- the rotation procedure;
- the Decision 17 field table, with its "Shopify guarantee" column
  filled from the pinned version's documentation.

Every row cites an authoritative Shopify source URL and its date or
API version.
- [ ] No Decision 17 stop rule fired, or the user resolved it in
    writing. The stop rules are: E.164 not guaranteed; a Required field
    that can be absent; a deprecated or removed field; a fallback whose
    semantics differ.
- [ ] Shopify's published `orders/paid` sample exists for the pinned
    `SHOPIFY_API_VERSION`. If it does not, stop and ask the user.
- [ ] The implementation plan states the mechanism for O1 and O2 (both
    resolved 2026-09-29):
- O1: the `401` for a missing `X-Shopify-Webhook-Id` on both topics,
  ordered before any uninstall state read or change;
- O2: the `400` in the existing error shape, produced before any
  credential handling.
- [ ] External prerequisites are in place:
- the ReviewFlow Shopify app exists in the Shopify Dev Dashboard
  (public distribution, limited visibility, standalone/API-only,
  `read_orders`);
- its App URL is `GET /integrations/shopify/install`;
- its redirect URL equals `SHOPIFY_REDIRECT_URI`;
- `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`, `SHOPIFY_API_VERSION`,
  `SHOPIFY_REDIRECT_URI` and `SHOPIFY_LINK_PAGE_URL` are available
  through the environment, and never committed;
- `SHOPIFY_LINK_PAGE_URL` points at the dashboard/frontend origin (not
  a Shopify domain, and not derived from any request parameter);
- a Shopify development store is available;
- a public HTTPS host is available for the subscription `uri`.

**Before `06-shopify-app` merges (development-store verification):**
- [ ] **U1a (VERIFY, required before merge):** confirm the exact query
    parameters Shopify sends to the App URL on a Shopify-initiated
    install from the App Store listing or admin. Record the observed
    parameters in `Shopify.md`.
- The existing docs do not establish them (F8).
- ReviewFlow relies only on `shop`.
- This does not block starting implementation.
- The parameters must not be fabricated or assumed.
- [ ] **U9 (VERIFY, required before merge):** a real `app/uninstalled`
    delivery on a development store must meet all of these, with the
    observed fields recorded in `Shopify.md`:
- its body is a Shop resource;
- its integer `id` equals the numeric part of the `shop { id }`
  fetched at install;
- it carries none of the listed Order keys;
- it passes `uninstall_payload_matches`.

Also record whether the `shop` query needs an extra access scope.
- [ ] V5 is confirmed on the first development-store delivery: the
    `X-Shopify-Topic` value is exactly `orders/paid`.
- [ ] V8 is confirmed on that delivery: its `X-Shopify-API-Version`
    equals the pinned `SHOPIFY_API_VERSION`.

**Remain open, and do not block implementation or merge:**
- **U8 stays OPEN.** It is a product decision and an App Store
submission prerequisite.
- ReviewFlow builds the existing-merchant linking flow, following the
  shape of F24's exception.
- Whether ReviewFlow qualifies for that exception, or whether App
  Store review will require a Shopify-credential sign-up path that
  creates a new ReviewFlow merchant, is unresolved.
- This spec claims neither answer.
- **U10 is ACCEPTED as a V1 residual risk.** It covers replay of a
captured, genuine signed body against a later or different
`Integration` of the same app. No timestamp, nonce or other replay
validation is added.
- U3, U5 and U6 are non-blocking, as the parent spec's U-table states.
U6 is a production prerequisite.
- The compliance webhooks (proposed owner Phase 15) and U6 gate **App
Store submission** and production value, not this branch.
