# Spec: Merchant Account Foundation

## Overview
Creates the identity and tenant root of ReviewFlow. It adds the
`accounts` app (a custom email-based `User`, `Merchant`, `TeamMember`
with roles OWNER / ADMIN / MANAGER / VIEWER). It installs DRF with the
standard error shape and adds role permission classes. It adds dashboard
session login, logout and refresh, which resolve the logged-in user's
merchant and set `request.merchant_id`. That is the hook Phase 01's
`TenantMiddleware` has been waiting for. It also adds `GET` and
`PATCH /merchant`, and the `auditlog` app (the `AuditLog` model, RLS and
a `record()` service). Every later tenant-owned model hangs off
`Merchant`, and every dashboard endpoint depends on this session to
merchant to tenant-context chain. This work is shared foundation for all
three planes (ingestion, automation, intelligence). The team-management
endpoints and TOTP 2FA come in a follow-up Phase 02 spec.

## Source docs
- `docs/ROADMAP.md`: §"02 — Accounts, roles & audit log"
- `docs/04-api/Authentication.md`: §1 (Dashboard Users), §"Session vs.
  API Key"
- `docs/04-api/API-Specification.md`: preamble (base path `/api/v1/`),
  §Auth (`/auth/login`, `/auth/logout`, `/auth/refresh`), §Merchant
  (`GET`/`PATCH /merchant`), §"General Conventions" (error shape, UUIDs,
  rate limits)
- `docs/02-architecture/Security-Architecture.md`: §Authentication
  (Dashboard Users), §Authorization (roles), §"Threat Model Highlights"
  (IDOR)
- `docs/02-architecture/Multi-Tenancy.md`: §"Isolation Layers" (Layer 1,
  Layer 2), §"No Standing Privileged Role", §"Data Model Note", §"Required
  Tests" (1–3)
- `docs/FINAL-ARCHITECTURE-REVIEW.md`: §8 (RLS)
- `docs/03-database/Data-Dictionary.md`: preamble, §Merchant, §User,
  §TeamMember, §AuditLog
- `docs/03-database/Database-Design.md`: Legend, §Merchant, §User,
  §TeamMember, §AuditLog, §"Design Principles Applied Throughout"
  (RESTRICT on audit tables)
- `docs/09-security/Audit-Logging.md`: all sections
- `docs/09-security/Security-Controls.md`: §"Permissions & Access",
  §"Identifiers", §"Row-Level Security", §"Privileged Admin Operations"
- `docs/01-product/User-Flows.md`: §1 step 1 (sign up creates a
  `Merchant`)
- `docs/02-architecture/SAD.md`: §3 (`accounts`, `auditlog`, `core`)
- `docs/10-development/Coding-Standards.md`: §1, §2, §5, §6
- `docs/10-development/Testing-Strategy.md`: §"Tenant Isolation",
  Permission tests, API tests, Security tests

## Depends on
- Phase 00, Project scaffold (Done)
- Phase 01, Core tenancy & RLS (Done). This spec uses `core.BaseModel`,
  `TenantScopedManager`, `tenant_context`, `tenant_atomic`,
  `TenantMiddleware`, `rls_direct` and the `reviewflow_app` non-superuser
  role.

## Roadmap Phase
- Phase: 02 — Accounts, roles & audit log
- Completes entire phase: No
- If No, remaining phase work (the next Phase 02 spec):
  `/team-members` endpoints (`GET`, `POST` invite, `PATCH` role change,
  `DELETE` revoke; OWNER/ADMIN only), each writing an `AuditLog` row. The
  "AuditLog middleware" roadmap bullet is decided there, together with
  its first callers. Optional TOTP 2FA (needs a new package). The invite
  acceptance flow and invite delivery channel are not specified in
  `docs/`, so that spec must settle them with the user first.

## Locked decisions touched
- Shared schema, `merchant_id` scoping, `Merchant` is the GLOBAL tenant
  root (`Multi-Tenancy.md` §Model, §Data Model Note) — DEPENDS ON
