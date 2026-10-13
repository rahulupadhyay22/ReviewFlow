# Spec: WhatsApp Embedded Signup

**Status: decisions APPROVED by the user on 2026-10-09.**

- **Approved:**
  - ES-3 and ES-4, both locked-decision changes;
  - ES-5: local-only disconnect;
  - the ES-2 sub-decision: `{{business_name}}` is required for
    shared-pool templates only;
  - ES-17: the Tech Provider model, subject to Meta business
    verification and App Review before production;
  - ES-18 (approved 2026-10-09): a database-backed, hash-only,
    atomically single-use `EmbeddedSignupState`, replacing the earlier
    session-stored state.
- **Still blocked by Meta evidence:** the implementation items listed
  under "Implementation blockers". E-4 stays an implementation blocker
  (user decision, 2026-10-09).
- **Not yet started:** planning and implementation. They wait for the
  user's go-ahead.
- **Evidence baseline:** `docs/05-integrations/WhatsApp-Meta.md`, the
  E-series dated 2026-10-09. Every Meta fact cited here carries its
  E-number and its status there.

## Overview

This spec delivers the remaining Phase 08 work: the merchant-owned
`OWN_NUMBER` sender, onboarded through Meta Embedded Signup **v4**
(E-1e). It belongs to the automation plane. It covers:

- the start/complete flow;
- the inline code exchange (the code lives 30 s, E-2a);
- the WABA/number ownership check;
- merchant-entered PIN registration;
- `WhatsAppAccount(OWN_NUMBER)` rows with an encrypted per-merchant
  business token (E-3);
- subscribing the app to the merchant's WABA, so deliveries reach the
  existing single `/webhooks/whatsapp` receiver (E-5);
- the own-number STOP lookup;
- template ↔ WABA binding (schema only, until E-7b is verified);
- disconnect and reconnect;
- the shared→own migration, which reuses `set_location_sender` and
  `whatsapp.sender_changed`.

**Business model (ES-17, APPROVED 2026-10-09).** ReviewFlow onboards
merchants as a Meta **Tech Provider**. Each merchant adds its own
payment method in WhatsApp Manager and pays Meta directly (E-9b). No
credit line is shared.

The approval is subject to two production prerequisites, neither done
yet:

- Meta business verification of ReviewFlow's business;
- App Review granting Advanced access to `whatsapp_business_messaging`
  and `whatsapp_business_management` (E-9a).

Until both are confirmed, the feature flag stays off in production
(production gate 3).

## Source docs

- `.claude/specs/08-whatsapp-foundation.md`: scope decision 1, Change 1,
  OD-1 (b), OD-2, Out of scope
- `docs/05-integrations/WhatsApp-Meta.md`: M-1 … M-10, E-1 … E-11
- `docs/06-automation/WhatsApp-Architecture.md`: Provider Adapter, Opt-Out,
  Migration, Onboarding Flow
- `docs/01-product/User-Flows.md` §2; `Business-Rules.md` §3, §9;
  `Feature-Scope.md` V1 list
- `docs/03-database/Data-Dictionary.md`: `WhatsAppAccount`,
  `MessageTemplate`; `Database-Design.md`; `ERD.md`
- `docs/02-architecture/Architecture.md` (the synchronous-call rule);
  `Multi-Tenancy.md` (lookup policies, Change 1); `SAD.md` §5, §8
- `docs/FINAL-ARCHITECTURE-REVIEW.md` §8
- `docs/04-api/API-Specification.md` §WhatsApp, §Integrations (the
  Shopify precedent); `Authentication.md` §3, §4;
  `Webhook-Specification.md`
- `docs/09-security/Security-Controls.md`, `Audit-Logging.md`,
  `Privacy-Data-Retention.md`
- `docs/10-development/Coding-Standards.md`, `Testing-Strategy.md`

## Depends on

**Phase 08 foundation (merged, `4bcaa45`).** These primitives are reused,
not redesigned:

- `WhatsAppAccount`, with its CHECK, `UNIQUE(provider, phone_number_id)`
  and `WhatsAppAccountManager`;
- the six Change 1 policies (`rls_shared_pool`), which already admit
  tenant-owned `OWN_NUMBER` writes;
- `WhatsAppLocationMapping`, `set_location_sender` /
  `clear_location_sender` / `get_location_sender`, and the
  `whatsapp.sender_changed` audit;
- `WhatsAppProvider` / `MetaCloudProvider` / `get_provider` and the
  transient/permanent error classes;
- the single `GET`/`POST /webhooks/whatsapp` receiver: signature first,
  zero DB queries in the view;
- `fan_out_inbound_opt_out` / `process_inbound_opt_out` /
  `opt_out_customer`;
- `core.crypto`;
- `secrets.token_urlsafe` and `hmac.compare_digest`, as in the Shopify
  install flow. The *storage* of the signup state is **not** the
  Shopify session pattern: it is the `EmbeddedSignupState` table below
  (ES-18);
- `IsOwnerOrAdmin`, `tenant_task`, `tenant_atomic`, `auditlog.record`.

**Not used and not changed:** `platform_write_atomic` (allowlist stays
`{"whatsapp_shared_pool"}`), `record_platform`.

**Phases 01–07 (Done).**

## Roadmap Phase

- Phase: 08 — WhatsApp
- Completes entire phase: **Yes**. This is the second and final Phase 08
  spec (ROADMAP §08), and every item the foundation deferred has a home
  here:
  - Embedded Signup;
  - `register_number`;
  - encrypted credentials;
  - the own-number inbound lookup;
  - template binding (the schema here; own-number template operations
    are gated by E-7b, inside this phase);
  - the shared→own migration.
- **No ROADMAP change.** Gating own-number template operations on E-7b
  keeps them in Phase 08; it does not move scope. If E-7b fails on the
  development WABA, the user decides whether to move that item, and that
  would need a ROADMAP edit.

## Locked decisions touched

- Shared schema, `merchant_id`, `TenantScopedManager` — DEPENDS ON
- PostgreSQL RLS, `SET LOCAL`, no RLS bypass
  (`FINAL-ARCHITECTURE-REVIEW.md` §8) — **LOCKED DECISION CHANGE (ES-3),
  APPROVED 2026-10-09**
- Change 1 asymmetric policies and the platform-write path
  (`Multi-Tenancy.md`) — NO CHANGE to the six policies. ES-3 *adds* one
  SELECT-only policy.
- "Django never talks to WhatsApp/Google synchronously in a
  request-response cycle (except short read-only dashboard calls)"
  (`Architecture.md`) — **LOCKED DECISION CHANGE (ES-4), APPROVED
  2026-10-09**. This sentence
  sits just above the numbered "Foundational Decisions (locked)" list.
  It is treated as locked because CLAUDE.md names `Architecture.md` as a
  locked-decision source.
