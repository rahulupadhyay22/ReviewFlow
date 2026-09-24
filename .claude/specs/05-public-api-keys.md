# Spec: Public API Keys

## Overview
Adds the public-API credential. An OWNER or ADMIN creates an `ApiKey`
from the dashboard. The plaintext `rf_live_<random32>` is shown exactly
once and only its sha256 hash is stored. Each key carries explicit
permission scopes and can be revoked on its own. External systems send
it as `Authorization: Bearer rf_live_...`. The key resolves to exactly
one merchant, and that merchant's tenant context (contextvar +
`SET LOCAL`) wraps the request. Requests are rate-limited per key and
per IP (Redis-backed DRF throttles).

It exists now because Phase 06's Generic REST API (`POST /sales`)
requires an API key with `sales:write`. This phase builds the
credential, the auth path and the throttles. Its only consumer is
`GET /merchant`, the one existing endpoint documented as "session or
API key". Plane: **ingestion** (it authenticates external systems that
push sales).

## Source docs
- `docs/ROADMAP.md` §05 — Public API keys
- `docs/04-api/Authentication.md` §2 (Public API), §"Session vs. API Key — Quick Reference", intro ("never mix them")
- `docs/04-api/API-Specification.md` — intro (`merchant_id` from principal), §Merchant (`GET /merchant`: "session or API key"), §Sales (`sales:write`), §General Conventions (error shape, pagination, per-key/per-IP rate limits, UUID IDs)
- `docs/02-architecture/Security-Architecture.md` §Authentication/Public API (`API Key -> sha256 hash lookup -> scoped to one Merchant -> merchant context set`), §Authorization
- `docs/02-architecture/Multi-Tenancy.md` §Layer 1, §Layer 2 (incl. §"Login membership lookup — `self_membership`" as the precedent), §No Standing Privileged Role, §Required Tests
- `docs/02-architecture/SAD.md` §3 (`apikeys/` app), Redis roles (rate-limit counters only)
- `docs/03-database/Data-Dictionary.md` §ApiKey
- `docs/03-database/Database-Design.md` §ApiKey (tenant ownership MERCHANT), ERD `Merchant 1───* ApiKey`
- `docs/09-security/Security-Controls.md` §Permissions & Access, §Rate Limiting & Abuse Prevention, §Identifiers, §Row-Level Security
- `docs/09-security/Audit-Logging.md` §What Gets Logged ("API key created / revoked")
- `docs/10-development/Testing-Strategy.md` — API tests row, cross-tenant scenario ("Merchant A's session/API key")
- `docs/10-development/Coding-Standards.md` §1, §5–§6
- `.claude/specs/01-tenant-core.md` — "Note for Phase 05" (see Decision 3 for the deviation)
- `.claude/specs/02-merchant-account-foundation.md` — `user_lookup_atomic` / `for_lookup_user` pattern mirrored here

## Depends on
- Phase 02 (Done): `Merchant` (+ `status`), `User`, `TeamMember`, role
  permission classes, session auth, `AuditLog` + `record()`,
  `core.api` error handler, `CursorPagination`.
- Phase 01 (Done): `TenantScopedManager`, `tenant_context`,
  `tenant_atomic`, `core/rls.py` helpers, `TenantMiddleware`.
- ROADMAP lists only 02 as the dependency; it is Done.

## Roadmap Phase
- Phase: 05 — Public API keys
- Completes entire phase: Yes
- If No: n/a. The spec covers all three roadmap bullets: the hashed,
  shown-once, scoped, revocable `ApiKey`; `Bearer rf_live_...` auth;
  and per-key plus per-IP rate limiting. The first scoped consumer,
  `POST /sales` (`sales:write`), is Phase 06 by the roadmap.

## Locked decisions touched
- Three auth mechanisms, never mixed (`Authentication.md` intro, §2) — DEPENDS ON
- `merchant_id` derived from the authenticated principal, never from request data (`Multi-Tenancy.md` §Layer 1, `API-Specification.md` intro) — DEPENDS ON
- `TenantScopedManager` on every tenant model (`Multi-Tenancy.md` §Layer 1) — DEPENDS ON
- Transaction-local `SET LOCAL app.current_merchant_id` (`FINAL-ARCHITECTURE-REVIEW.md` §8) — DEPENDS ON
- No standing privileged role / no `BYPASSRLS` (`Multi-Tenancy.md` §No Standing Privileged Role) — NO CHANGE
- RLS as the final isolation boundary; pre-tenant reads only via a signed-off SELECT-only policy (`Multi-Tenancy.md` §Layer 2) — LOCKED DECISION CHANGE
- Role permissions via DRF permission classes (`Security-Architecture.md` §Authorization) — DEPENDS ON
- Audit logging of privileged actions (`Audit-Logging.md`) — DEPENDS ON
- Redis holds rate-limit counters only (`SAD.md`) — DEPENDS ON
- UUID external IDs (`Security-Controls.md` §Identifiers) — DEPENDS ON

LOCKED DECISION CHANGE — USER SIGN-OFF REQUIRED

**What changes:** `apikeys_apikey` gets a second RLS policy next to
`tenant_isolation`. It is a SELECT-only `api_key_lookup` policy:
`USING (key_hash = NULLIF(current_setting('app.current_api_key_hash', true), ''))`.
It is the exact analogue of the signed-off `self_membership` policy.