- `TenantScopedManager` as primary application-layer scoping
  (`Multi-Tenancy.md` §Layer 1, `Coding-Standards.md` §2) — DEPENDS ON
- Tenant context set per request by middleware after auth resolves the
  merchant (`Multi-Tenancy.md` §Layer 1) — DEPENDS ON (this spec supplies
  the session → merchant resolution)
- Transaction-local `SET LOCAL app.current_merchant_id` and RLS on every
  tenant table (`FINAL-ARCHITECTURE-REVIEW.md` §8) — DEPENDS ON
- RLS policy shape: direct tables compare only `merchant_id`
  (`FINAL-ARCHITECTURE-REVIEW.md` §8, `Multi-Tenancy.md` §Layer 2) —
  LOCKED DECISION CHANGE
- No standing privileged role / no `BYPASSRLS` (`Multi-Tenancy.md` §No
  Standing Privileged Role) — NO CHANGE
- Three authentication mechanisms, never mixed; dashboard = session +
  CSRF (`Authentication.md`) — DEPENDS ON
- Roles OWNER/ADMIN/MANAGER/VIEWER enforced by DRF permission classes
  (`Security-Architecture.md` §Authorization) — DEPENDS ON
- `TeamMemberLocation` explicit through-model (`Multi-Tenancy.md`) — NO
  CHANGE (it is Phase 03 and is not built here)
- Externally exposed IDs are UUIDs (`Security-Architecture.md` §Threat
  Model) — DEPENDS ON
- Audit/financial tables use `RESTRICT` from `Merchant`
  (`Database-Design.md` §Design Principles) — DEPENDS ON

LOCKED DECISION CHANGE — USER SIGN-OFF REQUIRED

**What would change:** `accounts_teammember` gets a second RLS policy
alongside the standard `tenant_isolation` policy. The second policy is
`self_membership`, SELECT-only:
`USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)`.
`app.current_user_id` is set only by `SET LOCAL`, inside a dedicated
login-lookup transaction.

**Why:** at login no merchant is known yet. To find the merchant, the
code must read the user's `TeamMember` rows, and `tenant_isolation`
hides every row when no merchant context is set. Without this policy,
login cannot resolve a merchant. The user chose this option (over
dropping RLS on `TeamMember`) when the spec was scoped on 2026-09-19.
Formal sign-off is still required before merge.

**Why it stays inside the spirit of §8:**
- The value is transaction-local and fails closed: with no value set,
  no rows match.
- The policy is `FOR SELECT` only, so every write still needs
  `tenant_isolation` `USING`/`WITH CHECK`.
- It exposes only the authenticated user's own membership rows. No
  role or connection bypasses RLS.

The change must be documented in `Multi-Tenancy.md` §Layer 2 and
`Database-Design.md` §TeamMember.

## Django apps
- `accounts`: **created**. Contains `User`, `Merchant`, `TeamMember`,
  services, permissions, session → merchant middleware, auth and
  merchant views.
- `auditlog`: **created**. Contains `AuditLog` and `record()`.
- `core`: **touched**. Adds the user-lookup RLS primitive and helper,
  and the DRF exception handler.
- `config`: **touched**. Settings (DRF, `AUTH_USER_MODEL`, cache,
  cookies, middleware) and URLs.

## Models & database changes

### `accounts.User` (GLOBAL: no `merchant_id`, no RLS, plain manager)
Inherits `core.BaseModel` + `AbstractBaseUser` + `PermissionsMixin`.
`AUTH_USER_MODEL = "accounts.User"`.
- `id` UUID pk, `created_at`, `updated_at` (from `BaseModel`)
- `email`: `EmailField`, unique, `USERNAME_FIELD`. `REQUIRED_FIELDS = []`.
  Normalized to lowercase by the manager and before save, so uniqueness
  is case-insensitive.
- `password`: Django's built-in hashed field. It is the dictionary's
  `password_hash`. Do not add a literal `password_hash` column.
