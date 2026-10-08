# Spec: WhatsApp Foundation

## Overview

This spec delivers the WhatsApp sender layer that the campaign engine
(Phase 10) and dispatch (Phase 11) build on. It belongs to the
automation plane. It covers:

- the `whatsapp` app with `WhatsAppAccount` (shared platform
  `SHARED_POOL` sender only in this spec), `WhatsAppLocationMapping`
  and `MessageTemplate`;
- the `WhatsAppProvider` interface and its single V1 implementation,
  `MetaCloudProvider`;
- template submission to Meta, plus approval polling;
- the "use shared number" onboarding path (User-Flows.md §2);
- the inbound WhatsApp webhook and customer opt-out, both automatic
  (keyword) and manual (dashboard);
- shared-number quality monitoring;
- the seed `SHARED_POOL` fixture.

It exists now because Phase 10's activation rule ("WhatsApp sender
configured and template approved") and Phase 11's send path both need a
resolvable sender and an approved template. Phase 08 depends only on
Phase 03, so nothing here can reference `CampaignExecution`.

**Scope decisions taken with the user while writing this spec
(2026-10-07):**

1. **Phase 08 is split into two specs.** This spec is
   `08-whatsapp-foundation`. The second is `08-whatsapp-embedded-signup`:
   the `OWN_NUMBER` path via Meta Embedded Signup,
   `WhatsAppProvider.register_number`, encrypted Meta credentials, the
   `OWN_NUMBER` webhook merchant lookup and the template-to-WABA binding
   for own numbers. It runs behind its own Meta verification gate.
2. **Deferred to Phase 11:**
   - the `WhatsAppMessage` model;
   - persistence of status notifications (they arrive on the single
     WhatsApp webhook endpoint, which Phase 08 builds; Phase 08
     acknowledges them and stores nothing);
   - `WhatsAppService.send_template(execution, …)`.

   Data-Dictionary.md makes `WhatsAppMessage.execution_id` a non-null
   1:1 FK to `CampaignExecution` (Phase 10). The model has no
   `merchant_id`, so until Phase 10 exists it has no tenant path and no
   RLS policy. Phase 11 already owns "1:1 `WhatsAppMessage` creation in
   `QUEUED` state". This needs a `docs/ROADMAP.md` edit, which ships in
   this spec's PR (see Roadmap Phase).
3. **`SHARED_POOL` storage uses an asymmetric RLS policy on one
   table.** Tenants can SELECT their own rows plus `merchant_id IS NULL`
   rows. Writes require a merchant match. The platform row is written
   only through an audited management command. This is a **LOCKED
   DECISION CHANGE** (see that section).
4. **STOP to the shared number (OD-1, approved option (b),
   2026-10-07).** A STOP opts the phone out of every merchant that has a
   `Customer` with that phone and a location mapped to the shared
   account.

**Approvals received on 2026-10-07, after the spec was first written:**
- Change 1: APPROVED.
- OD-1: option (b) APPROVED.
- OD-7: the new API surface and its roles APPROVED.
- OD-6: the per-merchant send-rate cap moves to Phase 11.

## Source docs

- `docs/ROADMAP.md` — §08 WhatsApp, §10, §11, V1 Phases table
- `docs/06-automation/WhatsApp-Architecture.md` — all sections: Models,
  Why the Sender Belongs to the Merchant, Service Interface, Provider
  Adapter, Template Structure & Variables, Sending, Delivery/Read/Failure
  Tracking, Opt-Out, Shared Number, Migration, Onboarding Flow
- `docs/01-product/User-Flows.md` — §1 step 3–4, §2 WhatsApp Onboarding
  Flow
- `docs/01-product/Business-Rules.md` — §2 rule 7, §3 Opt-Out, §7
  (location overrides), §9 WhatsApp Compliance
- `docs/01-product/Feature-Scope.md` — V1 list ("WhatsApp sending (own
  number or shared pool)")
- `docs/03-database/Data-Dictionary.md` — `MessageTemplate`,
  `WhatsAppAccount`, `WhatsAppLocationMapping`, `WhatsAppMessage`,
  `Customer`
- `docs/03-database/Database-Design.md` — §MessageTemplate,
  §WhatsAppAccount, §WhatsAppLocationMapping, §WhatsAppMessage, delete
  behaviour notes
- `docs/03-database/ERD.md` — WhatsApp relationships
- `docs/02-architecture/SAD.md` — §2 (WhatsAppService), §3 (`whatsapp/`
  app), §5 (queues, Beat), §6, §8 (send failures)
- `docs/02-architecture/Multi-Tenancy.md` — Layer 1, Layer 2, the four
  signed-off lookup policies, No Standing Privileged Role, Data Model
  Note
- `docs/FINAL-ARCHITECTURE-REVIEW.md` — §8 RLS, §9 Retention, §10
  WhatsApp send recovery, Remaining work (verify Meta requirements)
- `docs/04-api/Webhook-Specification.md` — Inbound Webhook Endpoints
  (the single `/webhooks/whatsapp` row, amended by this spec),
  fail-closed rule, Security Notes
- `docs/04-api/API-Specification.md` — General Conventions, Locations,
  Campaigns (`PATCH /campaigns/{id}` activation prerequisites)
- `docs/04-api/Authentication.md` — §3 (Meta Embedded Signup; deferred to
  the second spec)
- `docs/09-security/Security-Controls.md` — Encryption & Secrets
  (`META_APP_SECRET`), Rate Limiting (per-merchant send-rate cap),
  Webhook Security, Identifiers, RLS, Privileged Admin Operations
- `docs/09-security/Audit-Logging.md` — "WhatsApp sender changed (own
  number ↔ shared pool)"; platform-side privileged actions
- `docs/09-security/Privacy-Data-Retention.md` — WhatsApp content 90
  days; Consent / Opt-Out
- `docs/10-development/Coding-Standards.md` — §1–§7
- `docs/10-development/Testing-Strategy.md` — WhatsApp tests, Tenant
  Isolation, Opt-Out Enforcement (the eligibility half is Phase 10),
  RLS
- `docs/10-development/Development-Setup.md` — Seed Data

## Depends on

- **Phase 03 (Done):**
  - `Location` (`locations_location`);
  - `locations.services.accessible_locations` (MANAGER scoping);
  - `seed_dev`.
- **Phase 01/02 (Done):**
  - `BaseModel`, `TenantScopedManager`, `tenant_context`,
    `tenant_atomic`, `tenant_task`;
  - `core/rls.py` (`rls_direct`, `rls_via_parent`);
  - `core.crypto`;
  - role permission classes (`IsOwnerOrAdmin`, `IsOwnerAdminOrManager`,
    `IsMerchantMember`);
  - `auditlog.services.record`.
- **Phase 04 (Done):**
  - `customers.Customer` (`opted_out`, `opted_out_at` already exist;
    the model comment marks them for Phase 08);
  - `integrations.core.schemas.validate_e164` and `verify_hmac_sha256`.
- **Phase 06 (Done):** the `webhook_ip` throttle rate and the
  webhook-receiver view pattern.
- **Phase 07 (Done):** `seed_dev` billing extension, and the per-merchant
  Beat fan-out pattern in `billing/tasks.py::run_billing_maintenance`
  that this spec copies.

## Roadmap Phase

- Phase: 08 — WhatsApp
- Completes entire phase: No
- If No, remaining phase work:
  - `08-whatsapp-embedded-signup`: `OWN_NUMBER` onboarding (Meta
    Embedded Signup), `register_number`, encrypted Meta credentials, the
    `OWN_NUMBER` inbound-webhook merchant lookup, template binding for
    own numbers, and the shared→own migration flow with its audit event.
  - **Moved out of Phase 08 by this spec (ROADMAP edit, user sign-off
    2026-10-07):** the `WhatsAppMessage` model, status-webhook
    persistence and `send_template` move to Phase 11.
  - **Moved to Phase 11 (OD-6, user sign-off 2026-10-07):** the
    per-merchant send-rate cap.

**ROADMAP edit carried in this PR:**
- §08: the bullet "`WhatsAppMessage`; status and inbound webhooks;
  opt-out" becomes "inbound webhook; opt-out".
- §08: the bullet "Embedded Signup; per-merchant send-rate cap;
  shared-number quality monitoring" becomes "Embedded Signup;
  shared-number quality monitoring".
- §11 gains two bullets:
  - "`WhatsAppMessage` model and persistence of status notifications
    from the single `/webhooks/whatsapp` endpoint (moved from Phase 08,
    spec 08-whatsapp-foundation)";
  - "Per-merchant WhatsApp send-rate cap (Security-Controls.md; moved
    from Phase 08, spec 08-whatsapp-foundation OD-6)".