**Why:** a Bearer request carries no merchant. The merchant is only
known after the hash row is read, and `FORCE ROW LEVEL SECURITY`
blocks that read without a merchant context. `Multi-Tenancy.md`
§Layer 2 allows pre-tenant reads only through a signed-off policy
(currently `self_membership` only), so this needs sign-off.

**Alternatives rejected:**
- A `SECURITY DEFINER` lookup function is a standing privileged path,
  against §No Standing Privileged Role.
- Encoding the merchant id in the token changes the documented
  `rf_live_<random32>` format.

**Scope of the change:**
- There is no write policy keyed on `app.current_api_key_hash`.
- The setting is only ever set with `SET LOCAL`.
- A row is visible only to a caller that already holds the plaintext
  key that hashes to it.

**Status: signed off by the user on 2026-09-24.** `Multi-Tenancy.md`
§Layer 2 now documents it in the subsection "API key lookup —
`api_key_lookup`". The approved invariants must not be weakened in
implementation:
- SELECT-only policy on `app.current_api_key_hash`, set via `SET LOCAL`
  only.
- No `SECURITY DEFINER`, no merchant id encoded in the key, no write
  policy keyed on the hash.
- `FORCE ROW LEVEL SECURITY` stays on.
- `for_lookup_hash()` is the only merchant-unscoped `ApiKey` read, and
  only inside the matching lookup context.

## Django apps
- `apikeys`: **created** (listed in `SAD.md` §3 and CLAUDE.md Project
  Structure). Holds the `ApiKey` model, services, the auth/throttle
  classes, the middleware, views and serializers. No admin (Decision
  14).
- `core`: **touched**. Adds `api_key_lookup_atomic()` +
  `get_current_lookup_key_hash()` to `core/tenancy.py`, and
  `rls_select_by_key_hash()` to `core/rls.py`. `core/api.py` gets a
  one-line `WWW-Authenticate` passthrough (Decision 15).
- `accounts`: **touched**. `GET /merchant` accepts an API key; there is
  a small permission-composition change (Decision 7).
- `config`: **touched**. `INSTALLED_APPS`, `MIDDLEWARE`, `urls.py`,
  throttle rates.

## Models & database changes

### `ApiKey` (`apikeys/models.py`, table `apikeys_apikey`)
Fields match `Data-Dictionary.md` §ApiKey exactly, on `core.BaseModel`
(UUID `id`, `created_at`, `updated_at`):

| Field | Django type | Notes |
|---|---|---|
| `merchant` | `ForeignKey(Merchant, on_delete=CASCADE)` | `merchant_id`, not null |
| `key_hash` | `CharField(max_length=64)` | lowercase hex sha256 of the full plaintext key; never plaintext |
| `scopes_json` | `JSONField` | non-empty list of distinct values from the scope allowlist |
| `is_active` | `BooleanField(default=True)` | `False` = revoked; one-way |
| `last_used_at` | `DateTimeField(null=True)` | throttled write, see Decision 6 |

**Scope allowlist** (`ApiKey.Scope` `TextChoices`, the three scopes
named in `Authentication.md` §2): `sales:write`, `reviews:read`,
`transactions:read`. Values are lowercase `resource:action` strings as
documented. They are OAuth-style scope strings, not status enums, so
the `UPPER_SNAKE_CASE` rule does not apply. Any other value is `422`.

No `name`/`label`/`prefix` field, or any other field beyond the Data
Dictionary: none is defined there. Keys are identified in the list by
`id`, `scopes`, `created_at` and `last_used_at`.

Future scope: a human-readable key label needs a Data Dictionary and
model decision in a later phase. It is not part of Phase 05.

**Constraints / indexes**
- `UNIQUE(key_hash)` (`apikeys_apikey_key_hash_uniq`). The lookup
  needs it, and it rejects a (practically impossible) collision at the
  DB instead of by check-then-insert.
- Index on `merchant_id` (the FK index Django creates).

**Manager**
- `ApiKeyManager(TenantScopedManager)` is the default `objects`
  (`tenant_field = "merchant_id"`).
- `ApiKey.objects.for_lookup_hash(key_hash)` is the only
  merchant-unscoped read. It raises `TenantContextError` unless
  `get_current_lookup_key_hash() == key_hash`. It then returns
  `super(TenantScopedManager, self).get_queryset().filter(key_hash=key_hash)`,
  which is exactly the `TeamMemberManager.for_lookup_user` pattern.

**RLS (migration)**
1. `rls_direct("apikeys_apikey")`: the `tenant_isolation` policy
   (USING + WITH CHECK on `merchant_id`), ENABLE + FORCE.
2. `rls_select_by_key_hash("apikeys_apikey")`: the new SELECT-only
   `api_key_lookup` policy above. It is reversible, with the policy
   dropped in reverse.

The migrations are: `0001_initial` (the table + constraint), then
`0002_rls` (both policies). That is one logical change each.

## API endpoints
Base `/api/v1/`. Key management is **session only**
(`authentication_classes = [SessionAuthentication]`), so a key can
never create or revoke keys. These three endpoints are new, and
`API-Specification.md` gets an "API Keys" section in the same PR.