- WhatsApp provider architecture — DEPENDS ON (`register_number`
  signature adapted; doc updated)
- Single Meta webhook receiver, no overrides, fail-closed `401` —
  DEPENDS ON, NO CHANGE
- Celery merchant context; poll-based dispatch; quota;
  `CampaignExecution`/`WhatsAppMessage` split — NO CHANGE

LOCKED DECISION CHANGE — USER SIGN-OFF REQUIRED

**Both changes were signed off by the user on 2026-10-09**, for exactly
the scope below. The approval covers that text only: any widening during
planning or implementation (another policy, another column in the
predicate, a fourth synchronous call, another endpoint) needs fresh
sign-off.

### ES-3: `whatsapp_number_lookup` (APPROVED 2026-10-09; recorded in `Multi-Tenancy.md`)

A seventh PERMISSIVE policy, SELECT-only, on `whatsapp_whatsappaccount`:

```sql
CREATE POLICY whatsapp_number_lookup ON whatsapp_whatsappaccount
  FOR SELECT
  USING (
    sender_type = 'OWN_NUMBER'
    AND provider = 'meta_cloud'
    AND phone_number_id = NULLIF(current_setting('app.current_whatsapp_phone_number_id', true), '')
  );
```

Reverse: `DROP POLICY whatsapp_number_lookup ON whatsapp_whatsappaccount;`.
Emitted by a new `core.rls.rls_select_by_phone_number_id(table)`, which
mirrors `rls_select_by_billing_ref`.

**Transaction-local context:** `core.tenancy.whatsapp_number_lookup_atomic(phone_number_id)`
mirrors `billing_ref_lookup_atomic`:

- it raises `TenantContextError` if a merchant context is active
  (PERMISSIVE policies are ORed);
- it validates the id against `^[0-9]{1,64}$` with `fullmatch`, so the
  literal is injection-safe and the id is PARTIAL-verified as
  digits-only (E-4);
- it opens `transaction.atomic(durable=True)` and runs
  `SET LOCAL app.current_whatsapp_phone_number_id = '<id>'`;
- it resets its contextvar in `finally`.

The application mirror is
`WhatsAppAccount.objects.for_lookup_phone_number_id(id)`. It works only
inside that atomic block for that same id, and it is used only as
`.values_list("merchant_id", flat=True)`.

**Why it preserves the guarantees:**

- **`FORCE ROW LEVEL SECURITY` stays on**, and the role stays
  `NOSUPERUSER NOBYPASSRLS`. No `SECURITY DEFINER`, no
  `platform_write_atomic`.
- **No write.** The policy is `FOR SELECT`. No INSERT, UPDATE or DELETE
  policy references `app.current_whatsapp_phone_number_id`, and the
  durable atomic carries no merchant id. Every write therefore still
  fails the tenant policies, whose `merchant_id = CUR` is NULL here, and
  the platform policies, whose `app.platform_write` is unset.
- **No enumeration.** The predicate is an equality on one value. At
  most one `OWN_NUMBER` row matches, because of `UNIQUE(provider,
  phone_number_id)`. There is no wildcard, range or partial match, and an
  empty or unset setting matches nothing (NULLIF → NULL). No other
  merchant's own number is ever visible through it.
- **The shared row is not part of this policy.** The `SHARED_POOL` row is
  readable inside the lookup, exactly as it is without it, through the
  foundation's `tenant_read` policy (`merchant_id IS NULL`, with or without
  a tenant context). `whatsapp_number_lookup` adds only the one matching own
  number and changes nothing about the shared row's visibility or its
  write protection (the six Change 1 policies).
- **Order.** It is used only by `fan_out_inbound_opt_out`, which runs
  only for pairs taken from a delivery whose `X-Hub-Signature-256`
  verified. The webhook view never calls it.
- **Column exposure.** Row-level security cannot hide individual
  columns, so the matched row's `access_token_enc` ciphertext is
  readable inside that one transaction. The mitigations:
  - the only caller selects `merchant_id` alone;
  - the ciphertext needs `FERNET_KEY` to decrypt;
  - the id is not a secret. It appears in signed Meta payloads, and the
    lookup runs only after signature verification.

### ES-4: Architecture exception (APPROVED 2026-10-09; recorded in `Architecture.md`)