In the V1 Phases table:
- Phase 11 already depends on 08 and 10, so its row is unchanged.
- **Phase 15's "Depends on" gains 11**, because the 90-day WhatsApp
  content purge targets `WhatsAppMessage`, which now lands in Phase 11.
  It becomes `02, 04, 08, 09, 11, 12`.

## Locked decisions touched

- Shared schema, `merchant_id` isolation (`Multi-Tenancy.md` §Model) —
  DEPENDS ON
- `TenantScopedManager` as primary application scoping
  (`Multi-Tenancy.md` §Layer 1) — DEPENDS ON
- PostgreSQL RLS, transaction-local `SET LOCAL`, no ordinary connection
  bypasses RLS (`FINAL-ARCHITECTURE-REVIEW.md` §8) — LOCKED DECISION
  CHANGE (Change 1)
- No standing privileged role (`Multi-Tenancy.md` §No Standing
  Privileged Role) — LOCKED DECISION CHANGE (Change 1 adds a narrow,
  audited platform-write path; no role or connection changes)
- Celery merchant context, explicit `merchant_id` (`Multi-Tenancy.md`
  §Layer 1) — DEPENDS ON
- WhatsApp provider architecture: `WhatsAppProvider`, `MetaCloudProvider`
  only, campaigns call only `WhatsAppService`
  (`WhatsApp-Architecture.md` §Provider Adapter) — DEPENDS ON
- `CampaignExecution` / `WhatsAppMessage` responsibility split;
  `DELIVERED`/`READ` only on `WhatsAppMessage` (`Campaign-Engine.md`
  §Responsibility Split) — NO CHANGE (no `WhatsAppMessage` in this spec)
- WhatsApp send recovery, 1:1 `QUEUED` `WhatsAppMessage`
  (`FINAL-ARCHITECTURE-REVIEW.md` §10) — NO CHANGE (Phase 11)
- Quota reservation at `SCHEDULED → SENDING`
  (`FINAL-ARCHITECTURE-REVIEW.md` §4) — NO CHANGE
- Poll-based dispatch, no `eta`/`countdown` (`SAD.md` §5) — NO CHANGE
  (this spec sends nothing)
- Provider webhooks fail closed with `401` (`Webhook-Specification.md`)
  — DEPENDS ON
- Retention: WhatsApp content 90 days (`FINAL-ARCHITECTURE-REVIEW.md`
  §9) — DEPENDS ON (no inbound message body is persisted at all)
- One `SHARED_POOL` account in V1, rotation is V1.1
  (`WhatsApp-Architecture.md` §Shared Number) — DEPENDS ON
- Roadmap phase content (`docs/ROADMAP.md` §08, §11, §15 dependency) —
  NO CHANGE to any architecture decision. This is a sequencing edit,
  signed off by the user on 2026-10-07 and described under Roadmap
  Phase.

LOCKED DECISION CHANGE — USER SIGN-OFF REQUIRED

### Change 1 — `SHARED_POOL` visibility and a platform-write path on `whatsapp_whatsappaccount`

**Status: APPROVED by the user on 2026-10-07.** The approval covers the
exact policies below, on both `whatsapp_whatsappaccount` and
`auditlog_auditlog`. Any change to them during implementation needs
fresh sign-off.

**What changes.** `whatsapp_whatsappaccount` holds both kinds of row:

- `OWN_NUMBER` rows, owned by a merchant;
- `SHARED_POOL` rows, which have `merchant_id IS NULL` (GLOBAL,
  Database-Design.md §WhatsAppAccount).

Every existing tenant table uses one symmetric `tenant_isolation`
policy. That policy cannot serve this table:

- `merchant_id = current` hides the shared row from every tenant;
- a NULL-tolerant `WITH CHECK` would let any tenant insert or edit a
  "shared" sender.

A PostgreSQL policy applies to exactly one command (or `ALL`), and
`FOR INSERT` accepts only `WITH CHECK`. This table therefore gets six
PERMISSIVE policies, one command each. Here `CUR` is the existing
`core.rls.CURRENT_MERCHANT` expression, and `PW` is
`NULLIF(current_setting('app.platform_write', true), '')`:

| Policy | Command | `USING` | `WITH CHECK` |
|---|---|---|---|
| `tenant_read` | SELECT | `merchant_id = CUR OR merchant_id IS NULL` | — |
| `tenant_insert` | INSERT | — | `merchant_id = CUR AND sender_type = 'OWN_NUMBER'` |
| `tenant_update` | UPDATE | `merchant_id = CUR` | `merchant_id = CUR AND sender_type = 'OWN_NUMBER'` |
| `tenant_delete` | DELETE | `merchant_id = CUR` | — |
| `platform_insert` | INSERT | — | `merchant_id IS NULL AND sender_type = 'SHARED_POOL' AND PW = 'whatsapp_shared_pool'` |
| `platform_update` | UPDATE | same predicate as `platform_insert` | same predicate |

What the policies do:

- `tenant_read` makes the shared row readable with or without a
  merchant context. The row holds no secret: `phone_number_id` and
  `business_account_id` are provider references, and the shared access
  token lives only in the environment (see Change 1 notes).
- A tenant can never create, edit or delete a `merchant_id IS NULL`
  row. It also cannot turn its own row into a shared one: the
  `tenant_update` `WITH CHECK` blocks that.
- There is **no platform DELETE policy**, so nobody can delete the
  shared row.

`app.platform_write` is set only by `SET LOCAL` inside a new
`core.tenancy.platform_write_atomic(scope)`, with these properties:
- transaction-local, never session-level, and fail-closed when unset;
- refuses to run while a merchant context is active, because PERMISSIVE
  policies are ORed (same reasoning as the four existing lookups);
- always its own outermost (durable) transaction;
- `scope` must be in an allowlist (`{"whatsapp_shared_pool"}`).

**The platform write must be audited, which needs a second table
change.** `auditlog_auditlog` currently has only `rls_direct`, so a
`merchant_id NULL` insert fails `WITH CHECK`. `auditlog.services.record()`
also requires a tenant context. Change 1 therefore also adds:

- **`auditlog_auditlog` → `platform_insert`** (INSERT,
  `WITH CHECK (merchant_id IS NULL AND PW IS NOT NULL)`).
  - No SELECT, UPDATE or DELETE policy is added; audit rows stay
    immutable.
  - The existing `tenant_isolation` policy is unchanged.
- **`auditlog.services.record_platform(action, *, actor=None,
  target=None, metadata=None)`**. It works only inside
  `platform_write_atomic` and raises otherwise. It writes
  `merchant_id = NULL`. A staff actor needs no merchant membership.

**Why.** The asymmetric policy is the minimum that satisfies four
requirements together:

- the Data-Dictionary model (one table, nullable `merchant_id`);
- "no ordinary connection bypasses RLS";
- "cross-tenant/platform access goes only through an explicit, audited
  privileged path";
- the fact that the app role owns its tables with `FORCE ROW LEVEL
  SECURITY`.

The alternative, a separate global table, would change the
Data-Dictionary model and the `WhatsAppLocationMapping` FK. The user
rejected it.

**Who may set `app.platform_write`.** Only
`whatsapp/management/commands/configure_shared_pool.py` and `seed_dev`,
through `whatsapp.services.upsert_shared_pool_account`. Django Admin does
not register this model in this spec (see Admin). Phase 16 owns the
audited admin write path.

**Not changed:**

- the app role (`NOSUPERUSER NOBYPASSRLS`);
- `FORCE ROW LEVEL SECURITY` (stays on);
- no `SECURITY DEFINER`;
- the existing four lookup policies.

**Change 1 notes.** The shared sender's Meta access token is a
**platform** secret (new env var, see Files to change and OD-5). It is
read from the environment only. It is never stored in the database, a
log, an audit row or an API response. This mirrors the
`SHOPIFY_CLIENT_SECRET` / `RAZORPAY_KEY_SECRET` rules in
Security-Controls.md.

**Tables whose RLS this change touches:**
- `whatsapp_whatsappaccount`: new table, six policies.
- `auditlog_auditlog`: one added INSERT-only policy, `platform_insert`.

**Documentation updated in this PR, so docs stay the source of truth:**

- `docs/02-architecture/Multi-Tenancy.md`: a new subsection
  "`SHARED_POOL` sender visibility and platform write —
  `app.platform_write` (signed off 2026-10-07, Phase 08 spec Change 1)".
- `docs/09-security/Audit-Logging.md`: "Platform-side" gains "Shared
  WhatsApp sender configured (`whatsapp.shared_pool_configured`)".
- `docs/03-database/Database-Design.md` §WhatsAppAccount: a short RLS
  note.

## Django apps

- **Created:** `whatsapp` (models, `services.py`, `providers/`,
  `tasks.py`, `views.py`, `serializers.py`, `urls.py`, `admin.py`,
  `management/commands/`)