- `is_active`: bool, default `True`
- `is_staff`: bool, default `False`. This is not in the dictionary, but
  Django Admin staff-only access (`Security-Controls.md`) needs it.
  `PermissionsMixin` adds `is_superuser`, `groups` and
  `user_permissions`, which Django Admin uses.
- `last_login`: from `AbstractBaseUser`
- `UserManager`: `create_user(email, password=None, **extra)` and
  `create_superuser(...)`, so `createsuperuser` works

### `accounts.Merchant` (GLOBAL: it is the tenant root, no RLS, plain manager)
Inherits `core.BaseModel`.
- `name`: `CharField(max_length=255)`
- `business_type`: `CharField(max_length=100, null=True, blank=True)`
- `timezone`: `CharField(max_length=64)`, a valid IANA name (validated
  against `zoneinfo.available_timezones()` in services)
- `status`: `CharField` choices `ACTIVE`, `SUSPENDED`, `DELETED`; default
  `ACTIVE`. Soft-delete only; never hard-deleted.
- **`plan_id` is omitted.** `Plan` is Phase 07, which adds the FK. Do
  not stub a `Plan` model.

### `accounts.TeamMember` (MERCHANT, direct)
Inherits `core.BaseModel`. `objects = TeamMemberManager()`, a subclass of
`TenantScopedManager`.
- `merchant`: FK → `Merchant`, `on_delete=PROTECT`
- `user`: FK → `User`, `on_delete=PROTECT`, `related_name="memberships"`
- `role`: `CharField` choices `OWNER`, `ADMIN`, `MANAGER`, `VIEWER`
- `invited_at`, `accepted_at`: nullable `DateTimeField`. Only rows with
  `accepted_at IS NOT NULL` grant access.
- **Unique constraint** `UniqueConstraint(fields=["merchant", "user"],
  name="uniq_teammember_merchant_user")` (`Database-Design.md`)
- **RLS**:
  - `rls_direct("accounts_teammember")`, the standard `tenant_isolation`
    policy
  - `rls_select_by_user("accounts_teammember")`, the `self_membership`
    policy (see Locked decisions)
- `TeamMemberManager.for_lookup_user(user_id)`: the only unscoped-by-
  merchant path. It raises `TenantContextError` unless
  `get_current_lookup_user_id() == user_id` (the contextvar set by
  `user_lookup_atomic`). It then returns
  `models.Manager.get_queryset(self).filter(user_id=user_id)`. The
  application layer therefore mirrors the RLS policy exactly and still
  fails closed.

### `auditlog.AuditLog` (MERCHANT, direct; nullable merchant)
Never updated, so it does **not** inherit `BaseModel`'s `updated_at`. It
declares `id` (UUID pk) and `created_at` itself.
`objects = TenantScopedManager()`.
- `merchant`: FK → `Merchant`, `null=True`, `on_delete=RESTRICT`
- `actor_user`: FK → `User`, `null=True`, `on_delete=RESTRICT`
- `action`: `CharField(max_length=100)`, a dotted verb such as
  `team_member.invited`
- `target_type`: `CharField(max_length=100, null=True)`. Model label, for
  example `accounts.teammember`.
- `target_id`: `CharField(max_length=64, null=True)`
- `metadata_json`: `JSONField(null=True)`. Never holds phone numbers,
  tokens, message bodies or raw payloads.
- Index on `(merchant, created_at)`
- **RLS**: `rls_direct("auditlog_auditlog")`. Rows with `merchant_id
  NULL` (platform-level actions) are invisible in any tenant context, and
  `WITH CHECK` rejects them. Platform-level rows are written only via the
  Phase 16 audited privileged path, which this spec does not build.

### Migrations
One logical change each:
- `accounts/0001_initial`: `User`, `Merchant`, `TeamMember` plus the
  unique constraint
- `accounts/0002_teammember_rls`: `rls_direct` and `rls_select_by_user`
- `auditlog/0001_initial`: the model
- `auditlog/0002_auditlog_rls`: `rls_direct`