- `GET /api-keys` — list the merchant's keys, active and revoked,
  newest first. Body per key: `{ id, scopes, is_active, created_at,
  last_used_at }`. Never `key_hash`, never the plaintext. Cursor
  pagination (`?cursor=`, `?limit=` max 100), `{ results, next_cursor }`.
  — session — OWNER, ADMIN
- `POST /api-keys` — request `{ scopes: [..] }`, response `201 { id,
  scopes, is_active, created_at, last_used_at, key }`. `key` is the
  plaintext `rf_live_...`, returned only here, once. `422` for a
  missing/empty/non-list `scopes`, an unknown scope, or duplicates.
  Any `merchant_id`, `id`, `is_active` or `key_hash` in the body is
  ignored. — session + CSRF — OWNER, ADMIN
- `DELETE /api-keys/{id}` — revoke (`is_active = False`). `204`.
  Idempotent: revoking an already-revoked key returns `204` and changes
  nothing (no second audit row). `404` for an unknown key or another
  merchant's key (never `403`). — session + CSRF — OWNER, ADMIN
- `GET /merchant` (existing, **changed**) — now also accepts
  `Authorization: Bearer rf_live_...`. Any active key is accepted with
  no scope requirement, because `API-Specification.md` §Merchant
  documents "session or API key" and no scope for it. Session
  behavior is unchanged: a request with no credentials still gets
  `403`, as today. `PATCH /merchant` stays session only.
  - **Phase-05 compatibility consumer.** `GET /merchant` is only the
    Phase-05 compatibility consumer of API-key auth. Its no-scope
    access says nothing about future endpoints: every later
    key-accepting endpoint must name its required scope explicitly
    (e.g. `POST /sales` → `sales:write`). No scope is added for
    `/merchant`.

Public-API auth errors, on every endpoint that accepts a key: `401`
with `WWW-Authenticate: Bearer` and the standard
`{ error: { code: "invalid_api_key", message } }`. It is one generic
response for a malformed, unknown, revoked or suspended-merchant key.
Throttled responses are `429` with `Retry-After`.
`core/api.py` must preserve `WWW-Authenticate` on these responses
(Decision 15).

## Services & background tasks

### `apikeys/services.py`
- `create_api_key(*, actor: TeamMember, scopes: list[str]) -> tuple[ApiKey, str]`
  - Generates the plaintext: `"rf_live_" + secrets.token_urlsafe(24)`,
    which gives 32 URL-safe chars (`Authentication.md` `<random32>`).
  - Stores `sha256(plaintext).hexdigest()` and returns the key with the
    plaintext. The plaintext is never stored or logged.
  - `merchant_id` comes from the tenant context, never an argument.
  - Writes `record("api_key.created", actor=actor.user, target=key,
    metadata={"scopes": scopes})`. Metadata never holds the plaintext
    or the hash.
  - Raises `InvalidScopes` (→ `422`) for scope validation failures,
    enforced here, not only in the serializer.
- `list_api_keys() -> QuerySet[ApiKey]`, tenant-scoped.
- `get_api_key(key_id) -> ApiKey`. Raises `ApiKeyNotFound` (→ `404`).
- `revoke_api_key(*, actor: TeamMember, api_key: ApiKey) -> None`
  - Conditional update `filter(pk=..., is_active=True).update(is_active=False)`.
    Audit (`api_key.revoked`) only when one row changed, so it is
    idempotent without check-then-set.
- `authenticate_api_key(raw: str) -> ApiKey`
  - The pre-tenant lookup. Rejects anything not matching
    `^rf_live_[A-Za-z0-9_-]{32}$` before hashing.
  - Computes the hash, then inside `api_key_lookup_atomic(key_hash)`
    reads `ApiKey.objects.for_lookup_hash(key_hash).select_related("merchant").first()`.
  - Raises `InvalidApiKey` if there is no row, `not is_active`, or
    `merchant.status != ACTIVE`: one exception, one response.
  - Hash comparison happens in the DB by equality on a unique index.
    The secret is 192 bits of entropy, so there is no timing oracle
    worth a constant-time compare, and none is added.
  - **Point-in-time check.** Key state (`is_active`) and merchant state
    (`status == ACTIVE`) are checked once, here, per request (Decisions
    11 and 12).
    - A request that has already authenticated is not retroactively
      cancelled when its key is revoked or its merchant is suspended
      while it runs.
    - Every subsequent request repeats the check. It fails with the
      same generic `invalid_api_key` `401`.
    - No merchant/key row locking, request cancellation or background
      mechanism is added for this.
- `touch_last_used(api_key: ApiKey) -> None` — **best-effort**
  - `last_used_at` is operational metadata, not an authentication
    dependency. The auth result depends only on the lookup, the key's
    `is_active` and the merchant's `status`.
  - Its own short `tenant_atomic()`, before the request-wide
    transaction:
    `filter(pk=...).filter(Q(last_used_at__isnull=True) | Q(last_used_at__lt=now - 60s)).update(last_used_at=now)`.
  - A zero-row match takes no lock. See Decision 6.
  - The caller (`ApiKeyMiddleware`) wraps it so any `DatabaseError`
    from this write is swallowed (logged by exception class name only,
    with no key, hash or merchant-identifying payload). A failed or
    skipped touch never turns a valid authenticated request into a
    `401`.
    - Its own `tenant_atomic()` has already rolled back by then, so
      the request-wide transaction is unaffected.
    - No retry, no Celery task, no new infrastructure.