- **Touched:**
  - `core`: `tenancy.py`, `rls.py`, `seed_dev`
  - `customers`: `services.py` (opt-out), plus a new `views.py`,
    `serializers.py` and `urls.py` for the manual opt-out endpoint
  - `config`: settings, urls, Beat schedule
  - `docs/`: ROADMAP, Multi-Tenancy, Database-Design, API-Specification,
    Webhook-Specification, Security-Controls (env var list)

## Models & database changes

Names and types follow `Data-Dictionary.md`. Every model extends
`core.BaseModel` (UUID `id`, `created_at`, `updated_at`).

### `WhatsAppAccount` (table `whatsapp_whatsappaccount`): MERCHANT for `OWN_NUMBER`, GLOBAL for `SHARED_POOL`

| Field | Type | Null | Notes |
|---|---|---|---|
| merchant | FK → `accounts.Merchant`, `PROTECT` | Yes | NULL iff `sender_type = SHARED_POOL` |
| sender_type | CharField choices | No | `OWN_NUMBER`, `SHARED_POOL` |
| provider | CharField choices | No | `meta_cloud`: lowercase provider identifier, following the `Integration.Provider` / billing `Provider` precedent and Data-Dictionary |
| phone_number_id | CharField(64) | No | Meta phone-number id |
| business_account_id | CharField(64) | Yes | Meta WABA id; required for `SHARED_POOL` by the service (template submission needs it) |
| status | CharField choices | No | `ACTIVE`, `PENDING`, `SUSPENDED` |
| connected_at | DateTimeField | Yes | |

**Constraints:**
- `CHECK ((sender_type = 'SHARED_POOL') = (merchant_id IS NULL))`
- `UNIQUE(provider, phone_number_id)`: one row per real number; this is
  also the inbound-webhook lookup key
- **V1 single shared account:** a partial unique index on
  `(sender_type) WHERE sender_type = 'SHARED_POOL'`. It is dropped in
  V1.1 when rotation lands (WhatsApp-Architecture.md §Shared Number).

**Index:** `(merchant_id)`.

**Manager:** `WhatsAppAccountManager(TenantScopedManager)`. It overrides
the tenant filter to
`Q(merchant_id=current) | Q(merchant_id__isnull=True)`, so it mirrors
`tenant_read`. It adds `shared_pool()`, the only read that works without
a tenant context. It returns only `sender_type=SHARED_POOL,
merchant__isnull=True` rows. The Beat quality task, the inbound webhook
and `configure_shared_pool` use it.

**RLS:** see Change 1. A new helper in `core/rls.py`,
`rls_shared_pool(table)`, emits the six policies with ENABLE + FORCE and
a full reverse. A second helper, `rls_platform_insert(table)`, adds the
`auditlog_auditlog` policy.

**`OWN_NUMBER` rows** are never created by this spec: the service raises
on that path. The model and the `tenant_insert`/`tenant_update` policies accept them so the
second spec needs no RLS migration.

### `WhatsAppLocationMapping` (table `whatsapp_whatsapplocationmapping`): LOCATION (transitive)

| Field | Type | Null | Notes |
|---|---|---|---|
| whatsapp_account | FK → `WhatsAppAccount`, `PROTECT` | No | |
| location | OneToOne → `locations.Location`, `PROTECT` | No | `UNIQUE(location_id)` (Data-Dictionary) |

**Manager:** `TenantScopedManager` with
`tenant_field = "location__merchant_id"`.

**RLS:** `rls_via_parent("whatsapp_whatsapplocationmapping",
"location_id", "locations_location")`. The policy goes through the
location, not the account, because the account's `merchant_id` is NULL
for shared rows.

**Service-layer invariant** (enforced before every insert or update, the
same way as `TeamMemberLocation`):
- `account.sender_type == SHARED_POOL`, or
  `account.merchant_id == location.merchant_id`;
- `account.status == ACTIVE`.

RLS cannot prove the account side, so the service is the guarantee.

### `MessageTemplate` (table `whatsapp_messagetemplate`): MERCHANT

| Field | Type | Null | Notes |
|---|---|---|---|
| merchant | FK → `accounts.Merchant`, `PROTECT` | No | |
| name | CharField(255) | No | merchant-facing label |
| language | CharField(16) | No | Meta language code, e.g. `en`, `en_US`, `hi` (validated against an allowlist, OD-4) |
| body | TextField | No | `{{variable}}` placeholders, standard names only |
| status | CharField choices | No | `PENDING`, `APPROVED`, `REJECTED` |
| provider_template_id | CharField(64) | Yes | Meta's template id, set after submission succeeds |

**Constraints:**
- `UNIQUE(merchant_id, name, language)`, which also makes double-submit
  idempotent;
- partial `UNIQUE(provider_template_id) WHERE provider_template_id IS
  NOT NULL`.

**Index:** `(merchant_id, status)`. Activation (Phase 10) asks "does an
`APPROVED` template exist".

**RLS:** `rls_direct("whatsapp_messagetemplate")`.

**Provider-side name** (proposed, OD-3): Meta template names are unique
per WABA, and every shared-pool merchant submits to the same platform
WABA. The name sent to Meta is therefore derived, `rf_<template uuid
hex>`, and never the merchant's free-text `name`. That makes collisions
across merchants impossible and leaks no merchant text into Meta-side
names.

**No account/WABA column.** Data-Dictionary has none. In this spec every
template is submitted to the single `SHARED_POOL` WABA. Binding
templates to an own-number WABA is decided in the second spec (OD-2).
This spec adds no column for it.

### `Customer` (existing, `customers_customer`)

No schema change. `opted_out` / `opted_out_at` start being written.

### Not created in this spec

- `WhatsAppMessage` (Phase 11, by the scope decision above).
- No column for the shared-number quality rating: Data-Dictionary has
  none, and each poll compares against the threshold statelessly.

### Migrations (one logical change each)

`whatsapp/migrations/`:
1. `0001_initial`: the three models, constraints and indexes.
2. `0002_whatsappaccount_rls`: `rls_shared_pool`.
3. `0003_whatsapplocationmapping_rls`: `rls_via_parent`.
4. `0004_messagetemplate_rls`: `rls_direct`.

`auditlog/migrations/0003_auditlog_platform_insert.py`:
`rls_platform_insert("auditlog_auditlog")` (Change 1).

No data migration. The shared row is created by `configure_shared_pool`
or `seed_dev`, never by a migration, so production values never live in
code.

## API endpoints

**None of these endpoints exist in `docs/04-api/API-Specification.md`
today. All are new and were APPROVED, with the roles below, on
2026-10-07 (OD-7).** This PR adds them
to API-Specification.md (§WhatsApp, §Customers) and adds the Meta
webhook rows to Webhook-Specification.md.

All endpoints follow the General Conventions:
- error shape `{ error: { code, message, field_errors? } }`;
- UUIDs only;
- cursor pagination on lists;
- session + CSRF;
- `merchant_id` derived from the session, never from the request.

**Session (dashboard):**

- `GET /whatsapp/senders` — the senders the merchant can map to: its
  `OWN_NUMBER` rows, plus the shared row when it is `ACTIVE`. Body:
  `{ id, sender_type, status }`.
  `phone_number_id` and `business_account_id` are never returned. Auth:
  session. Roles: any role.
- `GET /locations/{id}/whatsapp-sender` — the location's current sender
  (`{ sender: Sender | null }`). Auth: session. Roles: any role; a
  MANAGER gets `404` for an unassigned location (the same rule as `GET
  /locations/{id}`).