**Custom user model swap.** `AUTH_USER_MODEL` cannot change on a database
where `auth` has already migrated. Every local dev database from Phase
00/01 must be reset: recreate the Postgres container as in Phase 01, or
drop and recreate `reviewflow` owned by `reviewflow_app`, then run
`migrate`. No production database exists, so no real data is lost. The
PR description must still note the reset step, per the
destructive-migration rule.

**`/ship-feature`: copy this verbatim into the PR's `## Migrations`
section.**
> ⚠ Backup / reset step (Coding-Standards §6): this PR switches
> `AUTH_USER_MODEL` to `accounts.User`, which cannot be applied to a
> database where `auth` has already migrated. No production database
> exists. Every local development database created before this PR must
> be reset once:
> - either run `docker compose exec postgres psql -U reviewflow -d postgres -c "DROP DATABASE reviewflow;" -c "CREATE DATABASE reviewflow OWNER reviewflow_app;"`
> - or run `docker compose down` and then `docker compose up -d`
>
> Then run `python manage.py migrate`. Back up anything you need from
> the local database first; the reset deletes all local data.

## API endpoints
All endpoints are under `/api/v1/` and use session auth + CSRF.
`merchant_id` always comes from the session and never from the request.
- `GET /auth/login`: sets the `csrftoken` cookie (`ensure_csrf_cookie`)
  and returns `204`. Public. This is a small addition so the Next.js
  frontend can obtain a token before its first POST. Record it in
  `API-Specification.md` §Auth.
- `POST /auth/login`: `{ email, password }`. Returns
  `200 { user: {id, email}, merchant: {id, name}, role }` and sets the
  session cookie. Public, with CSRF enforced (`csrf_protect`).
  - `401` with the same generic body for all of these: bad credentials,
    inactive user, no accepted membership in an `ACTIVE` merchant. No
    enumeration.
  - `429` from a per-IP `login` throttle
  - The view must return 401 explicitly. Under session auth, DRF
    otherwise downgrades `AuthenticationFailed` to 403.
- `POST /auth/logout`: returns `204` and flushes the session. Session
  required. Any role.
- `POST /auth/refresh`: returns `200` with the same body as login and
  extends the session expiry. Session required. Any role.
  - It reads the membership via `get_active_membership` (the tenant
    context that `TenantMiddleware` already opened).
  - It never calls `resolve_login_membership` or `user_lookup_atomic`.
- `GET /merchant`: returns
  `200 { id, name, business_type, timezone, plan: null, status }`.
  `plan` stays `null` until Phase 07. Session auth. Any role. API-key
  access arrives in Phase 05.
- `PATCH /merchant`: partial `{ name?, business_type?, timezone? }`.
  Returns `200` with the merchant body. `422` for an invalid IANA
  timezone or other validation error, `403` for an insufficient role.
  Session auth. OWNER, ADMIN. Any `merchant_id`, `status` or `plan` in
  the body is ignored or rejected, never applied.

Errors always use `{ "error": { "code", "message", "field_errors"? } }`.

## Services & background tasks

### `core/tenancy.py` (additions)
- `get_current_lookup_user_id() -> uuid.UUID | None`
- `user_lookup_atomic(user_id)`: a context manager. It normalizes to a
  UUID (`ValueError` on bad input) and sets a lookup-user contextvar. It
  opens `transaction.atomic()` and runs
  `SET LOCAL app.current_user_id = '<uuid>'` as its first statement, and
  resets the contextvar on exit. It is transaction-local only, like
  `tenant_atomic`. Used only by login membership resolution.
  - It raises `TenantContextError` if `get_current_merchant_id()` is
    not `None`.
  - Why: both policies are PERMISSIVE, so SELECT combines them with
    OR. If `user_lookup_atomic` ran inside a `tenant_atomic`
    transaction, it would become a savepoint and set both values in one
    transaction. Merchant A's context would then also see the user's
    memberships in merchants B and C.