### `core/tenancy.py`
- `get_current_lookup_key_hash() -> str | None`
- `api_key_lookup_atomic(key_hash: str)`
  - Validates `^[0-9a-f]{64}$` (`ValueError` otherwise), which keeps
    the `SET LOCAL` literal injection-safe.
  - Refuses to run inside a merchant context (`TenantContextError`,
    because PERMISSIVE policies are ORed).
  - Uses `transaction.atomic(durable=True)` with
    `SET LOCAL app.current_api_key_hash = '<hash>'`.
  - Resets the contextvar on exit. It mirrors `user_lookup_atomic`.

### `core/rls.py`
- `rls_select_by_key_hash(table, column="key_hash")`: the SELECT-only
  `api_key_lookup` policy. Never add a write policy keyed on it.

### Auth / throttle plumbing (`apikeys/authentication.py`, `apikeys/throttling.py`, `apikeys/middleware.py`)
- **Opt-in mechanism (normative).** API-key acceptance is an explicit,
  per-method opt-in declared on the view class, and nothing else.
  - **`ApiKeyOptInMixin`** (`apikeys/authentication.py`) is the only
    way to opt in. A view opts in by inheriting the mixin and setting
    `api_key_methods = frozenset({...})`, a non-empty set of HTTP method
    names in upper case. Its default is `frozenset()`.
    - Only `accounts.views.MerchantView` opts in this phase, with
      `api_key_methods = frozenset({"GET"})`.
    - `PATCH /merchant`, every `/api-keys` view, every other existing
      view, and all pre-tenant auth views (`/auth/login`,
      `/auth/login/totp`, `/auth/accept-invite`) do not opt in.
    - Views that do not opt in keep their current, session-only
      authentication with no code change.
  - **Eligibility comes from the resolved view callable.**
    `ApiKeyMiddleware` resolves the request with
    `django.urls.resolve(request.path_info, urlconf=getattr(request, "urlconf", None))`.
    - It reads `cls = getattr(match.func, "cls", None)`, which DRF's
      `APIView.as_view()` sets on the callable.
    - The request is eligible iff
      `request.method in getattr(cls, "api_key_methods", frozenset())`.
    - `Resolver404`, a non-DRF callable, a missing attribute and an
      unlisted method (including `HEAD`/`OPTIONS` unless listed) are all
      not eligible, and the middleware does nothing.
    - Eligibility is never inferred from the request path, URL strings,
      URL names, view names or any naming convention.
    - There is no global switch: API-key auth is never added to
      `REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]`.
  - **Authenticators are exclusive per request.**
    `ApiKeyOptInMixin.get_authenticators()` returns
    `[ApiKeyAuthentication()]` only when the middleware marked the
    request as an API-key attempt. Otherwise it returns `super()`,
    which is the view's existing `SessionAuthentication`.
    - This reads the marker from `self.request`, the Django
      `HttpRequest` that `View.setup()` sets before DRF builds its
      `Request`.
    - So a Bearer request is judged by the key alone: a session cookie
      is never consulted and an invalid key never falls back to it.
    - A request with no Bearer header on an opted-in view is judged
      exactly as before, which keeps today's `403` for no credentials.
  - Because the mixin, the middleware and `ApiKeyAuthentication` all
    key off the same `api_key_methods` attribute, a view cannot accept
    keys by accident. Adding `ApiKeyAuthentication` to
    `authentication_classes` without the mixin has no effect.
  - A test enumerates every URL pattern and asserts that the set of
    `(view class, method)` pairs opted in is exactly
    `{(MerchantView, "GET")}` (see Definition of done).
- `ApiKeyMiddleware`, placed in `MIDDLEWARE` after
  `SessionMerchantMiddleware` and before `TenantMiddleware`. It acts
  only when both hold:
  1. The request has an `Authorization` header with the `Bearer`
     scheme.
  2. The request is eligible under the opt-in mechanism above.

  Then:
  - It sets `request.api_key_attempt = True`, the marker read by
    `ApiKeyOptInMixin`.
  - It first applies the per-IP throttle (`ApiKeyIpRateThrottle`),
    before any hash lookup, so failed-key attempts are counted.
  - It clears any session-derived `request.merchant_id`: Bearer is
    exclusive and never mixes with the session.
  - It calls `authenticate_api_key`, then the best-effort
    `touch_last_used`, then sets `request.api_key` and
    `request.merchant_id` so `TenantMiddleware` wraps the view
    unchanged.
  - On `InvalidApiKey` it sets `request.api_key_error = True` and
    leaves `merchant_id` unset. It does not fall back to the session.

  On an ineligible request (dashboard/session-only views, `/api-keys`,
  pre-tenant auth paths, unlisted methods), the middleware does
  nothing. The Bearer header is then ignored by the view's own
  `SessionAuthentication`, and the request is judged by its session
  alone. A key can never switch the tenant there, and
  `user_lookup_atomic` never runs inside a key-derived tenant context.