- `PUT /locations/{id}/whatsapp-sender` — `{ whatsapp_account_id }`
  maps or repoints the location; idempotent when it is unchanged. Writes
  the audit event `whatsapp.sender_changed` with metadata `{
  from_sender_type, to_sender_type }` and the location id only. Auth:
  session. Roles: OWNER, ADMIN, or a MANAGER assigned to the location
  (OD-7, approved). Errors:
  - `404`: unknown/foreign location, or an account that does not exist
    or is not visible to the caller (another merchant's `OWN_NUMBER`);
  - `422`: invariant violated for an account the caller can see by id:
    a `PENDING` or `SUSPENDED` account (the caller's own `OWN_NUMBER`, or
    the shared account, which every merchant can read). The distinction
    is deliberate (user decision, 2026-10-07): a non-`ACTIVE` account is
    refused with `422` when explicitly selected, but `GET
    /whatsapp/senders` still lists only selectable (`ACTIVE`) senders,
    and an account the caller cannot see is never revealed by a `422`.
- `DELETE /locations/{id}/whatsapp-sender` — unmaps the location (`204`,
  idempotent). Audited the same way (`to_sender_type: null`). Auth:
  session. Roles: same as `PUT`.
- `GET /whatsapp/templates` — lists the merchant's templates (`{ id,
  name, language, body, status, created_at, updated_at }`). Supports
  `?status=`. Auth: session. Roles: any role.
- `POST /whatsapp/templates` — `{ name, language, body }`. Validates
  (see `validate_template`), creates a `PENDING` row, and enqueues
  submission to Meta after commit. Returns `201`. Auth: session. Roles:
  OWNER, ADMIN (OD-7, approved). Errors:
  - `422`: validation failed, or 503 `whatsapp_not_configured` when no
    `ACTIVE` shared account exists or the platform token is unset;
  - `409`: duplicate `(name, language)`.
- `POST /customers/{id}/opt-out` — sets a manual opt-out (Business-Rules
  §3); idempotent `200 { id, opted_out: true, opted_out_at }`. Auth:
  session. Roles: OWNER, ADMIN, MANAGER (OD-7, approved). Errors: `404`
  for an unknown or foreign customer. Opt back in is **not** provided:
  Privacy-Data-Retention.md requires "a documented merchant process",
  which does not exist yet.

**Webhook signature (Meta):**

- `GET /webhooks/whatsapp` — Meta subscription verification handshake
  (`hub.mode=subscribe`, `hub.verify_token`, `hub.challenge`; gate M-2,
  verified). Same path as the POST below. Answers `200` with the
  `hub.challenge` value only when `hub.mode` is `subscribe` and
  `hub.verify_token` equals `META_WEBHOOK_VERIFY_TOKEN` (constant-time
  compare). Otherwise, or when the token is unset, answers `403`. Auth:
  shared verify token. Roles: n/a. Throttle: `webhook_ip`.
- `POST /webhooks/whatsapp` — Meta's single app-level `messages`
  callback (**amended 2026-10-07, user sign-off, gate M-3**: Meta
  delivers inbound messages and status notifications through one
  `messages` webhook field to one callback URL, so ReviewFlow exposes
  one endpoint instead of the separate `/status` and `/inbound` paths
  that `Webhook-Specification.md` previously listed). Auth:
  `X-Hub-Signature-256` HMAC-SHA256 over the raw body with
  `META_APP_SECRET` (**gate M-1**). Roles: n/a. Behaviour:
  - Order: signature verified **before any database access**. A missing
    or invalid signature, or an unset `META_APP_SECRET`, gets `401`
    (fail closed). Only then is the body parsed.
  - The receiver distinguishes the two kinds of delivery by payload
    shape: `value.messages[]` entries are inbound messages;
    `value.statuses[]` entries are status notifications. One delivery
    may contain either or both.
  - **Inbound messages:** only opt-out processing. For each message it
    extracts only (`phone_number_id`, sender phone, text), and for each
    whole-message keyword match enqueues `fan_out_inbound_opt_out`. It
    returns `200` fast and makes no database query.
  - **Status notifications:** acknowledged with `200` and discarded.
    They are **not persisted**, not logged beyond a count, and trigger
    no `WhatsAppMessage`, `CampaignExecution`, dispatch or Phase 11
    behaviour. Phase 11 adds their persistence on this same endpoint.
  - Nothing from the payload is persisted.
  - Throttle: `webhook_ip`.
- **Not built (user decision, 2026-10-07):** per-phone-number or
  per-WABA callback URL overrides. Meta's app-level callback is the only
  one ReviewFlow uses.

## Services & background tasks

### `whatsapp/providers/base.py`: `WhatsAppProvider(ABC)`

These are the interface methods from WhatsApp-Architecture.md §Provider
Adapter, plus the template and quality methods this spec needs. Adding
methods to an interface is the adapter pattern itself, not a decision
change.

- `send(self, account, to: str, template: MessageTemplate, variables:
  dict) -> ProviderSendResult`
  - Implemented and contract-tested now. **No caller until Phase 11.**
- `parse_status_webhook(self, payload: dict) -> list[StatusUpdate]`
  - Implemented and contract-tested now. Persistence is Phase 11.
- `parse_inbound_webhook(self, payload: dict) -> list[InboundMessage]`
  - `InboundMessage(phone_number_id, from_phone, text)`. It carries no
    reply-context field: Meta's reply `context.id` is unverified (gate
    M-4) and OD-1 (b) does not use it. If reply context is needed later,
    Phase 11 introduces it after Meta verification (user decision,
    2026-10-07).
- `submit_template(self, account, template: MessageTemplate) -> str`
  - Returns `provider_template_id`.
- `fetch_template_status(self, account, provider_template_id: str) -> TemplateStatus`
  - Returns `APPROVED` / `REJECTED` / `PENDING`.
- `fetch_quality_rating(self, account) -> str`
  - Returns Meta's rating string (gate M-6).
- `verify_signature(self, raw_body: bytes, signature_header: str | None) -> bool`
- `register_number(...)` — **not implemented** (second spec). It is
  declared, and `MetaCloudProvider` raises `NotImplementedError` until
  then.

Provider rules:

- Dataclasses `ProviderSendResult`, `StatusUpdate`, `InboundMessage`
  and `TemplateStatus` live in `providers/base.py`.
- Errors are `ProviderTransientError` (network, 5xx, 429) and
  `ProviderPermanentError` (4xx validation, invalid number, template
  rejected). SAD.md §8 says transient errors are retried and permanent
  ones are not.
- Exception messages never contain phone numbers, tokens or bodies.

### `whatsapp/providers/meta_cloud.py`: `MetaCloudProvider`

- The only module that talks to Meta.
  - Stdlib `urllib.request`, like `billing/razorpay.py` and
    `integrations/shopify/services.py`.
  - Graph API base URL and version come from settings
    (`META_GRAPH_API_VERSION`, gate M-7).
  - No database access, no business logic.
  - Called only from `whatsapp/services.py`.
- Signature verification reuses
  `integrations.core.schemas.verify_hmac_sha256` (hex, `sha256=`
  prefix stripped).
- Template variable mapping: ReviewFlow's named placeholders
  (`{{customer_name}}`, `{{business_name}}`, `{{location_name}}`,
  `{{review_link}}`) are translated to Meta's parameter format at
  submit and send time. Whether that format is positional or named is
  gate M-5.
- The access token is `settings.META_SHARED_POOL_ACCESS_TOKEN` for a
  `SHARED_POOL` account (OD-5). No other account kind is supported here.
- A request timeout is always set.

### `whatsapp/providers/__init__.py`

`get_provider(account) -> WhatsAppProvider` dispatches on
`account.provider`. It is a dict lookup, not a registry framework.

### `whatsapp/services.py`

All functions run in the caller's tenant context unless noted, and raise
`core.exceptions` subclasses that the existing API error handler maps.

- `upsert_shared_pool_account(*, phone_number_id: str,
  business_account_id: str, status: str, actor_user_id=None) ->
  WhatsAppAccount`
  - Runs inside `platform_write_atomic("whatsapp_shared_pool")`.
  - Creates or updates the single shared row and writes the platform
    `AuditLog` (`whatsapp.shared_pool_configured`, metadata: status
    only).
  - Raises `TenantContextError` if a merchant context is active.
  - Idempotent: running it again with the same values changes nothing
    and writes no second audit row.
- `list_senders() -> QuerySet[WhatsAppAccount]`
- `get_location_sender(location) -> WhatsAppAccount | None`
  - The "what account does this location map to" query
    (WhatsApp-Architecture.md §Why the Sender Belongs to the Merchant).
  - Returns only an `ACTIVE` account. Phase 10 activation and Phase 11
    dispatch call this.
- `set_location_sender(*, location, account, actor) ->
  WhatsAppLocationMapping`
  - Validates the invariant, then upserts on `UNIQUE(location_id)` via
    `update_or_create` inside `tenant_atomic`, with the IntegrityError
    retry pattern from `customers.services.get_or_create_customer`.
  - Records `whatsapp.sender_changed` only when the account actually
    changed.
  - Raises `NotFound` (account not visible) or `ValidationError`
    (invariant).
- `clear_location_sender(*, location, actor) -> None`
  - Idempotent; audited only when a mapping existed.
- `validate_template(*, name, language, body) -> None`
  - Raises `ValidationError` with `field_errors` when:
    - the body has an unknown `{{placeholder}}` (only the four standard
      names are allowed);
    - the body is missing `{{business_name}}`, which every template here
      targets the shared pool and so must include (Business-Rules §9,
      WhatsApp-Architecture.md "Shared-pool sends must lead with
      `{{business_name}}`"; whether "lead" means *first placeholder* is
      OD-3);
    - the language is not allowlisted;
    - the length exceeds Meta's limit (gate M-5).
  - This is the doc's `validate_template(template) -> bool`, shaped as a
    raise-on-invalid service.
- `create_template(*, name, language, body) -> MessageTemplate`
  - Validates, then inserts `PENDING`. `UNIQUE(merchant, name,
    language)` gives `409` (the IntegrityError is caught, not
    check-then-insert).
  - Enqueues `submit_template` via `transaction.on_commit`.
- `submit_template_to_provider(template_id) -> None`
  - Called by the task. Idempotent: it is a no-op when
    `provider_template_id` is already set or the status is not
    `PENDING`.
  - On `ProviderPermanentError` it sets `REJECTED`.
  - `ProviderTransientError` propagates so the task retries.
- `sync_template_statuses() -> int`
  - For the current merchant's `PENDING` templates that have a
    `provider_template_id`, fetches the status and applies only a
    `PENDING → APPROVED|REJECTED` transition, under `select_for_update`
    on the row.
  - Returns the count changed. Safe to run twice.
- `templates_pending_sync() -> bool`
  - The cheap per-merchant check used by the Beat fan-out.
- `check_shared_pool_quality() -> None`
  - No tenant context needed (uses `shared_pool()`).
  - For each `ACTIVE` shared account, fetches the rating. When it is in
    `settings.WHATSAPP_QUALITY_ALERT_RATINGS` (OD-5) it emits the V1
    "internal alert": `logger.error("whatsapp.shared_pool_quality_low",
    account id, rating)`, with no phone number. Whether to also write an
    `AuditLog` row is OD-5.
- `handle_inbound(raw_body: bytes, signature: str | None) -> list[tuple]`
  - Used by the view.
  - Verifies the signature first and raises `WebhookSignatureError` (→
    `401`).
  - Parses, and returns `(phone_number_id, from_phone)` pairs whose
    trimmed, case-folded text is an opt-out keyword
    (`settings.WHATSAPP_OPT_OUT_KEYWORDS`, OD-4).
  - Never logs or returns the text.
- `shared_opt_out_applies(phone: str) -> bool` (OD-1 (b), approved)
  - Runs in the tenant context.
  - True when the current merchant has a `Customer` with this phone
    **and** at least one of its locations is currently mapped to the
    `SHARED_POOL` account.
  - Called by the fan-out task.

**Inbound flow under OD-1 (b):**

1. The view verifies the signature, parses the delivery and, for each
   STOP, enqueues `fan_out_inbound_opt_out(phone_number_id, phone)`.
   The view makes **no** database read.
2. The task resolves `phone_number_id` through
   `WhatsAppAccount.objects.shared_pool()`:
   - unknown or not shared → no-op, logging the account id only. The
     webhook already answered `200`, so Meta does not retry forever.
     The `OWN_NUMBER` lookup is the second spec.
   - shared → it iterates the non-`DELETED` merchant ids (the
     `run_billing_maintenance` pattern). Under each merchant's
     `tenant_context` + `tenant_atomic` it checks
     `shared_opt_out_applies(phone)`, and on true it `on_commit`
     enqueues `process_inbound_opt_out(merchant_id, phone)`.
3. A failure for one merchant is logged (merchant id + exception class
   only) and the loop continues.

The opt-out never comes from the payload's choice of merchant: the
merchant comes only from existing `Customer` and mapping rows read
under that merchant's own RLS context.

### `customers/services.py` (extended)

- `opt_out_customer(*, phone: str | None = None, customer=None, source:
  str, actor=None) -> Customer | None`
  - Runs in the tenant context.
  - Sets `opted_out=True` and `opted_out_at=now` only if they are not
    already set (a conditional `UPDATE … WHERE opted_out = false`, so
    two concurrent STOPs are safe).
  - `source` is `INBOUND_KEYWORD` or `MANUAL`.
  - Returns `None` when no `Customer` exists for the phone. A phone with
    no `Customer` was never messaged; Data-Dictionary allows a
    `Customer` only from a sale.
  - Never deletes history (Business-Rules §3).
  - This is the doc's `handle_opt_out(phone)`; opt-out belongs to the
    `customers` app (SAD.md §3).
  - A manual opt-out writes no `AuditLog` row (OD-7, approved).
    Audit-Logging.md does not list it.

### `core/tenancy.py`, `core/rls.py` (extended)

- `platform_write_atomic(scope: str)`
  - `SET LOCAL app.platform_write = %s` inside a new durable atomic
    block.
  - Refuses an active merchant context.
  - `scope` is checked against an allowlist (`{"whatsapp_shared_pool"}`).
- `rls_shared_pool(table)`: the six `whatsapp_whatsappaccount` policies of Change 1; `rls_platform_insert(table)`: the `auditlog_auditlog` INSERT policy, with ENABLE,
  FORCE and a full reverse.

### Celery tasks (`whatsapp/tasks.py`)

| Task | Queue | Args | Notes |
|---|---|---|---|
| `submit_template(merchant_id, template_id)` | `whatsapp` | explicit `merchant_id` | `@tenant_task`; `autoretry_for=(ProviderTransientError,)`, exponential backoff, capped retries; after the cap the row stays `PENDING` without a `provider_template_id`, visible in admin. No `eta`/`countdown` on any send (there is no send) |
| `poll_template_statuses()` | `default` | none | Beat fan-out: copies `run_billing_maintenance`, iterates non-`DELETED` merchant ids, and per merchant under `tenant_context` + `tenant_atomic` checks `templates_pending_sync()`, then `on_commit` enqueues `sync_merchant_templates(merchant_id)`. Per-merchant failures are logged (id + exception class only) and the loop continues |
| `sync_merchant_templates(merchant_id)` | `default` | explicit `merchant_id` | `@tenant_task`; calls `sync_template_statuses()` |
| `monitor_shared_pool_quality()` | `default` | none | Platform-level; reads no tenant table except the GLOBAL shared row |
| `fan_out_inbound_opt_out(phone_number_id, phone)` | `whatsapp` | none (platform) | OD-1 (b) fan-out, see "Inbound flow". Reads only the GLOBAL shared row and `Merchant` ids outside a tenant context; all tenant reads happen inside each merchant's own context. Idempotent: running it again re-enqueues only no-op opt-outs. The same phone-logging rule as below applies |
| `process_inbound_opt_out(merchant_id, phone)` | `whatsapp` | explicit `merchant_id` | Enqueued once per matching merchant (OD-1 (b)); `@tenant_task`; calls `opt_out_customer(phone=…, source=INBOUND_KEYWORD)`; idempotent. **The phone is a task argument:** the Celery task args log level must not print it (existing worker logging config is checked, and the arg is never interpolated into a log line) |

Template polling and quality polling use the `default` queue so that
slow Meta management calls never compete with `whatsapp` sends (SAD.md
§5 rationale).

### Celery Beat (`config/settings.py` `CELERY_BEAT_SCHEDULE`)

- `whatsapp-template-status`: `whatsapp.tasks.poll_template_statuses`,
  every `WHATSAPP_TEMPLATE_POLL_SECONDS` (proposed default 900, OD-5),
  queue `default`.
- `whatsapp-shared-pool-quality`:
  `whatsapp.tasks.monitor_shared_pool_quality`, every
  `WHATSAPP_QUALITY_POLL_SECONDS` (proposed default 3600, OD-5), queue
  `default`.

### Adapter/provider interfaces implemented

- `WhatsAppProvider` → `MetaCloudProvider`
  (`send`, `parse_status_webhook`, `parse_inbound_webhook`,
  `submit_template`, `fetch_template_status`, `fetch_quality_rating`,
  `verify_signature`).
- `register_number` is the second spec.
- No `BaseAdapter` or `GoogleSyncProvider` work.

### Deferred `WhatsAppService` methods (WhatsApp-Architecture.md §Service Interface)

- `send_template` → Phase 11
- `get_message_status` → Phase 11
- `handle_webhook` (status) → Phase 11
- `validate_template` → here
- `handle_opt_out` → `customers.services.opt_out_customer`, here

## Admin

**Amended at plan time (user-approved, 2026-10-07):** no WhatsApp model
is registered in Django Admin. This follows the `billing/admin.py` and
`accounts/admin.py` precedent: every `whatsapp` model is a tenant table
(or, for `WhatsAppAccount`, a tenant-scoped manager), and
`TenantScopedManager` needs a tenant context, so cross-tenant admin
waits for the audited Phase 16 path.

- `whatsapp/admin.py` holds only a docstring explaining this.
- Template status (including stuck `PENDING` submissions) stays visible
  through `GET /whatsapp/templates`.
- The shared row is created only by `configure_shared_pool` and
  `seed_dev`.

## Files to change

- `config/settings.py`:
  - `INSTALLED_APPS += "whatsapp"`;
  - `META_APP_SECRET`, `META_WEBHOOK_VERIFY_TOKEN`,
    `META_SHARED_POOL_ACCESS_TOKEN`, `META_GRAPH_API_VERSION`,
    `WHATSAPP_TEMPLATE_POLL_SECONDS`, `WHATSAPP_QUALITY_POLL_SECONDS`,
    `WHATSAPP_QUALITY_ALERT_RATINGS`, `WHATSAPP_OPT_OUT_KEYWORDS`
    (empty-string defaults for secrets, so an unset secret fails
    closed);
  - two Beat entries.
- `config/urls.py`: include `whatsapp.urls` and `customers.urls`.
- `config/tests/test_scaffold.py`: the exact `CELERY_BEAT_SCHEDULE`
  assertion gains the two entries, and the env placeholder list gains
  the new `META_*` vars.
- `.env.example`: new `META_*` / `WHATSAPP_*` vars with comments
  (platform secrets, never commit).
- `core/tenancy.py`: `platform_write_atomic`.
- `core/rls.py`: `rls_shared_pool`, `rls_platform_insert`.
- `auditlog/services.py`: `record_platform` (Change 1).
- `docs/09-security/Audit-Logging.md`: the platform-side audit entry.
- `core/management/commands/seed_dev.py`: `seed_whatsapp(merchant)`.
  - Creates the dev `SHARED_POOL` account through
    `upsert_shared_pool_account`, with obviously fake ids
    (`DEV-SHARED-PHONE-ID`, `DEV-SHARED-WABA-ID`) and status `ACTIVE`.
  - Maps both seed locations to it.
  - Idempotent, and runs for an already-seeded database the same way
    `seed_billing` does.
- `customers/services.py`: `opt_out_customer`.
- `customers/models.py`: remove the stale "Never set in Phase 04"
  comment.
- `docs/ROADMAP.md`: §08 and §11 edits (see Roadmap Phase), plus
  Development-Setup seed note.
- `docs/10-development/Development-Setup.md`: seed now includes the
  `SHARED_POOL` fixture; `configure_shared_pool` usage.
- `docs/02-architecture/Multi-Tenancy.md`: Change 1 subsection.
- `docs/03-database/Database-Design.md` §WhatsAppAccount: RLS note.
- `docs/04-api/API-Specification.md`: new §WhatsApp and §Customers
  sections (OD-7, approved).
- `docs/04-api/Webhook-Specification.md`: the `/webhooks/whatsapp/status`
  and `/webhooks/whatsapp/inbound` rows are replaced by one
  `/webhooks/whatsapp` row (GET handshake and POST delivery), with a short
  description of the receiver (amended 2026-10-07, user sign-off).
- `docs/06-automation/WhatsApp-Architecture.md`: the Delivery/Read/Failure
  Tracking and Opt-Out sections name the single endpoint instead of the
  two paths.
- `docs/09-security/Security-Controls.md`: env secret list gains
  `META_WEBHOOK_VERIFY_TOKEN` and `META_SHARED_POOL_ACCESS_TOKEN`, with
  the "platform credential, never stored" rule.

## Files to create

- `whatsapp/__init__.py`, `apps.py`, `models.py`, `services.py`,
  `tasks.py`, `views.py`, `serializers.py`, `urls.py`, `admin.py` (docstring only, see Admin),
  `exceptions.py` (only if `core.exceptions` lacks a fitting class)
- `whatsapp/providers/__init__.py`, `base.py`, `meta_cloud.py`
- `whatsapp/management/__init__.py`, `commands/__init__.py`,
  `commands/configure_shared_pool.py` (args: `--phone-number-id`,
  `--business-account-id`, `--status`; refuses to run with a merchant
  context; prints no secret)
- `whatsapp/migrations/0001_initial.py`, `0002_whatsappaccount_rls.py`,
  `0003_whatsapplocationmapping_rls.py`, `0004_messagetemplate_rls.py`
- `whatsapp/tests/__init__.py`, `conftest.py` (feature-local fixtures;
  the shared account created through the service)
- `whatsapp/tests/fixtures/`: anonymized Meta payloads
  (`inbound_text_stop.json`, `inbound_text_other.json`,
  `status_delivered.json`, `status_read.json`, `status_failed.json`,
  `template_status_approved.json`, `template_status_rejected.json`,
  `quality_rating.json`, `send_response.json`). Each one is taken from
  Meta's documentation (gate M-8) with the source URL noted in a
  sibling `README` line, and with phone numbers replaced by test
  numbers.
- `whatsapp/tests/test_models_rls.py`, `test_sender_mapping.py`,
  `test_templates.py`, `test_meta_provider_contract.py`,
  `test_inbound_webhook.py`, `test_tasks.py`,
  `test_shared_pool_command.py`
- `customers/views.py`, `serializers.py`, `urls.py`
- `customers/tests/test_opt_out.py`
- `core/tests/test_platform_write.py`
- `auditlog/migrations/0003_auditlog_platform_insert.py`
- `auditlog/tests/test_platform_audit.py`

## New dependencies

No new dependencies. HTTP uses stdlib `urllib.request` (same as
`billing/razorpay.py`). HMAC uses the existing
`integrations.core.schemas.verify_hmac_sha256`.

## Meta verification gate

WhatsApp-Architecture.md says Meta requirements "should be verified
against current Meta documentation before implementation".
FINAL-ARCHITECTURE-REVIEW.md lists this as remaining work. Following
the `docs/05-integrations/Shopify.md` facts table precedent, each item
below must be verified against the current Meta docs, with a URL and
date recorded. The verified facts go in a new
`docs/05-integrations/WhatsApp-Meta.md`, written in this PR. **Nothing
below is asserted by this spec.**

| # | To verify | Affects |
|---|---|---|
| M-1 | Webhook payload signature: header name (`X-Hub-Signature-256`?), algorithm, key (app secret), body encoding | `verify_signature`, fail-closed tests |
| M-2 | GET subscription handshake parameters and expected response | `GET /webhooks/whatsapp` |
| M-3 | Whether statuses and inbound messages arrive on **one** callback URL per app (docs specify two endpoints) | Webhook-Specification.md paths |
| M-4 | Inbound message payload shape: where `phone_number_id`, sender `from`, text body and reply `context.id` live | `parse_inbound_webhook`, OD-1 |
| M-5 | Template create/status API: endpoint, positional vs named parameters, name rules and uniqueness scope, body length limit, required category for review requests (utility vs marketing) | `submit_template`, `validate_template`, OD-3 |
| M-6 | Phone-number quality rating: endpoint, field, possible values | `fetch_quality_rating`, OD-5 |
| M-7 | Current Graph API version and its deprecation date | `META_GRAPH_API_VERSION` |
| M-8 | Official sample payloads usable as anonymized contract fixtures | contract tests |
| M-9 | Platform shared-number access-token model (system-user token, expiry, scopes) | OD-5, `META_SHARED_POOL_ACCESS_TOKEN` |
| M-10 | Meta's own opt-out/STOP handling and any keyword requirements for business-initiated templates | OD-4 |

## Rules for implementation

- Django + DRF monolith. Business logic lives only in `services.py`:
  views, serializers, tasks, admin and the management command call
  services.
- Every tenant-owned model uses `core.TenantScopedManager`
  (`WhatsAppAccountManager` subclasses it), and RLS is enabled with
  FORCE on all three new tables.
- Never trust a client-supplied `merchant_id` or `location_id`:
  - the location comes from the URL, resolved through
    `accessible_locations` for the session user;
  - the merchant comes from the session;
  - for the inbound webhook, the merchant comes only from a stored
    `Customer` + shared-mapping rows read under each merchant's own context (OD-1 (b)), never from the payload.
- Celery tasks take `merchant_id` explicitly and set the tenant context
  first (`@tenant_task`). The two platform Beat tasks take no
  `merchant_id` and read only GLOBAL rows (`Merchant` ids, the shared
  account).
- Idempotency comes from database unique constraints and conditional
  updates, never check-then-insert:
  - `UNIQUE(location_id)`, `UNIQUE(merchant, name, language)`,
    `UNIQUE(provider, phone_number_id)`, and the single-shared-account
    partial unique;
  - template status transitions only from `PENDING`;
  - opt-out is set only when it is not already set.
- Role checks use DRF permission classes (`IsMerchantMember`,
  `IsOwnerOrAdmin`, `IsOwnerAdminOrManager` + `accessible_locations`).
  There are no inline role `if`s.
- External IDs are UUIDs. Meta `phone_number_id` and
  `business_account_id` are never returned by any API.
- Secrets come from env only. `META_APP_SECRET`,
  `META_WEBHOOK_VERIFY_TOKEN` and `META_SHARED_POOL_ACCESS_TOKEN` are
  never stored, logged, audited or returned. An unset secret fails
  closed: the webhook gets `401`/`403`, and template creation gets
  `503 whatsapp_not_configured`. No OAuth token is stored in this spec
  (the encrypted own-number token is in the second spec).
- Status enums are `UPPER_SNAKE_CASE`, and timestamps end in `_at`.
- No V2 features and no bespoke admin app. No `eta`/`countdown`
  anywhere. **No message is sent by any code path in this spec.**
- Webhook signature verification happens before any database read
  (mirrors the Razorpay rule).
- Never log phone numbers, message text, tokens or raw payloads. Inbound
  payloads are **not persisted**: retention is then trivially satisfied.
- `platform_write_atomic` is the only code that sets
  `app.platform_write`. It is never used in a request path in this spec.
- No `WhatsAppMessage`, `CampaignExecution` or `ReviewCampaign` code.
  No campaign-activation check (Phase 10 calls `get_location_sender` and
  the template query).
- Do not implement any item behind an unresolved Open Decision marked
  *blocking* until the user settles it, and do not decide it in a plan.
- Every new service function has a unit test. Every provider method and
  the inbound webhook have a fixture-based contract test using an
  anonymized Meta payload (gate M-8).
- Mock only the Meta HTTP boundary (`MetaCloudProvider`'s urllib call),
  never the services under test.

## Definition of done

Run with `pytest` against real PostgreSQL, with Celery eager where a
task boundary is crossed, unless stated otherwise.

### Schema & RLS

- [ ] `python manage.py migrate` applies `whatsapp` 0001–0004 cleanly.
      `migrate whatsapp zero` reverses them.
- [ ] RLS is enabled **and forced** on all three tables (checked via
      `pg_class.relrowsecurity` / `relforcerowsecurity`).
- [ ] **Tenant isolation, application layer:** Merchant A's session
      cannot list, read or map Merchant B's `OWN_NUMBER` account (a row
      created directly in a B context for the test), cannot see B's
      templates, and cannot read B's location sender. Every case answers
      `404`, never the data.
- [ ] **Tenant isolation, RLS backstop:** raw SQL in Merchant A's
      context:
  - returns only A's rows plus the shared row from
    `whatsapp_whatsappaccount`;
  - returns only A's rows from `whatsapp_messagetemplate`;
  - returns only mappings of A's locations from
    `whatsapp_whatsapplocationmapping`.
- [ ] **Change 1, tenant cannot write the shared row:** in a merchant
      context, raw SQL `INSERT` of a `merchant_id NULL` /
      `SHARED_POOL` row fails. `UPDATE` and `DELETE` of the shared row
      affect 0 rows. An `INSERT` of an `OWN_NUMBER` row for another
      merchant fails.
- [ ] **Change 1, tenant cannot convert its own row:** a tenant
      `UPDATE` that sets its own `OWN_NUMBER` row to
      `merchant_id NULL` / `SHARED_POOL` fails.
- [ ] **Change 1, platform path:** outside `platform_write_atomic`, the
      shared row cannot be inserted. Inside it, it can. Calling
      `platform_write_atomic` while a merchant context is active raises.
      An unknown scope raises. Each write produces exactly one
      platform-level `AuditLog` (`merchant_id NULL`) via
      `record_platform`.
- [ ] **Change 1, audit table:** a `merchant_id NULL` `AuditLog` insert
      fails outside `platform_write_atomic`. Inside it, the inserted row
      cannot be read back, updated or deleted by a tenant context.
      `record_platform` outside the block raises. The existing
      `record()` behaviour is unchanged (existing tests pass).
- [ ] `pg_policies` for `whatsapp_whatsappaccount` lists exactly the six
      Change 1 policies with their commands. For `auditlog_auditlog` it
      lists `tenant_isolation` (ALL) plus `platform_insert` (INSERT).
- [ ] **Change 1, no tenant context:** `WhatsAppAccount.objects.shared_pool()`
      returns the shared row with no tenant context. Any other
      `WhatsAppAccount.objects` query without a context raises
      `TenantContextError`.
- [ ] The CHECK constraint rejects `SHARED_POOL` with a merchant and
      `OWN_NUMBER` without one. A second `SHARED_POOL` row is rejected.
      A duplicate `(provider, phone_number_id)` is rejected.
- [ ] Pooled-connection safety: after a `platform_write_atomic` block
      commits, a following transaction on the same connection cannot
      write the shared row (`SET LOCAL` did not leak).
- [ ] The application role is still `NOSUPERUSER NOBYPASSRLS` (the
      existing test still passes).

### Sender mapping

- [ ] `PUT /locations/{id}/whatsapp-sender` to the shared account → `200`.
      `get_location_sender(location)` returns it, and one
      `whatsapp.sender_changed` audit row is written with
      `{from_sender_type: null, to_sender_type: SHARED_POOL}`. No phone
      or provider id appears in the metadata.
- [ ] Repeating the same `PUT` → `200`, one mapping row, **no** second
      audit row.
- [ ] Mapping to a `SUSPENDED`/`PENDING` account the caller can see (the
      shared account, or the caller's own `OWN_NUMBER`) → `422`. Mapping
      to another merchant's `OWN_NUMBER` id, or to an id that does not
      exist → `404`.
- [ ] Two concurrent `set_location_sender` calls for the same location
      leave exactly one mapping row (`UNIQUE(location_id)`).
- [ ] `DELETE` unmaps (`204`). Repeating it → `204`, with no extra audit
      row.
- [ ] Roles:
  - a VIEWER gets `403` on `PUT`/`DELETE`;
  - a MANAGER gets `404` for an unassigned location and succeeds for an
    assigned one (OD-7);
  - an unauthenticated request gets `403`;
  - a request without CSRF gets `403`.
- [ ] `GET /whatsapp/senders` never contains `phone_number_id` or
      `business_account_id`.

### Templates

- [ ] `validate_template` rejects:
  - an unknown placeholder;
  - a missing `{{business_name}}`;
  - a non-allowlisted language;
  - an over-length body.

  It accepts the WhatsApp-Architecture.md example template.
- [ ] `POST /whatsapp/templates` → `201 PENDING`, and after commit the
      `submit_template` task stores `provider_template_id` (Meta HTTP
      mocked with the fixture). A duplicate `(name, language)` → `409`.
      VIEWER and MANAGER → `403` (OD-7).
- [ ] No `ACTIVE` shared account, or `META_SHARED_POOL_ACCESS_TOKEN`
      unset → `503 whatsapp_not_configured`, and no row is created.
- [ ] `submit_template` run twice for the same template calls Meta once.
      A permanent provider error → `REJECTED`. A transient error →
      retried, and no status change.
- [ ] `sync_template_statuses`: fixture `APPROVED` → `APPROVED`; fixture
      `REJECTED` → `REJECTED`. An `APPROVED` template is never moved
      back. Running it twice is a no-op the second time.
- [ ] **Celery tenant context:** `sync_merchant_templates(merchant_id=A)`
      never reads or updates Merchant B's templates, even when B's
      template id is known.
- [ ] `poll_template_statuses` enqueues one `sync_merchant_templates`
      per merchant that has a submitted `PENDING` template, and none for
      `DELETED` merchants. A failure for one merchant does not stop the
      others.

### Provider contract (fixture-based)

- [ ] Each fixture in `whatsapp/tests/fixtures/` parses to the exact
      expected dataclass. This covers:
  - `parse_inbound_webhook`: text STOP, other text, a reply with
    context;
  - `parse_status_webhook`: delivered, read and failed fixtures →
    `StatusUpdate`s with `DELIVERED` / `READ` / `FAILED`;
  - `fetch_template_status`;
  - `fetch_quality_rating`;
  - `send`: request body built from the named variables;
    `ProviderSendResult` parsed from `send_response.json`.
- [ ] `verify_signature` accepts a correct signature over the raw
      fixture body. It rejects a tampered body, a wrong secret, a
      missing header, a malformed header and an empty
      `META_APP_SECRET`.
- [ ] Transient vs permanent classification: 5xx/429/timeout →
      `ProviderTransientError`; a 4xx validation error →
      `ProviderPermanentError`. No exception message contains the phone,
      token or body.

### Inbound webhook & opt-out

- [ ] **Fail closed:** `POST /webhooks/whatsapp` with a missing
      or invalid signature, or with `META_APP_SECRET` unset → `401`,
      **no database query executed** (asserted with
      `django_assert_num_queries(0)` around the view), and nothing
      enqueued.
- [ ] **Status notifications:** a signed delivery containing only
      `statuses[]` entries (sent, delivered, read, failed) → `200`, no
      database query executed, nothing persisted, nothing enqueued, and
      no `WhatsAppMessage` exists. A delivery containing both a STOP
      message and a status → the STOP is processed and the status is
      discarded.
- [ ] A valid signature with a non-keyword text → `200`, no task
      enqueued. A delivery that contains only statuses → `200`, nothing
      persisted.
- [ ] A valid signed STOP → `200`, and the view itself executes **no**
      database query. Exactly one `fan_out_inbound_opt_out` is enqueued.
- [ ] **Duplicate webhook:** the same signed STOP delivery posted 5 times
      → each matching customer is opted out once (`opted_out_at`
      unchanged after the first), with no error.
- [ ] **OD-1 (b) fan-out:** the phone is a `Customer` of Merchants A, B
      and C, and the shared number receives STOP:
  - A and B have a location mapped to the shared account → both are
    opted out;
  - C has no location mapped to it → C is unchanged;
  - Merchant D has the shared mapping but no `Customer` with that phone
    → no `Customer` is created for D;
  - a `DELETED` merchant is skipped;
  - a failure for one merchant does not stop the others.
- [ ] **Celery tenant context:** `process_inbound_opt_out(merchant_id=A,
      phone)` never changes Merchant B's customer with the same phone.
- [ ] Unknown, or non-shared, `phone_number_id` → `200`. The fan-out
      task is a no-op, with no opt-out and no error.
- [ ] `opt_out_customer`:
  - sets `opted_out`/`opted_out_at` once;
  - a second call leaves `opted_out_at` unchanged;
  - an unknown phone returns `None` and creates no `Customer`;
  - existing transactions are untouched (Business-Rules §3);
  - **cross-tenant:** opting out a phone under Merchant A never changes
    Merchant B's customer with the same phone.
- [ ] `POST /customers/{id}/opt-out` → `200` and idempotent. Another
      merchant's customer id → `404`. VIEWER → `403`; OWNER, ADMIN and MANAGER succeed (OD-7).
- [ ] `GET /webhooks/whatsapp` echoes the `hub.challenge` (`200`) only for
      `hub.mode=subscribe` and the correct verify token. A wrong or unset
      token, or another mode → `403`.
- [ ] No log record emitted during these tests contains the test phone
      number or the message text (a caplog assertion).

### Quality monitoring

- [ ] `monitor_shared_pool_quality` with a rating in
      `WHATSAPP_QUALITY_ALERT_RATINGS` emits exactly one
      `whatsapp.shared_pool_quality_low` error log per poll. A healthy
      rating emits none. A provider error is logged (exception class
      only) and does not crash the Beat task.

### Seed, config, docs

- [ ] `python manage.py seed_dev` on an empty dev database creates the
      `SHARED_POOL` account and maps both seed locations to it. Running
      it again creates nothing new. On a database seeded before Phase 08
      it adds the WhatsApp fixtures.
- [ ] `python manage.py configure_shared_pool --phone-number-id …
      --business-account-id … --status ACTIVE` creates the row once.
      Re-running it is a no-op, and changing the status updates the row
      with one audit row.
- [ ] `config/tests/test_scaffold.py` passes with the two new Beat
      entries and the new env placeholders.
- [ ] `docs/ROADMAP.md` §08/§11, Multi-Tenancy.md (Change 1),
      Database-Design.md, API-Specification.md, Webhook-Specification.md,
      Security-Controls.md, Development-Setup.md and the new
      `docs/05-integrations/WhatsApp-Meta.md` (gate M-1…M-10) are
      updated in the PR.
- [ ] The full suite (`pytest`) is green, with no skipped or `xfail`
      tests added.

### Priority scenarios from Testing-Strategy.md touched by this spec

- Tenant Isolation (both layers): covered above.
- Duplicate Webhook Event, as it applies to inbound WhatsApp: covered
  above (opt-out idempotency).
- RLS pooled-connection safety: covered above (platform write
  `SET LOCAL`).
- WhatsApp tests (adapter mocked): the contract half is covered here.
  The `WhatsAppMessage` status transitions are Phase 11.
- Opt-Out Enforcement: the opt-out write side is covered here. The
  "eligibility fails at rule #2" half is Phase 10.
- Celery tenant context: covered above.

## Out of scope

- **Known V1 limitation (final code review, accepted by the user
  2026-10-08):** a template whose submission to Meta never completes stays
  `PENDING` with no `provider_template_id` and is not retried automatically.
  This happens after the submit task's 5 transient-error retries are used
  up, or when the recovery lookup after a refused submit fails with a
  permanent error (which is not retried). `poll_template_statuses` syncs only
  templates that have a `provider_template_id`, so nothing picks such a row up
  again. The merchant sees it as `PENDING` in `GET /whatsapp/templates`; it can
  never reach `APPROVED`, so it cannot activate a campaign, and the merchant
  can create a new template under a different name (the same name and language
  answers `409`). It is never wrongly `REJECTED`. Upgrade path, not built: have
  the poll also re-enqueue `submit_template` for `PENDING` rows with no
  `provider_template_id` older than an `updated_at` floor; the submit is already
  idempotent.
- `OWN_NUMBER`, Embedded Signup, `register_number`, encrypted Meta
  tokens, the `OWN_NUMBER` inbound lookup policy, and the shared→own
  migration flow. These are in `08-whatsapp-embedded-signup`.
- `WhatsAppMessage`, persistence of status notifications (this phase only
  acknowledges them), `send_template`, `get_message_status` and send recovery: Phase 11.
- The per-merchant send-rate cap: moved to Phase 11 (OD-6, approved).
- Campaign activation checks and eligibility rule 2/7: Phase 10.
- Opt back in (no documented merchant process exists).
- Shared-pool rotation (V1.1), and other providers (Gupshup,
  360dialog).
- Template edit or delete endpoints. Meta-side template edits are not
  in the docs.
- Staff-gated Django Admin writes for the shared account: Phase 16.
- Invalid-signature spike alerting: Phase 18.

## Open Decisions

**Blocking** means the item must be settled before the code it names is
written. All other code in this spec can proceed.

- **OD-1: DECIDED, option (b), user approval 2026-10-07.** Implemented as
  described under "Inbound flow under OD-1 (b)". The original analysis is
  kept below for the record.

  A customer replies STOP to the **shared** number. `Customer.opted_out`
  is per merchant, and many merchants share the number. Which
  merchant(s) are opted out? The docs are silent. Candidates from the
  spec discussion, with what each costs:
  - (a) **The merchant whose message was replied to**, via Meta's reply
    `context.id` → `WhatsAppMessage.provider_message_id`.
    - Needs `WhatsAppMessage` (Phase 11), so it can't ship in Phase 08.
    - Needs a new pre-tenant SELECT-only lookup policy on
      `WhatsAppMessage` by `provider_message_id`, which is another
      LOCKED DECISION CHANGE.
    - A bare STOP with no reply context is unattributable, so a
      fallback rule would still be needed.
  - (b) **Every merchant that has a `Customer` with this phone and a
    location mapped to the shared account.**
    - Reuses the existing per-merchant fan-out over `Merchant` ids,
      like `run_billing_maintenance`: enter each merchant's context,
      then run the conditional opt-out. No new policy is needed.
    - It is the broadest choice: one STOP opts the customer out of
      every shared-pool merchant they bought from.
    - Ships in Phase 08.
  - (c) Another rule.

  *Recorded as open on 2026-10-07; (b) approved later the same day.*
- **OD-2 (second spec).** Template ↔ WABA binding. Meta approves
  templates per WABA, and `MessageTemplate` has no account column. What
  happens to a merchant's shared-WABA templates, and to campaigns that
  reference them, when a location moves to an own number? Not needed by
  this spec, because every template targets the shared WABA.
- **OD-3 (non-blocking, proposed).**
  - Provider-side template name `rf_<uuid hex>`.
  - Interpretation of "lead with `{{business_name}}`": proposed as
    "`{{business_name}}` must appear in the body", not "must be the
    first placeholder".
  - Template category per gate M-5.
- **OD-4 (non-blocking, proposed).**
  - Opt-out keyword set: proposed `{"STOP", "UNSUBSCRIBE"}`, matched
    after trim and case-fold, whole message only. Business-Rules §3 gives
    "STOP" as an example only.
  - Language allowlist: proposed `{"en", "en_US", "hi"}`. Confirm
    against gate M-5 and M-10.
- **OD-5 (non-blocking, proposed).**
  - Quality alert ratings: proposed `{"YELLOW", "RED"}`.
  - "Internal alert" in V1: an error-level structured log; proposed to
    also write no `AuditLog` row, because it is not a privileged action.
  - Poll intervals: template 900 s, quality 3600 s.
  - Platform shared token: `META_SHARED_POOL_ACCESS_TOKEN` (gate M-9).
- **OD-6: DECIDED, user approval 2026-10-07.** The per-merchant send-rate
  cap (Security-Controls.md) moves to Phase 11 alongside `send_template`,
  and is recorded in the ROADMAP §08 and §11 edit. This spec adds no
  rate-cap code.
- **OD-7: DECIDED, user approval 2026-10-07.** The new API surface under
  API endpoints is approved, with these roles:
  - sender `PUT`/`DELETE`: OWNER, ADMIN, assigned MANAGER;
  - template `POST`: OWNER, ADMIN;
  - manual opt-out: OWNER, ADMIN, MANAGER;
  - every `GET`: any role.

  A manual opt-out writes no `AuditLog` row.