### `core/rls.py` (addition)
- `rls_select_by_user(table, user_column="user_id")`: a `RunSQL` that
  creates policy `self_membership ON <table> FOR SELECT USING
  (<user_column> = NULLIF(current_setting('app.current_user_id', true),
  '')::uuid)`. Its reverse is `DROP POLICY self_membership ON <table>`.
  RLS is already enabled and forced by `rls_direct`, which must run
  first.

### `core/api.py`
- `exception_handler(exc, context)`: the DRF `EXCEPTION_HANDLER`. It
  produces the standard error shape.
  - DRF `ValidationError` and Django `ValidationError` →
    `422 validation_error` with `field_errors`
  - `NotAuthenticated` / `PermissionDenied` → `403`
  - `AuthenticationFailed` → `401`
  - `Throttled` → `429`
  - `NotFound` → `404`
  - Unhandled exceptions still return 500. `TenantContextError` is never
    turned into a success.

### `accounts/services.py`
- `create_merchant_with_owner(*, name, timezone, owner_email,
  owner_password, business_type=None) -> TeamMember`
  - Pre-generates the merchant UUID, then inside `tenant_context` +
    `tenant_atomic` creates the `User`, the `Merchant` and an `OWNER`
    `TeamMember` with `accepted_at=now`. It is one transaction.
  - Raises `django.core.exceptions.ValidationError` for an invalid
    timezone, a password rejected by `validate_password`, or an email
    that is already registered.
  - Used by tests and by the Phase 03 seed command. There is **no
    signup endpoint**, because `API-Specification.md` defines none
    (User-Flows §1 does). Flag this for a future docs decision; do not
    invent an endpoint.
- `authenticate_login(*, request, email, password) -> TeamMember`
  - Calls `django.contrib.auth.authenticate`, which keeps constant-time
    behaviour for unknown emails, then `resolve_login_membership(user)`.
  - Raises `InvalidCredentials` (a `ReviewFlowError` subclass) for any
    failure.
- `resolve_login_membership(user) -> TeamMember | None`
  - Inside `user_lookup_atomic(user.id)`, uses
    `TeamMember.objects.for_lookup_user(user.id)` filtered to
    `accepted_at` not null and `merchant__status=ACTIVE`, ordered by
    `created_at`, and returns the first row.
  - `# ponytail:` a multi-merchant user lands on their oldest
    membership. Add a merchant switch when the API specifies one.
- `get_active_membership(user) -> TeamMember | None`: requires a tenant
  context. Returns the user's accepted membership in the current
  merchant, or `None` if that merchant is not `ACTIVE`.
- `update_merchant(merchant, *, name=None, business_type=None,
  timezone=None) -> Merchant`: validates the timezone and saves only the
  given fields. Raises `ValidationError`.

### `auditlog/services.py`
- `record(action, *, actor=None, target=None, metadata=None) -> AuditLog`
  - `merchant_id` comes from `get_current_merchant_id()`; it raises
    `TenantContextError` if none is set. It never accepts a merchant
    argument.
  - `target` gives `target_type` (`_meta.label_lower`) and
    `target_id` (`str(pk)`).
  - There are no update or delete services. Audit rows are immutable in
    application code.

### Middleware
`accounts/middleware.py` → `SessionMerchantMiddleware`: if
`request.user.is_authenticated` and `request.session["merchant_id"]`
exists, it sets `request.merchant_id`. Placed after
`AuthenticationMiddleware` and before `core.middleware.TenantMiddleware`.
Membership and role are re-checked on every request by the permission
classes, so a revoked member or a non-`ACTIVE` merchant loses access
immediately.

### Permissions (`accounts/permissions.py`)
- `IsMerchantMember`: the request is authenticated, has
  `request.merchant_id`, and `get_active_membership` returns a row. The
  row is cached as `request.team_member`. This is the DRF default
  permission, so an endpoint fails closed if it forgets to declare one.