- `ApiKeyAuthentication(BaseAuthentication)`
  - Is only ever installed by `ApiKeyOptInMixin` for a marked request.
  - Returns `(ApiKeyPrincipal, api_key)` when `request.api_key` is set.
  - Raises `AuthenticationFailed(code="invalid_api_key")` when
    `api_key_error` is set.
  - `authenticate_header()` returns `'Bearer'` (DRF → `401`, not
    `403`).
  - `ApiKeyPrincipal` is a minimal non-`User` object
    (`is_authenticated = True`, holds the key). It is never a `User`
    and never has a `TeamMember`.
- `HasApiKeyScope(scope)`, a permission-class factory: passes iff
  `request.auth` is an `ApiKey`, `is_active` and `scope in scopes_json`.
  Unused by any endpoint this phase; built for Phase 06's `POST /sales`
  and unit-tested here.
- `ApiKeyRateThrottle(SimpleRateThrottle)` — scope `api_key`, cache
  key = the ApiKey `id`. It applies only when `request.auth` is an
  `ApiKey`. The default rate comes from settings, and there is no
  per-key rate field. "Independently rate-limited" means one counter
  per key.
- `ApiKeyIpRateThrottle(SimpleRateThrottle)` — scope `api_key_ip`,
  keyed on DRF's existing `get_ident()`.
  - The effective client IP is whatever `get_ident()` returns under the
    configured `REST_FRAMEWORK["NUM_PROXIES"]` (`DJANGO_NUM_PROXIES`),
    the same behavior as the Phase 02 login throttles.
  - No new trusted-proxy mechanism, header parsing or IP logic is
    added.
  - It runs in the middleware before the key lookup, so invalid-key
    attempts are counted.
- Rates in `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]`:
  - `api_key` = env `API_KEY_RATE`, default `600/min`
  - `api_key_ip` = env `API_KEY_IP_RATE`, default `1200/min`

  The docs name no numbers, so these are defaults, both
  environment-configurable.
  DRF's `SimpleRateThrottle` keeps a timestamp history per key in the
  Redis cache, which is a sliding window, as the docs require.

- No Celery tasks, no Beat schedules, no adapter/provider interfaces.

## Admin
No admin changes. `ApiKey` is **not** registered in Django Admin in
Phase 05, and there is no `apikeys/admin.py` (Decision 14, approved
and final).

## Files to change
- `config/settings.py`: `INSTALLED_APPS += "apikeys"`, the
  `ApiKeyMiddleware` in `MIDDLEWARE`, and the `api_key` / `api_key_ip`
  throttle rates.
- `config/urls.py`: include `apikeys.urls`.
- `core/tenancy.py`: `api_key_lookup_atomic`,
  `get_current_lookup_key_hash`.
- `core/rls.py`: `rls_select_by_key_hash`.
- `core/api.py`: the one-line header passthrough change. The tuple
  becomes `("Retry-After", "Allow", "WWW-Authenticate")` (Decision 15).
  Nothing else in the handler changes.
- `accounts/views.py`: `MerchantView` changes.
  - It inherits `ApiKeyOptInMixin` with
    `api_key_methods = frozenset({"GET"})`.
  - Its `authentication_classes` stay the session default. The mixin
    swaps in `ApiKeyAuthentication` only for a marked request.
  - `GET` uses the key-or-member permission (Decision 7).
  - `PATCH` stays session + `IsOwnerOrAdmin`, and is not opted in.
  - `_merchant()` must not depend on `request.team_member` or
    `request.user` for a key principal.
- `accounts/permissions.py`: `IsMerchantMemberOrApiKey` (Decision 7).
- `.env.example` (if present): `API_KEY_RATE`, `API_KEY_IP_RATE`.
- Docs already updated during spec clarification (signed off
  2026-09-24), so implementation needs no further doc edits for these:
  - `docs/04-api/API-Specification.md`: the "API Keys" section, and
    the `GET /merchant` compatibility note.
  - `docs/02-architecture/Multi-Tenancy.md` §Layer 2: the
    "API key lookup — `api_key_lookup`" subsection.
  - `docs/04-api/Authentication.md` §2: opt-in, per-request status
    check, best-effort `last_used_at`, IP semantics.
- `docs/03-database/Data-Dictionary.md` §ApiKey: `UNIQUE(key_hash)`
  and the scope allowlist (already documented during spec
  clarification).

## Files to create
- `apikeys/__init__.py`, `apikeys/apps.py`
- `apikeys/models.py`
- `apikeys/migrations/__init__.py`, `apikeys/migrations/0001_initial.py`,
  `apikeys/migrations/0002_rls.py`
- `apikeys/exceptions.py`: `InvalidApiKey`, `InvalidScopes`,
  `ApiKeyNotFound`, on the `core` exception base with API codes.
- `apikeys/services.py`
- `apikeys/authentication.py`: `ApiKeyAuthentication`,
  `ApiKeyPrincipal`, `ApiKeyOptInMixin`
- `apikeys/permissions.py`: `HasApiKeyScope`
- `apikeys/throttling.py`: `ApiKeyRateThrottle`,
  `ApiKeyIpRateThrottle`
- `apikeys/middleware.py`: `ApiKeyMiddleware`
- `apikeys/serializers.py`, `apikeys/views.py`, `apikeys/urls.py`
- No `apikeys/admin.py` (Decision 14).
- `apikeys/tests/__init__.py`, `apikeys/tests/conftest.py`
  (locmem-cache fixture, as in `accounts/tests/conftest.py`)