> **Exception (APPROVED 2026-10-09 — spec 08-whatsapp-embedded-signup
> ES-4).** `POST /whatsapp/embedded-signup/complete` may make exactly
> three synchronous Meta Graph API calls inside the request, and no
> other:
>
> 1. exchange the Embedded Signup code for the merchant's business token;
> 2. read the WABA's phone numbers to confirm the selected number belongs
>    to it;
> 3. register the number with the merchant-entered PIN.
>
> Reasons: Meta gives the code a 30-second lifetime, so it cannot wait in
> a queue. The PIN must not be persisted or placed in a task argument.
>
> The rules for these calls:
>
> - each call runs outside any database transaction or row lock, with a
>   strict per-call timeout (the provider's fixed 10-second timeout);
> - none is retried automatically;
> - a failure ends the request with a fixed error that contains no
>   secret, and the merchant restarts Embedded Signup;
> - the calls run only after the server-side signup state has been
>   validated: bound to the session's merchant and user, unexpired and
>   single-use.
>
> All other Meta work stays in Celery, including subscribing the app to
> the WABA, template submission and sending. No other endpoint gains a
> synchronous non-read-only Meta call.

## Django apps

- **Touched:** `whatsapp` (models, services, providers, tasks, views,
  serializers, urls, exceptions, migrations, tests), `core` (`tenancy.py`,
  `rls.py`, for ES-3 only), `config` (settings, throttle), `docs/`.
- **Created:** none.

## Models & database changes

### `WhatsAppAccount`: ES-1

| Field | Type | Null | Notes |
|---|---|---|---|
| access_token_enc | TextField | Yes | Fernet ciphertext (`core.crypto.encrypt`) of the merchant's business integration system user token (E-2b, E-3). NULL for `SHARED_POOL` (its token is env-only) and after a disconnect |

- **New CHECK:** `CHECK (sender_type = 'OWN_NUMBER' OR access_token_enc
  IS NULL)`.
- **Service rule:** a row is never `ACTIVE` with a NULL
  `access_token_enc`. A disconnect clears the column and sets
  `SUSPENDED` in one transaction.
- **No expiry or refresh column.** E-2c is UNVERIFIED. If the
  development-WABA check shows the token expires for the chosen Login for
  Business configuration, this spec is amended (an expiry column plus a
  refresh path) before implementation.
- **RLS:** the six Change 1 policies are unchanged. The ciphertext is
  readable only under `tenant_read` by its owner, and under ES-3's lookup
  policy only by the platform task that runs it (see the ES-3 note on
  columns).

### `MessageTemplate`: ES-2

| Field | Type | Null | Notes |
|---|---|---|---|
| whatsapp_account | FK → `WhatsAppAccount`, `PROTECT` | Yes | NULL = the shared-pool WABA (every existing row, unchanged). Non-NULL = the merchant's own `OWN_NUMBER` account, whose WABA the template is submitted to (E-7a) |

**Uniqueness.** `UNIQUE(merchant, name, language)` is replaced by two
partial unique indexes:

- `UNIQUE(merchant, name, language) WHERE whatsapp_account IS NULL`
  (shared, unchanged behaviour);
- `UNIQUE(merchant, whatsapp_account, name, language) WHERE
  whatsapp_account IS NOT NULL`.

**Why partial indexes, not `NULLS NOT DISTINCT`.**

- Django 5.2.17 (`requirements.txt`) supports
  `UniqueConstraint(nulls_distinct=False)`, but it needs PostgreSQL 15+.
- Local dev is `postgres:16` (`docker-compose.yml`). The production
  Supabase major version is not recorded anywhere in the repo.
- Partial indexes work on every supported version, and they follow the
  foundation's existing partial-unique precedent
  (`whatsapp_account_single_shared_pool`).
- Replacing the constraint drops no data.

**Service invariant** (RLS cannot prove it, as for `WhatsAppLocationMapping`):
`whatsapp_account` is NULL or an `OWN_NUMBER` row of the template's own
merchant. RLS stays `rls_direct` on `merchant_id`. The partial unique on
`provider_template_id` is unchanged.

### `EmbeddedSignupState`: ES-18 (approved 2026-10-09)

Table `whatsapp_embeddedsignupstate`. MERCHANT-owned. It holds one row per
issued signup state. It extends `core.BaseModel` (UUID `id`,
`created_at`, `updated_at`).

| Field | Type | Null | Notes |
|---|---|---|---|
| merchant | FK → `accounts.Merchant`, `PROTECT` | No | the session merchant at `start` |
| user | FK → `accounts.User`, `PROTECT` | No | the session user at `start` |
| state_hash | CharField(64), `UNIQUE` | No | lowercase hex SHA-256 of the state value. **The plaintext state is never stored**, logged or audited |
| expires_at | DateTimeField | No | `created_at + WHATSAPP_SIGNUP_STATE_MAX_AGE_SECONDS` |

- **Manager:** `TenantScopedManager`.
- **RLS:** `rls_direct("whatsapp_embeddedsignupstate")`, the standard
  `tenant_isolation` policy with ENABLE and FORCE. No lookup policy, no
  platform policy, no locked-decision change. No Redis.
- **Single-use by deletion.** Consuming a state deletes its row, so there
  is no `consumed_at` column. Nothing references the table, and it has
  no signals, so Django performs the consume as **one conditional
  `DELETE`**, and the affected-row count decides the outcome.
- **Cleanup:**
  - a consumed row is gone at consumption;
  - expired rows are deleted by the same merchant's next `start`, inside
    that request's transaction;
  - an idle merchant's leftovers are inert: they are expired, hashed and
    tenant-isolated. No Beat task is added.
- **No index beyond `UNIQUE(state_hash)`.** Every lookup is by hash, and
  the purge is per merchant on a table of at most a handful of rows per
  merchant.

### Migrations (one logical change each; non-destructive)

1. `0005_whatsappaccount_access_token_enc`: column + CHECK.
2. `0006_messagetemplate_whatsapp_account`: FK column.
3. `0007_messagetemplate_unique_per_waba`: drop
   `whatsapp_template_merchant_name_lang_uniq`; add the two partial
   uniques. Reverse: restore it.
4. `0008_whatsappaccount_number_lookup`: the ES-3 policy (approved
   2026-10-09, exactly as written above).
5. `0009_embeddedsignupstate`: `CreateModel` **and** its
   `rls_direct(...)` policy in **one** migration. The reverse drops the
   policy, then the table.
   - **Why one migration, not two:** a new table plus its standard RLS
     is one logical change.
   - In a single migration the table never exists without RLS at any
     migration state, and PostgreSQL DDL is transactional, so the pair
     applies or reverses atomically.
   - The foundation split its models (`0001`) from its RLS
     (`0002`–`0004`) only because `0001` created three models at once.
   - Depends on `0008` (linear chain) and on the `accounts` migration
     that creates `User`/`Merchant`. That migration already exists, so
     no new cross-app dependency is introduced.

## API endpoints

All endpoints:

- session + CSRF; merchant from the session only;
- the `{ error: { code, message, field_errors? } }` shape; UUIDs only;
- never return `phone_number_id`, `business_account_id`, codes, tokens or
  PINs.

**Roles (ES-7):** OWNER and ADMIN via `IsOwnerOrAdmin` for every
Embedded Signup management operation. Every `GET` is open to any role
(`IsMerchantMember`).

### `POST /whatsapp/embedded-signup/start`

- **Roles:** OWNER, ADMIN.
- **Behaviour** (inside the request's normal tenant transaction):
  1. deletes the merchant's expired `EmbeddedSignupState` rows;
  2. generates `state = secrets.token_urlsafe(32)`;
  3. inserts one row with `state_hash = sha256(state)`, the session
     merchant and user, and `expires_at = now +
     WHATSAPP_SIGNUP_STATE_MAX_AGE_SECONDS` (default 600).

  The plaintext `state` is returned once in the response, and never
  stored. Nothing is kept in the Django session (ES-18).
- **Response:** `200 { state, app_id, config_id }`. The v4 `extras` the
  frontend passes are PARTIAL (E-1a) and are fixed only after the
  development-WABA check.
- **Errors:**
  - `503 whatsapp_not_configured`: flag off (ES-10), or any of
    `META_APP_ID`, `META_APP_SECRET`, `META_EMBEDDED_SIGNUP_CONFIG_ID`,
    `FERNET_KEY` unset;
  - `429`: throttle `whatsapp_signup`.

### `POST /whatsapp/embedded-signup/complete`

- **Roles:** OWNER, ADMIN.
- **Request:** `{ state, code, business_account_id, phone_number_id, pin
  }`.
  - `code` is `authResponse.code`.
  - `business_account_id` and `phone_number_id` are the `waba_id` /
    `phone_number_id` of the `WA_EMBEDDED_SIGNUP` message event (E-1b).
  - `pin` is the merchant's 6-digit two-step PIN (ES-11).
  - Any other field, including `merchant_id`, `business_id`,
    `location_id` and `sender_type`, is ignored.
- **The frontend must POST within the code's 30 s lifetime (E-2a).**

**Order** (no Meta call inside a DB transaction or under a row lock):

1. **Validate input. No Meta call.**
   - `phone_number_id` missing or empty → `422
     embedded_signup_no_phone_number` (ES-15). This covers a
     `FINISH_ONLY_WABA` completion; the server relies only on the field
     being absent, not on Meta's event name.
   - Ids that are not 1–64 digits → `400 embedded_signup_invalid`.
     Digits-only is PARTIAL (E-4).
   - `pin` not exactly 6 digits → `422 validation_error`
     (`field_errors.pin`).
2. **Consume the state atomically (ES-18), in its own short
   `tenant_atomic()` that commits before any Meta call.**
   - One conditional delete:
     `EmbeddedSignupState.objects.filter(state_hash=sha256(state),
     user=<session user>, expires_at__gt=now).delete()`, under the
     session merchant's RLS and manager.
   - Exactly one row deleted → proceed.
   - Zero rows (missing, expired, already used, another user's, or
     another merchant's) → `400 embedded_signup_invalid`.
   - Under PostgreSQL READ COMMITTED, a concurrent second delete of the
     same row waits for the first transaction, then matches nothing. So
     **at most one request can ever consume a state.**
   - The hash lookup is an exact equality on a value derived
     server-side. No plaintext state is compared against stored data.
   - **After consumption the state is gone for good.** If any later step
     fails (a Meta call, persistence or registration), the state is
     never restored or reused, and the merchant must start a new
     Embedded Signup.
3. **Exchange the code inline** (ES-4; E-2b PARTIAL). The token is held
   in memory only.
4. **Ownership check, inline** (ES-4; E-4 PARTIAL). With that token, call
   `GET /{business_account_id}/phone_numbers`; `phone_number_id` must be
   in the result. Not confirmed, or a permanent refusal → `422
   embedded_signup_unverified`.
5. **Persist** in `tenant_atomic`: upsert the merchant's `OWN_NUMBER` row
   as `PENDING` with `access_token_enc`, and audit
   `whatsapp.own_number_connected`. Commit.
   - Any `IntegrityError` on `UNIQUE(provider, phone_number_id)` that is
     not the merchant's own `OWN_NUMBER` row gives `409
     whatsapp_number_unavailable`. That covers another merchant's
     number and the shared pool's number. The shared row is never
     updated or treated as the merchant's own.
6. **Register inline** with the in-memory PIN (ES-4, ES-11; E-6a
   VERIFIED; E-6e idempotency UNVERIFIED). Never retried automatically.
7. **On success:** `on_commit`, enqueue `activate_own_number(merchant_id,
   account_id)`, which subscribes the app and sets `ACTIVE`. Respond.

**Response:** `202 { sender }` (status `PENDING`).

**Errors:**

| Status | Code | When | State left behind |
|---|---|---|---|
| `400` | `embedded_signup_invalid` | bad or used state, malformed id | nothing |
| `422` | `validation_error` | bad `pin` | nothing |
| `422` | `embedded_signup_no_phone_number` | ES-15 | nothing |
| `422` | `embedded_signup_unverified` | the code exchange is permanently refused (e.g. an expired or reused code, E-2a/E-2c), or the ownership check fails | nothing |
| `409` | `whatsapp_number_unavailable` | the number is held by another merchant or is the shared number; generic, names no merchant | nothing new |
| `422` | `whatsapp_registration_failed` | registration permanently refused: wrong PIN, Meta's register limit (E-6c, `133016`), or any other 4xx | the row stays `PENDING` with its token |
| `502` | `whatsapp_provider_unavailable` | transient Meta failure at exchange or ownership check | nothing |
| `502` | `whatsapp_provider_unavailable` | transient Meta failure at registration | the row stays `PENDING` |
| `503` | `whatsapp_not_configured` | flag off or config unset | nothing |
| `429` | throttle | `whatsapp_signup` | nothing |

**Recovery is always a new Embedded Signup run** ("reconnect"). The code
may have expired, and whether it is single-use is UNVERIFIED (E-2c). A
failed `complete` is **never** retried with the same code. Reconnect
must happen within Meta's 14-day registration window (E-6d).
Reconnecting the same number updates the same row: the token is
replaced and `PENDING` is kept.

**Timeouts.** Each inline call uses the provider's existing 10 s
timeout, so the worst case is about 30 s of Meta time per request. The
deployed web worker timeout must exceed this (production-readiness
gate 6). This spec changes no deployment setting.

### `DELETE /whatsapp/senders/{id}` (disconnect, ES-5)

- **Roles:** OWNER, ADMIN.
- **Applies to:** the caller's own `OWN_NUMBER` only. Shared, foreign or
  unknown id → `404`.
- **Mapped:** any location still mapped → `409 sender_in_use`. Nothing
  changes; locations are never unmapped implicitly.
- **Otherwise,** in one `tenant_atomic`: the row becomes `SUSPENDED`,
  `access_token_enc` is set to NULL, and
  `whatsapp.own_number_disconnected` is audited.
- **Response:** `204`. Idempotent: an already-`SUSPENDED` row with no
  token gives `204` and no audit row.
- **Disconnect is local only. No Meta call is made.**
  - **Never deregisters the number.** Deregistering makes it "unusable
    with Cloud API" and uses up Meta's 10-per-72-hours limit (E-8a).
  - **Does not unsubscribe the app from the WABA**, for three reasons:
    - `DELETE subscribed_apps` is only PARTIAL (E-5a);
    - calling it would either widen the ES-4 exception or need the
      token in a task, which is forbidden;
    - while still subscribed, a customer's STOP to the disconnected
      number still reaches ReviewFlow and is honoured (see Inbound).
  - Deliveries for a `SUSPENDED` account are otherwise acknowledged and
    discarded, as today. A reconnect re-runs `subscribe_app`.
- **Residual risk (accepted by the user, ES-5, 2026-10-09):**
  - **Clearing `access_token_enc` does not revoke Meta's grant.** Meta's
    side of the business token, and the merchant's grant to ReviewFlow's
    app, stay as they were. Revocation is UNVERIFIED (E-8c). Only the
    merchant can remove the app, in Meta.
  - **The app stays subscribed to the WABA.** Meta may keep delivering
    events for that number to `/webhooks/whatsapp`: `messages` (customer
    texts and any statuses), `account_update`, and any other subscribed
    field.
  - **How those deliveries are handled:**
    - each one is signature-verified before any processing;
    - nothing from it is persisted or logged;
    - only an opt-out keyword triggers work: the ES-3 lookup resolves
      the merchant, and its customer is opted out;
    - everything else is acknowledged and discarded.
  - **Data minimisation.** ReviewFlow keeps receiving, without storing,
    message text from a disconnected merchant's customers. This was
    accepted, because it lets ReviewFlow honour every STOP sent while
    the merchant was disconnected.

### Existing endpoints

- `GET /whatsapp/senders` (ES-9): own numbers are listed in **every**
  status (`ACTIVE`, `PENDING`, `SUSPENDED`). The shared account is
  listed only when it is `ACTIVE`. Selectability is decided by
  `PUT /locations/{id}/whatsapp-sender`, which answers `422` for a
  non-`ACTIVE` account. API-Specification.md is corrected to say this.
- `PUT` / `DELETE /locations/{id}/whatsapp-sender`: unchanged. This is
  the mapping step and the shared→own migration (ES-13).
- `GET` / `POST /webhooks/whatsapp`: contract unchanged. Own-number
  deliveries arrive at the app callback with no override (E-5b),
  signed with `META_APP_SECRET` (E-5c PARTIAL).
- `/whatsapp/templates`: an optional `whatsapp_account_id` is added to
  `POST`, and to the response body, **only once E-7b is verified**.
  Until then, a request naming an own account gets `422`
  (`field_errors.whatsapp_account_id`: "not yet available").

## Services & background tasks

### Provider (`whatsapp/providers/`): no DB access, only module that talks to Meta

**Token selection.** `_request` (`meta_cloud.py:70`) gains a `token`
argument. `_token_for(account)` returns:

- `SHARED_POOL`: the env token, unchanged;
- `OWN_NUMBER`: `core.crypto.decrypt(account.access_token_enc)`;
- NULL or unset: `WhatsAppNotConfigured`.

Onboarding calls take the in-memory token directly.

**Dataclasses:**

- `ProviderCredential(access_token, expires_at=None)`, with
  `repr=False` on the token;
- `ProviderNumberRef(phone_number_id, business_account_id)`.

**Methods:**

| Method | Gate | Notes |
|---|---|---|
| `exchange_code(code) -> ProviderCredential` | E-2b PARTIAL | The response fixture is captured on a development app (E-10) |
| `verify_number_access(credential, business_account_id, phone_number_id) -> ProviderNumberRef` | E-4 PARTIAL | `ProviderPermanentError` if the number is not listed or access is refused |
| `register_number(self, credential, phone_number_id, pin) -> None` | E-6a | **Replaces `register_number(self, merchant, number)`** (`base.py:76`, `meta_cloud.py:261`): no `Merchant`, no DB. The PIN is a plain argument used once |
| `subscribe_app(self, account) -> None` | E-5a | |

No `deregister_number` and no `unsubscribe_app` (ES-5: disconnect is
local only).

**Error mapping:** as today. 429/5xx/network → transient; other 4xx →
permanent. Exception text never contains a code, token, PIN, phone or
body.

### `whatsapp/services.py`

New functions:

- `begin_embedded_signup(*, actor) -> dict`. ES-18 storage; returns
  `{state, app_id, config_id}`.
- `complete_embedded_signup(*, actor, state, code,
  business_account_id, phone_number_id, pin) -> WhatsAppAccount`. The
  merchant comes from the tenant context and the user from `actor`;
  neither takes a `session` argument.
  - Follows the order above.
  - Decorated with Django's `sensitive_variables("code", "pin",
    "credential")`.
  - The view uses `sensitive_post_parameters("code", "pin")`, so
    Django's error reports never show them.
- `activate_own_number(account_id)`:
  - no-op unless the row is `PENDING` with a token;
  - `subscribe_app` outside any transaction;
  - then a conditional `UPDATE … WHERE status='PENDING'` → `ACTIVE`,
    setting `connected_at` if NULL;
  - transient errors propagate (the task retries);
  - permanent → conditional `PENDING → SUSPENDED`, plus audit
    `whatsapp.own_number_activation_failed`.
- `disconnect_own_number(*, account, actor)`: ES-5. Raises `SenderInUse`
  when the account has any mapping. Otherwise it suspends, clears the
  token and audits. No Meta call.

**Changed existing functions:**

- `list_senders` (`services.py:109`): own rows in any status, plus the
  shared row only when `ACTIVE`. This already matches the code; only the
  docs change.
- Template functions (`services.py:217`, `259`, `283`, `327`):
  - resolve the target account from `template.whatsapp_account` (NULL →
    shared, unchanged);
  - `create_template`'s `503` precondition applies to shared-WABA
    templates only;
  - `validate_template` gains the target account. It requires
    `{{business_name}}` only when `whatsapp_account` is NULL (shared
    pool, ES-2 sub-decision approved 2026-10-09). All other rules apply
    to both: the placeholder allowlist, the language allowlist and the
    length limit. Shared-pool behaviour is unchanged.
  - **Own-number branches are BLOCKED by E-7b.**
- `fan_out_inbound_opt_out` (`tasks.py:99`): ES-3.

### Inbound STOP for own numbers (ES-3)

The view is unchanged: signature first, zero queries,
`fan_out_inbound_opt_out(phone_number_id, phone)` enqueued.

The task already runs only for pairs taken from a delivery whose
signature verified. It now does:

- **shared number:** OD-1 (b), unchanged;
- **otherwise:** `whatsapp_number_lookup_atomic(phone_number_id)` →
  `WhatsAppAccount.objects.for_lookup_phone_number_id(phone_number_id)
  .values_list("merchant_id", flat=True)`. That read returns no other
  column. Then:
  - exactly one merchant → opt the customer out **inline** under that
    merchant's own tenant context, in the fan-out task (foundation
    Amendment A-1, 2026-10-12; there is no second broker hop). A failure
    is retried, bounded, never dropped;
  - none → no-op.

There is **no status filter**: a STOP to a `PENDING` or `SUSPENDED` own
number still opts out its merchant (M-10, Business-Rules §3).

### Celery tasks

| Task | Queue | Args | Notes |
|---|---|---|---|
| `activate_own_number(merchant_id, account_id)` | `default` | ids only | `@tenant_task`; `autoretry_for=(ProviderTransientError,)`, backoff, `max_retries=5`. After exhaustion the row stays `PENDING` (ES-12). Subscribing is the only Meta call; registration never runs in a task |
| `fan_out_inbound_opt_out` (existing) | `whatsapp` | unchanged | own-number branch per ES-3; inline opt-out, bounded retry of failures, `acks_late` (foundation Amendment A-1) |

No Beat entry. No `eta`/`countdown`.

## Admin

None (Phase 16).

## Files to change

- `whatsapp/models.py`, `providers/base.py`, `providers/meta_cloud.py`,
  `services.py`, `tasks.py`, `views.py`, `serializers.py`, `urls.py`,
  `exceptions.py`:
  - new exceptions `EmbeddedSignupInvalid` (400),
    `EmbeddedSignupNoPhoneNumber` (422), `EmbeddedSignupUnverified`
    (422), `WhatsAppNumberUnavailable` (409),
    `WhatsAppRegistrationFailed` (422), `WhatsAppProviderUnavailable`
    (502), `SenderInUse` (409).
- `core/tenancy.py` (`whatsapp_number_lookup_atomic`) and `core/rls.py`
  (`rls_select_by_phone_number_id`): ES-3 (approved 2026-10-09).
- `config/settings.py`:
  - `META_APP_ID`, `META_EMBEDDED_SIGNUP_CONFIG_ID`,
    `WHATSAPP_EMBEDDED_SIGNUP_ENABLED=False`,
    `WHATSAPP_SIGNUP_STATE_MAX_AGE_SECONDS=600`;
  - throttle `whatsapp_signup` (`WHATSAPP_SIGNUP_RATE`, default
    `10/min`).
- `config/tests/test_scaffold.py`, `.env.example`.
- **Docs, already amended in this change set (decisions approved
  2026-10-09):**
  - `Data-Dictionary.md`, `Database-Design.md`, `ERD.md` (incl. ES-18 `EmbeddedSignupState`);
  - `Architecture.md` (ES-4, approved);
  - `Multi-Tenancy.md` (ES-3, approved);
  - `Business-Rules.md` §9 (the own-number `{{business_name}}`
    clarification);
  - `API-Specification.md`;
  - `Audit-Logging.md`;
  - `Security-Controls.md`;
  - `WhatsApp-Architecture.md`;
  - `WhatsApp-Meta.md`.

## Files to create

- Migrations `0005`–`0009` (above).
- Tests: `whatsapp/tests/test_embedded_signup.py`,
  `test_own_number_activation.py`, `test_own_number_disconnect.py`,
  `test_own_number_inbound.py`, `test_own_number_rls.py`,
  `test_template_waba_binding.py`, `test_embedded_signup_state.py`;
  `core/tests/test_number_lookup.py`.
- Fixtures, from Meta samples per E-10:
  - `es_message_event_finish.json` and `es_message_event_cancel.json`
    (E-1b; client-side shapes, used only in docs/tests of the frontend
    contract);
  - `success_true.json`;
  - `subscribed_apps_list.json`;
  - `phone_numbers_list.json` (M-6);
  - `oauth_token_exchange.json`: **captured on a development app, or
    flagged as written from field names** (E-10).

## New dependencies

None.

## Implementation blockers (must clear before the code they name is written)

| Blocker | Blocks | Evidence needed |
|---|---|---|
| E-7b UNVERIFIED | own-number template submit and sync (the schema may land) | Create and poll a template on a customer WABA with its business token |
| E-2c UNVERIFIED | final token schema (expiry column or not) | Expiry of a real business token for the chosen configuration |
| E-4 PARTIAL (kept as an implementation blocker by user decision, 2026-10-09) | `verify_number_access`, and therefore the whole `complete` endpoint, since the ownership check is a security control | Customer X's token is refused on customer Y's WABA; X's `phone_numbers` lists the selected id |
| E-10 PARTIAL | `exchange_code` contract fixture | A real (redacted) `oauth/access_token` response |
| E-1a PARTIAL | the frontend `extras` for v4 | A real v4 launch plus FINISH/CANCEL events |

Items that ship but stay UNVERIFIED until production gates 1–2 (and 4 for E-11):

- E-5c: signature on deliveries from customer WABAs;
- E-6b: whether fresh numbers already have a PIN;
- E-6e: registering twice;
- E-6f: subscribe/register order;
- E-8c: token revocation;
- E-11: ready-to-send criterion.

## Production-readiness gates (the flag stays off until all pass, ES-10)

Turning the flag on does not by itself mean the feature is production
ready.

1. E-1a, E-2b/c, E-4, E-5c, E-6b/e/f, E-8c and E-10 are VERIFIED in
   `WhatsApp-Meta.md`. E-8c covers token behaviour after a merchant removes the app; the ES-16 limitation stays accepted either way. E-7b too, before own-number templates are
   enabled.
2. A dated end-to-end run on a development WABA: connect → `ACTIVE` →
   map a location → a signed STOP to the own number opts out only that
   merchant → disconnect (`409` while mapped, then `204`).
3. E-9: business verification and App Review are complete, and the
   Tech Provider billing model is confirmed (ES-17).
4. E-11: the development run shows whether sends need `name_status =
   APPROVED` and/or a payment method. Until then, `ACTIVE` means
   "registered and subscribed" only, not proven able to send.
5. Secrets set: `FERNET_KEY`, `META_APP_ID`, `META_APP_SECRET`,
   `META_EMBEDDED_SIGNUP_CONFIG_ID`.
6. The web worker request timeout exceeds the ES-4 worst case (about
   30 s of Meta time). No request-body logging and no error-reporting
   tool that captures frame locals without scrubbing `code`, `pin` and
   tokens.
7. The Login for Business configuration is set to v4 (E-1e), with the
   App Dashboard settings of E-1d.

## Rules for implementation

- Business logic only in `services.py`. Role checks only via DRF
  permission classes.
- `TenantScopedManager` everywhere. RLS forced.
  - The only new policy is ES-3 (approved 2026-10-09), exactly as written.
  - Never `SECURITY DEFINER`, `BYPASSRLS` or `platform_write_atomic` for
    own numbers.
- The merchant comes from the session (and must equal the state's
  merchant) or, for inbound, from the ES-3 lookup. **Never** from
  request data or the payload.
- Signup state (ES-18):
  - only its SHA-256 hash is stored;
  - it is bound to merchant and user, and expires;
  - it is consumed by one conditional delete in a short committed
    transaction before any Meta call;
  - it is never restored after a later failure;
  - no Redis and no Django session storage are used for it.
- WABA and phone ids are persisted only after the ownership check.
- **Secrets** (code, business token, PIN):
  - never logged, never in an exception message, never in audit
    metadata, never in an API response, never in a Celery task argument;
  - the token is persisted only as `access_token_enc`;
  - the code and PIN are never persisted at all;
  - `sensitive_variables` / `sensitive_post_parameters` on the code
    paths that hold them.
- Only the three calls named in the ES-4 exception may run inside a
  request. All are outside DB transactions and row locks, each with the
  fixed 10-second timeout. None is retried automatically.
- Registration is never retried automatically. Celery retries only
  `ProviderTransientError`, and only for `subscribe_app`.
- Idempotency: `UNIQUE(provider, phone_number_id)`, single-use state,
  conditional status `UPDATE`s, the partial template uniques. Never
  check-then-insert.
- No `WhatsAppMessage`, status persistence, `send_template`,
  `CampaignExecution` code or send-rate cap (Phases 10/11).
- Do not implement anything behind an open blocker.
- Every service function has a unit test. Every provider method has a
  fixture contract test. Mock only the `urllib` boundary.

## Definition of done

`pytest` against real PostgreSQL, with Celery eager where needed.

### Schema & RLS

- [ ] Migrations `0005`–`0009` apply in order and reverse
      (`migrate whatsapp 0004`). `0008` is ES-3 (approved). `0009` creates
      `EmbeddedSignupState` and its `rls_direct` policy together; its
      reverse drops both, losing only in-flight signup states.
- [ ] `whatsapp_embeddedsignupstate` has RLS enabled **and forced**, with
      exactly the `tenant_isolation` policy. Merchant B's raw SQL sees,
      updates and deletes none of A's rows.
- [ ] The CHECK rejects a non-NULL `access_token_enc` on `SHARED_POOL`.
- [ ] Partial uniques:
  - a duplicate `(merchant, name, language)` with NULL account → error;
  - the same `(merchant, name, language)` on the shared WABA and on an
    own account → allowed;
  - a duplicate on the same own account → error.
- [ ] Merchant B's raw SQL cannot read, update or delete A's own row or
      its ciphertext.
- [ ] `pg_policies`: the six Change 1 policies, plus
      `whatsapp_number_lookup` (SELECT) after `0008`. Nothing else.
- [ ] Lookup atomic:
  - refused inside a tenant context;
  - `durable=True`;
  - rejects a non-digit or over-long id;
  - `SET LOCAL` does not leak on a pooled connection;
  - with the setting, `INSERT`/`UPDATE`/`DELETE` of any row fails or
    affects 0 rows;
  - inside the lookup, exactly one `OWN_NUMBER` row is visible (the
    matching one); other merchants' own numbers stay invisible, and the
    `SHARED_POOL` row is readable only through the existing `tenant_read`
    policy, as it is without the lookup;
  - without the setting, no `OWN_NUMBER` row is visible outside a tenant
    context (the shared row stays readable through `tenant_read`, as
    before).
- [ ] `platform_write_atomic`'s allowlist is unchanged, and no new caller
      exists.

### Embedded Signup

- [ ] `start`: one `EmbeddedSignupState` row bound to the session
      merchant and user, with `expires_at` = now + max age. The
      merchant's expired rows are purged; other merchants' rows are
      untouched. Flag off or config unset → `503`, and no row is stored.
- [ ] **No plaintext state at rest.** The returned `state` string appears
      in no column of `whatsapp_embeddedsignupstate`; only
      `sha256(state)` does. It appears in no log record (caplog), audit
      row or session.
- [ ] **Atomic single use (concurrency).** Two threads,
      `django_db(transaction=True)`, `complete` with the same `state`:
      exactly one passes step 2, the other gets `400
      embedded_signup_invalid`, and Meta's exchange is called once.
- [ ] **Expiry, user and merchant binding.** An expired state, a state
      issued to another user of the same merchant, and a state issued
      under another merchant → each `400`, with no Meta call.
- [ ] **No reuse after a failure.** A `complete` that fails after step 2
      (exchange, ownership, persistence or registration) leaves the
      state consumed. Retrying with the same `state` → `400`.
- [ ] `complete` happy path: exchange → ownership → persist `PENDING`
      (ciphertext ≠ token, and decrypts to it) → register with the PIN →
      `activate_own_number` enqueued with ids only → `202`. One
      `whatsapp.own_number_connected` audit row, with `{reconnect}` only.
- [ ] Each row of the error table produces its documented status, code
      and leftover state:
  - missing `phone_number_id` → `422 embedded_signup_no_phone_number`,
    with no Meta call;
  - bad PIN → `422`, with no Meta call;
  - replayed, expired or foreign state → `400`, with no Meta call after
    validation.
- [ ] The number is held by another merchant → `409`, and the body names
      no merchant. The shared pool's number → `409`, and the shared row is
      unchanged.
- [ ] Registration permanent error → `422 whatsapp_registration_failed`,
      the row stays `PENDING`, and the register call happens exactly once.
- [ ] Reconnect: the same row id, a new ciphertext, `reconnect: true`.
- [ ] Two concurrent completes for one number by the same merchant, with
      two different valid states → one `WhatsAppAccount` row.
- [ ] Roles: VIEWER and MANAGER → `403` on `start`, `complete` and
      `DELETE`; anonymous → `403`; no CSRF → `403`.
- [ ] Secrets: no response body, log record (caplog), audit row, task
      argument or exception message contains the code, token, PIN,
      `phone_number_id` or `business_account_id`. Django's error-report
      filter masks `code` and `pin`.

### Activation

- [ ] `subscribe_app` called once → `ACTIVE`, `connected_at` set. A
      repeat is a no-op.
- [ ] Transient error → retried; after exhaustion the row is still
      `PENDING`. Permanent error → `SUSPENDED` plus a
      `whatsapp.own_number_activation_failed` audit row.
- [ ] `activate_own_number(merchant_id=A, account_id=<B's>)` changes
      nothing.
- [ ] The own account's calls send its decrypted token. Shared-pool calls
      still send the env token. The foundation provider tests are
      unchanged.

### Mapping & migration (reuse)

- [ ] `PUT` to an `ACTIVE` own number → `200` and `sender_changed
      {SHARED_POOL → OWN_NUMBER}`. `PUT` to a `PENDING` or `SUSPENDED`
      own number → `422`.
- [ ] `GET /whatsapp/senders` lists own rows in all three statuses, and
      never a non-`ACTIVE` shared row.

### Disconnect

- [ ] Mapped → `409 sender_in_use`, nothing changed, no audit row.
- [ ] Unmapped → `204`: `SUSPENDED`, token NULL, one audit row, and
      **no Meta call at all** (the HTTP mock records none). A repeat →
      `204` with no audit row.
- [ ] A signed STOP to the disconnected number still opts out the
      merchant's customer.
- [ ] Shared or foreign id → `404`.

### Inbound

- [ ] A signed STOP to A's own number → only A's customer is opted out.
      This holds for A's number in `ACTIVE`, `PENDING` and `SUSPENDED`.
- [ ] The view runs zero queries. Unsigned → `401` with zero queries.
- [ ] Five duplicate deliveries → one opt-out.
- [ ] An unknown number → no-op. The OD-1 (b) shared tests are unchanged.

### Templates

- [ ] The schema and partial uniques are tested now. Own-number submit
      and sync tests are written when E-7b clears. Until then, `POST` with
      an own `whatsapp_account_id` → `422`.
- [ ] `validate_template`:
  - a shared-pool template (NULL account) without `{{business_name}}` is
    still rejected (existing test unchanged);
  - an own-number template without it passes that rule;
  - every other rule (placeholders, language, length) is enforced for
    both.

### Config & docs

- [ ] `test_scaffold.py` passes with the new env vars. The docs are
      consistent. Full `pytest` is green, with no skip or `xfail` added.

## Reconciliation with the merged foundation

No foundation code is changed by this documentation task. These are the
implementation changes this spec requires:

1. **Sender listing.** `list_senders` (`services.py:109`) already returns
   own numbers in every status. The *docs* said "only selectable
   (`ACTIVE`) senders". ES-9 keeps the code and corrects
   API-Specification.md.
2. **Provider registration signature.** `register_number(self, merchant,
   number)` (`base.py:76`, `meta_cloud.py:261`, WhatsApp-Architecture.md)
   becomes `register_number(self, credential, phone_number_id, pin)`.
   There is no `Merchant` and no DB access in the provider.
3. **Shared token in `_request`** (`meta_cloud.py:70`). It gains a
   `token` argument, with per-account selection via `_token_for`. The
   shared-pool behaviour is byte-for-byte unchanged.
4. **Templates assume the shared WABA** (`services.py:259`, `283`, `327`).
   They resolve the WABA from `template.whatsapp_account` (NULL = shared,
   as today). Own-number branches are blocked by E-7b.
5. **Inbound STOP resolves only the shared number** (`tasks.py:99`). The
   own-number branch goes via the ES-3 lookup (approved 2026-10-09).
6. **`validate_template` requires `{{business_name}}` everywhere**
   (`services.py:217`). It becomes required for shared-pool templates
   only (ES-2 sub-decision approved 2026-10-09; Business-Rules §9
   clarified). The shared behaviour is unchanged.
7. **Status-only webhook events** are acknowledged and discarded, and
   that is unchanged. Own-number status notifications and
   `account_update` deliveries (E-1d, E-8b) are also acknowledged and
   discarded until Phase 11 (statuses) or the ES-16 follow-up
   (`account_update`). The receiver stays at zero DB queries.

## Out of scope

- Phase 10/11 items: `WhatsAppMessage`, status persistence, sends,
  send-rate cap, campaign activation.
- **Known limitation, ES-16: `PARTNER_APP_UNINSTALLED` (E-8b).** If a
  merchant removes ReviewFlow's app in Meta, ReviewFlow is not told:
  - the receiver acknowledges and discards `account_update` deliveries,
    as today;
  - the account stays `ACTIVE` locally, while Meta calls with its token
    will fail.

  Production implication: from Phase 11, sends from that number will
  fail until the merchant disconnects or reconnects. Follow-up: handle
  `account_update` with a WABA-id lookup, which would be another
  pre-tenant lookup, and therefore a future locked-decision change.
- Own-number quality monitoring; account/number status webhooks;
  credential refresh (unless E-2c requires it).
- Transferring a number between merchants (Phase 16); deregistration;
  webhook overrides; showing the display number.
- Admin registrations; other providers; shared-pool rotation.

## Open Decisions (status after the 2026-10-09 amendment)

| ES | Decision | Status |
|---|---|---|
| ES-1 | `WhatsAppAccount.access_token_enc` (Fernet). Per-merchant token required by E-3. No expiry or refresh schema until E-2c | **RESOLVED** (the user's chosen option, 2026-10-09; Data-Dictionary updated). Expiry part **BLOCKED BY EVIDENCE** (E-2c) |
| ES-2 | Nullable `MessageTemplate.whatsapp_account`; two partial uniques | **RESOLVED** (schema). Own-number template operations **BLOCKED BY EVIDENCE** (E-7b) |
| ES-2 sub | `{{business_name}}` required only for shared-pool templates (`whatsapp_account` is NULL) | **APPROVED 2026-10-09** |
| ES-3 | `whatsapp_number_lookup` SELECT-only policy + `whatsapp_number_lookup_atomic`, exactly as written above | **APPROVED 2026-10-09** (locked-decision change) |
| ES-4 | Synchronous-call exception for exactly three calls: exchange, ownership check, registration | **APPROVED 2026-10-09** (locked-decision change) |
| ES-5 | `409 sender_in_use`; no implicit unmap; local-only disconnect: the token is cleared, there is no deregister and no unsubscribe, and verified STOPs are still honoured. Residual risk accepted: Meta's grant is not revoked (E-8c UNVERIFIED) and deliveries may continue | **APPROVED 2026-10-09** |
| ES-6 | Keep `ACTIVE`/`PENDING`/`SUSPENDED`. `ACTIVE` = registered + subscribed, not proven send-ready | **RESOLVED** |
| ES-7 | OWNER + ADMIN for `start`/`complete`/`DELETE`; any role for `GET` | **RESOLVED** |
| ES-8 | `whatsapp.own_number_connected` `{reconnect}`, `whatsapp.own_number_disconnected` `{}`, `whatsapp.own_number_activation_failed` `{}` (system). No ids, tokens, codes or PINs | **RESOLVED** |
| ES-9 | Own numbers in all statuses; doc corrected | **RESOLVED** |
| ES-10 | Flag defaults to off; production gates listed | **RESOLVED** |
| ES-11 | Merchant-entered PIN, request memory only, inline registration, never retried automatically | **RESOLVED** (ES-4 approved 2026-10-09). E-6b/E-6e/E-6f remain UNVERIFIED |
| ES-12 | Retry exhaustion → `PENDING`; recovery = reconnect within 14 days | **RESOLVED** |
| ES-13 | No mapping during `complete` | **RESOLVED** |
| ES-14 | v4 only; v4 `extras` PARTIAL | **RESOLVED**. Frontend details **BLOCKED BY EVIDENCE** (E-1a) |
| ES-15 | Missing `phone_number_id` → `422 embedded_signup_no_phone_number` | **RESOLVED** |
| ES-16 | `PARTNER_APP_UNINSTALLED` out of scope; known limitation | **RESOLVED** |
| ES-17 | Tech Provider model; merchant pays Meta | **APPROVED 2026-10-09**, subject to Meta business verification and App Review before production (production gate 3) |
| ES-18 | Signup state in `EmbeddedSignupState`: SHA-256 hash only; bound to merchant and user; expires; consumed by one conditional delete in a short committed transaction before any Meta call; never restored; standard `rls_direct`; one migration (`0009`) | **APPROVED 2026-10-09** (replaces the session-stored state) |