- `HasRole` base class, with `roles` as a class attribute, plus
  `IsOwner`, `IsOwnerOrAdmin`.
- MANAGER location scoping is Phase 03.

### Tasks, adapters and schedules
- No Celery tasks
- No Beat schedules
- No adapters or providers

## Admin
- `accounts.User`: registered with a `UserAdmin` subclass (email as
  identifier, no username), so staff can log in and manage staff users.
- `accounts.Merchant`: registered **read-only** (no add, change or
  delete). Suspension and reactivation are audited platform actions that
  belong to Phase 16.
- `TeamMember` and `AuditLog` are **not** registered.
  `TenantScopedManager` raises without a tenant context, so cross-tenant
  admin needs the Phase 16 audited privileged path.

## Files to change
- `config/settings.py`
  - Add `"rest_framework"`, `"accounts"` and `"auditlog"` to
    `INSTALLED_APPS`.
  - Set `AUTH_USER_MODEL = "accounts.User"`.
  - Insert `accounts.middleware.SessionMerchantMiddleware` before
    `core.middleware.TenantMiddleware`.
  - Add `REST_FRAMEWORK`:
    - `SessionAuthentication` only
    - default permission `accounts.permissions.IsMerchantMember`
    - `EXCEPTION_HANDLER = "core.api.exception_handler"`
    - throttle rate `login` (for example `5/min`)
  - Add `CACHES` using Django's built-in `RedisCache` on `REDIS_URL`
    (rate-limit counters only). Tests may override it to local memory.
  - Set `SESSION_COOKIE_SECURE` and `CSRF_COOKIE_SECURE` from env,
    default `True`.
- `config/urls.py`: include `accounts.urls` under `api/v1/`
- `core/tenancy.py`: `user_lookup_atomic`, `get_current_lookup_user_id`
- `core/rls.py`: `rls_select_by_user`
- `requirements.txt`: add pinned `djangorestframework`
- `.env.example`: `DJANGO_SECURE_COOKIES=False` for local development
- `docs/02-architecture/Multi-Tenancy.md` §Layer 2 and
  `docs/03-database/Database-Design.md` §TeamMember: document the
  `self_membership` policy. **Only after sign-off.**
- `docs/04-api/API-Specification.md` §Auth: add `GET /auth/login` (the
  CSRF cookie) and the login response body
- `docs/10-development/Development-Setup.md`: note the one-time local
  database reset for the custom user model

## Files to create
- `accounts/__init__.py`, `accounts/apps.py`
- `accounts/models.py`: `User`, `UserManager`, `Merchant`, `TeamMember`,
  `TeamMemberManager`, role and status choices
- `accounts/migrations/0001_initial.py`,
  `accounts/migrations/0002_teammember_rls.py`
- `accounts/services.py`, `accounts/exceptions.py` (`InvalidCredentials`)
- `accounts/permissions.py`, `accounts/middleware.py`
- `accounts/serializers.py`, `accounts/views.py`, `accounts/urls.py`
- `accounts/admin.py`
- `accounts/tests/__init__.py`, plus tests written by `/test-feature`:
  - `test_models.py`
  - `test_services.py`
  - `test_rls.py`
  - `test_auth_api.py`
  - `test_merchant_api.py`
  - `test_permissions.py`
- `auditlog/__init__.py`, `auditlog/apps.py`, `auditlog/models.py`,
  `auditlog/services.py`
- `auditlog/migrations/0001_initial.py`,
  `auditlog/migrations/0002_auditlog_rls.py`
- `auditlog/tests/__init__.py`, plus `test_services.py` and
  `test_rls.py` (written by `/test-feature`)
- `core/api.py`: the DRF exception handler

## New dependencies
- `djangorestframework`: pinned to the current stable release in
  `requirements.txt`. The stack is "Django + Django REST Framework"
  (CLAUDE.md) but it is not installed yet. **Flag this to the user.**