- `apikeys/tests/test_services.py`, `test_rls.py`, `test_auth_api.py`,
  `test_management_api.py`, `test_throttling.py`

## New dependencies
No new dependencies. `hashlib`, `secrets` and `re` are stdlib, and DRF
throttling plus Django's Redis cache backend are already installed.

## Decisions
1. **Pre-tenant lookup** is the `api_key_lookup` SELECT-only policy
   plus `api_key_lookup_atomic` and `for_lookup_hash`. It mirrors
   `self_membership` exactly (LOCKED DECISION CHANGE above).
2. **The plaintext format** is `rf_live_` + `secrets.token_urlsafe(24)`
   (32 chars of `[A-Za-z0-9_-]`). The hash is sha256 of the full
   string including the prefix. There is no `rf_test_` variant: the
   docs name none.
3. **Where tenant context is entered** is a middleware, not the DRF
   authentication class.
   - This deviates from spec 01's "Note for Phase 05": that note says
     to enter context "from its DRF authentication/view layer".
   - The reason: a DRF authenticator runs inside the view and cannot
     wrap the rest of the request in `tenant_atomic`.
   - Setting `request.merchant_id` before `TenantMiddleware` reuses the
     existing wrapper unchanged.
   - The DRF `ApiKeyAuthentication` still owns the principal and the
     `401`, so the error shape comes from `core.api`.
4. **Opt-in by view, per method.** The only opt-in is
   `ApiKeyOptInMixin` + `api_key_methods`, read from the resolved view
   callable's `cls` (see "Opt-in mechanism (normative)").
   - Never by path, URL string or naming convention, and never global.
   - This protects the pre-tenant paths: `user_lookup_atomic` would
     otherwise raise inside a key-derived tenant context.
   - It also keeps a key from ever reaching dashboard/session-only
     views or `/api-keys` (`Authentication.md`: never mix).
   - The key path swaps authenticators instead of prepending
     `ApiKeyAuthentication`. That keeps no-credential session requests
     on today's `403`, and it guarantees a Bearer request never
     consults the session.
5. **One generic 401.** Malformed, unknown, revoked and
   suspended-merchant keys are indistinguishable (the login pattern).
6. **`last_used_at`** is written in its own short transaction before
   the request-wide `tenant_atomic`, and at most once per 60 s per key.
   An UPDATE inside the request-wide transaction would hold the row
   lock for the whole request and serialize concurrent calls on one key
   (for example Phase 06 `POST /sales` bursts).
   - It is best-effort operational metadata. A `DatabaseError` from the
     touch is swallowed and never changes the authentication result.
   - No retries, no Celery.
7. **Permission composition.** A key principal never reaches
   `IsMerchantMember`, which calls
   `get_active_membership(request.user)`. `IsMerchantMemberOrApiKey`
   branches on the principal's type:
   - An `ApiKey` in `request.auth` passes (plus a scope check where
     the endpoint names a scope).
   - Otherwise it delegates to `IsMerchantMember`.
8. **Per-IP throttle before the lookup.** DRF runs throttles after
   authentication, so a failed key would never be counted. The
   middleware applies `ApiKeyIpRateThrottle.allow_request` before
   hashing. The per-key throttle runs in DRF as usual.
   - When the IP throttle denies, the middleware skips the lookup and
     records the wait on the request. `ApiKeyAuthentication` then
     raises DRF `Throttled(wait)`, so the `429` + `Retry-After` comes
     from DRF in the standard error shape, not from the middleware.
   - The client IP is DRF `get_ident()` under the existing
     `NUM_PROXIES`, with no new trusted-proxy logic.
   - `API_KEY_RATE` (default `600/min`) and `API_KEY_IP_RATE` (default
     `1200/min`) are both environment-configurable.
9. **Key access to `/transactions` is deferred** to Phase 06
   (`GET /sales`). `transactions.services` scopes by
   `request.team_member` (MANAGER location scoping), which a key
   principal does not have.
10. **Management is OWNER/ADMIN and session only**, matching
    `/integrations` and `/team-members`. MANAGER and VIEWER get `403`.
11. **Merchant suspension race.** `merchant.status == ACTIVE` is
    checked inside `authenticate_api_key`, once per request.
    - If the merchant becomes `SUSPENDED` after a request
      authenticated, that in-flight request is not retroactively
      cancelled.
    - Every subsequent request re-checks and gets the generic
      `invalid_api_key` `401`.
    - No merchant row lock and no background mechanism.
12. **Revocation vs in-flight requests.** Revocation affects the
    authentication of subsequent requests only.
    - A request that already authenticated is not retroactively
      cancelled because its key is revoked while it runs.
    - No locking and no request-cancellation mechanism.
13. **`GET /merchant` no-scope access is a Phase-05 compatibility
    consumer only**, per `API-Specification.md` §Merchant. Future
    key-accepting endpoints must each name a required scope. No
    `/merchant` scope is added.
14. **D-A: No Django Admin registration in Phase 05** (approved,
    final).
    - `ApiKey` is not registered in Django Admin, and there is no
      `apikeys/admin.py`.
    - This follows the existing tenant-admin precedent in
      `accounts/admin.py:24`: `TenantScopedManager` requires a tenant
      context, and there is no audited cross-tenant privileged admin
      path yet.
    - No `SECURITY DEFINER`, `BYPASSRLS`, privileged role or any other
      RLS bypass is introduced for Admin.
    - Cross-tenant `ApiKey` administration is deferred to Phase 16's
      audited privileged path.
    - Verified by `admin.site.is_registered(ApiKey) is False`.