- No other package. Redis caching uses Django's built-in `RedisCache`
  with the `redis` client that `celery[redis]` already installs. IANA
  validation uses stdlib `zoneinfo` (Django already pulls in `tzdata` on
  Windows). TOTP is deferred to the next spec.

## Rules for implementation
- Django + DRF monolith; business logic only in `services.py`, never in
  views/serializers
- Every tenant-owned model uses `core.TenantScopedManager`; RLS enabled
  on its table
- Never trust client-supplied `merchant_id`/`location_id` — derive from
  the authenticated principal
- Celery tasks take `merchant_id` explicitly and set tenant context first
- Idempotency via database unique constraints, not check-then-insert
- Role checks via DRF permission classes (OWNER/ADMIN/MANAGER/VIEWER)
- External IDs are UUIDs/hashids, never sequential integers
- Secrets/OAuth tokens encrypted (Fernet), never hardcoded or committed
- Status enums in `UPPER_SNAKE_CASE`; timestamps suffixed `_at`
- No V2 features, no bespoke admin app, no broker-side `eta` scheduling
- Every new service function has a unit test; every adapter/webhook has
  a fixture-based contract test

Feature-specific rules:
- **Do not build these (Phase 03):** `TeamMemberLocation`,
  `PUT /team-members/{id}/locations`, and the locations field of
  `GET /team-members`.
- **Do not build these (Phase 07):** `Merchant.plan` and a `Plan` stub.
- **Do not build these (next Phase 02 spec):** `/team-members` and TOTP.
- **Do not build these (Phase 05):** API-key auth.
- **Do not build these (Phase 16):** platform-level audit rows.
- `User` and `Merchant` are GLOBAL. They have no `merchant_id`, no RLS
  and a plain manager. This is intentional (`Database-Design.md`
  Legend), not a missed RLS table.
- `self_membership` is `FOR SELECT` only. Never add a write policy keyed
  on `app.current_user_id`. Never set `app.current_user_id` outside
  `user_lookup_atomic`, and never set it at session level.
- `TeamMemberManager.for_lookup_user` is the only merchant-unscoped
  read of `TeamMember`. It must check the lookup-user contextvar.
  Nothing else may bypass `TenantScopedManager` with `.objects.all()`,
  raw SQL or `_base_manager` on tenant tables.
- `GET` and `POST /auth/login` explicitly set
  `permission_classes = [AllowAny]`. They are the only public views.
  Never weaken the `IsMerchantMember` default to make them work.
- Login returns one identical `401` for every failure reason, with no
  user enumeration. Never log the email, password or session key.
- CSRF is enforced on `POST /auth/login`, `POST /auth/logout`,
  `POST /auth/refresh` and `PATCH /merchant`. Tests use
  `APIClient(enforce_csrf_checks=True)`.
- Login calls `django.contrib.auth.login()`, which cycles the session
  key and so prevents session fixation. `merchant_id` goes into the
  server-side session only.
- `AuditLog` has no update or delete code path. Its admin is not
  registered in this spec.
- Merchant soft-delete only: nothing hard-deletes a `Merchant`, and the
  FKs to it are `PROTECT` or `RESTRICT`.

## Definition of done
- [ ] `python manage.py check` is clean.
- [ ] `makemigrations --check --dry-run` reports no changes.
- [ ] `migrate` succeeds as `reviewflow_app` on a reset database.
- [ ] `createsuperuser` works with email only.
- [ ] `pytest` passes, including the Phase 00 and 01 tests (the
      connected role is still not superuser or `BYPASSRLS`).
- [ ] `User`: email is unique case-insensitively (`A@x.com` and `a@x.com`
      conflict).
- [ ] `User`: the password is stored hashed, and `id` is a UUID.
- [ ] `create_merchant_with_owner` creates `User`, `Merchant` and an
      `OWNER` `TeamMember` with `accepted_at` set, atomically.
- [ ] `create_merchant_with_owner` raises `ValidationError` for an
      invalid timezone, a weak password or a duplicate email, and
      leaves no partial rows behind.