15. **D-B: Preserve `WWW-Authenticate`** (approved, final).
    - The contract for invalid API-key authentication is `401` +
      `WWW-Authenticate: Bearer` + `error.code = "invalid_api_key"`.
    - DRF builds the header, but `core/api.py` copies only
      `Retry-After` and `Allow` into the standard error response and
      would drop it.
    - `core/api.py` must preserve it. The implementation is only the
      one-line change of the header passthrough tuple to
      `("Retry-After", "Allow", "WWW-Authenticate")`.
    - Session-authentication behavior is unchanged.
      `SessionAuthentication` sends no `WWW-Authenticate`, so session
      failures stay `403` with the same body and headers.

## Rules for implementation
- Django + DRF monolith. Business logic lives only in
  `apikeys/services.py`, never in views, serializers, middleware or
  auth classes. Those call services.
- `ApiKey` uses `ApiKeyManager(TenantScopedManager)`. RLS is enabled
  and forced on `apikeys_apikey`. `for_lookup_hash` is the only
  unscoped read and works only inside `api_key_lookup_atomic` for that
  same hash.
- `api_key_lookup_atomic`:
  - It never runs inside a merchant context.
  - It is always `durable=True`.
  - It sets the hash only via `SET LOCAL`, never session-level.
  - No write policy is ever keyed on `app.current_api_key_hash`.
- Never trust a client-supplied `merchant_id`/`location_id`. For key
  requests the merchant comes only from `ApiKey.merchant_id`.
- API-key acceptance is opt-in only via `ApiKeyOptInMixin` +
  `api_key_methods` on the resolved view class. It is never inferred
  from paths, URL strings or naming, and never global. Only
  `MerchantView` `GET` opts in this phase.
- A Bearer request is authenticated by the key alone. A session cookie
  on the same request is ignored, and an invalid key never falls back
  to the session.
- Never log, return (after the create response), audit or store the
  plaintext key. Never expose `key_hash` in the API, admin, logs or
  audit metadata.
- Idempotency via DB constraints and conditional updates:
  `UNIQUE(key_hash)`; revoke is a conditional `UPDATE`, not
  check-then-set.
- Role checks via DRF permission classes (OWNER/ADMIN for management).
  Scope checks via `HasApiKeyScope`, never inline `if` checks.
- External IDs are UUIDs (`BaseModel`).
- Status enums `UPPER_SNAKE_CASE`, timestamps `_at` (`last_used_at`).
- No V2 features, no bespoke admin app, no broker-side `eta`
  scheduling. There are no Celery tasks in this phase.
- Every new service function has a unit test. There is no adapter or
  webhook in this phase.
- Throttle tests use the locmem cache fixture so counters never leak
  through Redis between tests.

## Definition of done
All verifiable with `pytest` (real PostgreSQL) unless noted.

**Model, migrations, RLS**
- [ ] `python manage.py migrate` applies `apikeys` 0001 + 0002 and
      they reverse cleanly (`migrate apikeys zero`).
- [ ] `apikeys_apikey` has RLS ENABLED + FORCED with exactly two
      policies: `tenant_isolation` and the SELECT-only `api_key_lookup`
      (checked via `pg_policies`).
- [ ] Inside `tenant_atomic` for merchant A, a raw `SELECT` on
      `apikeys_apikey` returns only A's keys. An `INSERT` with
      B's `merchant_id` fails `WITH CHECK`.
- [ ] Inside `api_key_lookup_atomic(h)`, a raw `SELECT *` returns only
      the row whose `key_hash = h` and no other merchant's rows. A raw
      `UPDATE`/`DELETE` affects 0 rows.
- [ ] With no context set, a raw `SELECT` returns 0 rows.
- [ ] `api_key_lookup_atomic` raises `TenantContextError` inside an
      active `tenant_context`. It raises `ValueError` for a non-hex or
      wrong-length hash. It raises `RuntimeError` when nested in
      another `atomic()` (durable).
- [ ] `ApiKey.objects.for_lookup_hash(h)` raises `TenantContextError`
      outside `api_key_lookup_atomic` or for a different hash.
      `ApiKey.objects.all()` without a tenant context raises.
- [ ] Inserting two rows with the same `key_hash` raises
      `IntegrityError`.

**Services**
- [ ] `create_api_key` returns a plaintext matching
      `^rf_live_[A-Za-z0-9_-]{32}$`. The stored `key_hash` equals
      `sha256(plaintext)`, and the plaintext appears in no DB column.
      It writes one `api_key.created` `AuditLog` row whose metadata
      holds neither the plaintext nor the hash.
- [ ] `create_api_key` rejects an empty list, an unknown scope and
      duplicate scopes (`InvalidScopes`).
- [ ] `revoke_api_key` sets `is_active=False` with one
      `api_key.revoked` audit row. A second revoke is a no-op with no
      second audit row.
- [ ] `authenticate_api_key` returns the key for a valid active key.
      It raises `InvalidApiKey` for: a malformed string, an unknown
      key, a revoked key, and a key whose merchant is `SUSPENDED`.