- [ ] `UNIQUE(merchant_id, user_id)` on `TeamMember` rejects a second
      membership at the database level.
- [ ] **Tenant isolation, RLS backstop** (Multi-Tenancy Required
      Test 2): raw SQL on `accounts_teammember` and `auditlog_auditlog`
      in merchant A's `tenant_atomic` returns only A's rows, and returns
      zero rows with no context set.
- [ ] RLS: inserting a row with merchant B's id in A's context fails
      `WITH CHECK`.
- [ ] **`self_membership` policy**, inside `user_lookup_atomic(U)`:
  - [ ] Raw SQL returns exactly U's `TeamMember` rows across merchants
        and no other user's rows.
  - [ ] INSERT or UPDATE of `accounts_teammember` fails or affects zero
        rows.
  - [ ] After the transaction ends, the same connection's
        `current_setting('app.current_user_id', true)` is empty.
- [ ] Nesting guard: `user_lookup_atomic` inside an active
      `tenant_context` (including inside `tenant_atomic`) raises
      `TenantContextError`, and no `SET LOCAL app.current_user_id` is
      issued. With PERMISSIVE policies OR-combined, this guard is what
      stops merchant A's transaction seeing U's merchant-B membership.
- [ ] `POST /auth/refresh` under merchant A's session, for a user with
      memberships in A and B, succeeds and never issues
      `SET LOCAL app.current_user_id`.
- [ ] `for_lookup_user` raises `TenantContextError` outside
      `user_lookup_atomic` or for a different user id.
- [ ] `TenantScopedManager` on `TeamMember` and `AuditLog` raises with no
      context, and never returns another merchant's rows (Required
      Test 1, application layer).
- [ ] **Login**:
  - [ ] Valid credentials return `200` with user, merchant and role, and
        set the session.
  - [ ] Wrong password, unknown email, inactive user, no membership,
        unaccepted membership, and `SUSPENDED`/`DELETED` merchant all
        return the same `401` body.
  - [ ] The 6th rapid attempt from one IP returns `429`.
  - [ ] Missing CSRF token returns `403`.
  - [ ] The session key changes on login.
- [ ] Logout returns `204`, after which `GET /merchant` returns `403`.
- [ ] Refresh returns `200` with the same body.
- [ ] Unauthenticated logout or refresh returns `403`.
- [ ] **Tenant isolation, API** (Testing-Strategy §Tenant Isolation):
  - [ ] Merchant A's session `GET /merchant` returns only A.
  - [ ] `PATCH /merchant` with `merchant_id`, `id` or `status` of B in
        the body never changes B, and never changes A's `status`.
- [ ] **Permissions**:
  - [ ] `PATCH /merchant` succeeds for OWNER and ADMIN.
  - [ ] `PATCH /merchant` returns `403` for MANAGER and VIEWER.
  - [ ] `GET /merchant` succeeds for all four roles.
  - [ ] A session whose membership is deleted mid-session gets `403` on
        the next request.
- [ ] `PATCH /merchant` with `timezone="Mars/Olympus"` returns `422`
      with `{error: {code: "validation_error", message, field_errors:
      {timezone: [...]}}}`.
- [ ] Every error response matches `{error: {code, message,
      field_errors?}}`.
- [ ] `SessionMerchantMiddleware` + `TenantMiddleware`: an authenticated
      request runs with `current_setting('app.current_merchant_id')`
      equal to the session merchant. An anonymous request gets no
      tenant context.
- [ ] `auditlog.services.record`:
  - [ ] Writes a row with the current tenant's `merchant_id` and the
        target label and id.
  - [ ] Raises `TenantContextError` with no context.
  - [ ] A row can't be created for another merchant (RLS).
- [ ] Django Admin: a staff superuser can log in and list users.
      `Merchant` is visible read-only, with no add, change or delete.
- [ ] `requirements.txt` pins `djangorestframework`.
- [ ] Docs updated per "Files to change" (the `self_membership` docs
      only after sign-off).