- [ ] `touch_last_used` sets `last_used_at` when null, does not change
      it within 60 s, and updates it after 60 s.

**Public-API auth (`GET /merchant`)**
- [ ] A valid key returns `200` with that key's merchant. There is no
      session cookie and no CSRF.
- [ ] Malformed (including an empty `Bearer ` value), unknown and
      revoked keys each return `401` with `WWW-Authenticate: Bearer`
      and the identical body `{error: {code: "invalid_api_key", ...}}`.
- [ ] No `Authorization` header and no session still returns `403`
      (existing `test_unauthenticated_merchant_get_returns_403_with_error_shape`
      passes unchanged).
- [ ] A revoked key returns `401` on the very next request after
      `DELETE /api-keys/{id}`.
- [ ] A key belonging to a `SUSPENDED` merchant returns `401`.
- [ ] **Cross-tenant (Testing-Strategy priority scenario):**
  - Merchant A's key on `GET /merchant` returns A only.
  - A request carrying A's key **and** a logged-in session cookie for
    merchant B returns A: Bearer wins and there is no mixing.
  - A's key never yields B's data on any endpoint.
- [ ] A bad Bearer key with a valid session cookie returns `401`
      (no fallback to the session).
- [ ] Session behavior of `GET /merchant` and `PATCH /merchant` is
      unchanged. `PATCH /merchant` with a valid key and no session
      returns `403` (not opted in, so the session path applies) and
      changes nothing.
- [ ] A Bearer header on a dashboard-only endpoint (e.g.
      `GET /locations`, `GET /api-keys`) with no session returns `403`,
      never data, and the key's `last_used_at` is not touched (the
      middleware did not act).
- [ ] A valid Bearer key plus a valid session on a session-only
      endpoint (e.g. `GET /locations`) returns the **session** merchant's
      data. The key is ignored and never switches the tenant.
- [ ] A Bearer header on `POST /auth/login` does not break login
      (no 500): the pre-tenant path is not tenant-wrapped.
- [ ] **Opt-in registry test:** iterating every resolvable URL pattern,
      the set of `(view class, HTTP method)` pairs whose view has a
      non-empty `api_key_methods` is exactly `{(MerchantView, "GET")}`.
      `ApiKeyAuthentication` is not in
      `REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]`.
- [ ] `HEAD /merchant` with a Bearer key is not API-key authenticated
      (unlisted method), so it gets the session path.
- [ ] `last_used_at` is set after a successful key request.
- [ ] **Best-effort touch:** with `touch_last_used`'s update forced to
      raise `DatabaseError`, a valid key on `GET /merchant` still
      returns `200` with the key's merchant. The same forced failure
      does not make an invalid/revoked key succeed (still `401`).
- [ ] **Suspension / revocation are point-in-time:**
  - A request that authenticated, and whose merchant is then suspended
    or whose key is then revoked during the view, completes normally.
    Test it by patching the view to suspend or revoke mid-request.
  - The next request with that key returns the generic
    `invalid_api_key` `401`.
- [ ] `HasApiKeyScope("sales:write")` passes for a key with that scope
      and denies a key without it and a session principal (unit test
      with a DRF test view).

**Key management API**
- [ ] `POST /api-keys` as OWNER and ADMIN returns `201` with `key`.
      As MANAGER and VIEWER it returns `403`. Without CSRF it returns
      `403`. With a valid API key and no session it returns `403`
      (session-only, keys cannot mint keys).
- [ ] `POST /api-keys` ignores `merchant_id`/`is_active`/`key_hash` in
      the body. An unknown scope returns `422` in the standard error
      shape.
- [ ] `GET /api-keys` never includes `key` or `key_hash`, is
      cursor-paginated (`limit` capped at 100), and lists only the
      caller's merchant's keys.
- [ ] `DELETE /api-keys/{id}` returns `204` and is idempotent. Another
      merchant's key id returns `404`, never `403`. MANAGER and VIEWER
      get `403`.

**Rate limiting**
- [ ] With a low `api_key` rate override, the (N+1)th request with one
      key returns `429` with `Retry-After`. A second key from the same
      merchant is unaffected (independent counters).
- [ ] With a low `api_key_ip` rate override, repeated **invalid-key**
      requests from one IP hit `429` with `Retry-After` in the standard
      error shape (the throttle runs before the lookup).
- [ ] A rotating `X-Forwarded-For` does not create fresh per-IP
      buckets (the `NUM_PROXIES` behavior, as in the Phase 02 login
      throttle test).

**Admin / error handler / docs**
- [ ] **D-A:** `admin.site.is_registered(ApiKey) is False`, and there
      is no `apikeys/admin.py`.
- [ ] **D-B:** an invalid key on `GET /merchant` returns `401` with the
      `WWW-Authenticate: Bearer` header present in the final response
      and `error.code == "invalid_api_key"`. The existing `core`
      error-handler and session-auth tests pass unchanged: session
      failures stay `403` with no `WWW-Authenticate` header.
- [ ] `API-Specification.md` has the "API Keys" section,
      `Multi-Tenancy.md` has the `api_key_lookup` subsection, and
      `Authentication.md` §2 matches. These were written during spec
      clarification; the implementation must match them.
- [ ] Full `pytest` is green. No test is skipped or xfailed.
