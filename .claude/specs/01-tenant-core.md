# Spec: Tenant Core

## Overview
Creates the `core` app: the tenant-isolation machinery every later
ReviewFlow model depends on. It provides a per-request/per-task tenant
contextvar, a context manager that runs tenant database work inside
`transaction.atomic()` with `SET LOCAL app.current_merchant_id`,
`TenantScopedManager` (application-layer scoping that refuses to query
without a tenant context), a reusable PostgreSQL RLS policy pattern for
direct and transitive tenant tables, a Celery entry point that takes
`merchant_id` explicitly, a tenant middleware, and base model/exception
classes. It also moves the application's database connection off the
Postgres superuser onto a non-superuser, non-`BYPASSRLS` role, because
superusers bypass RLS and would make every RLS policy meaningless. It
must exist before Phase 02 creates `Merchant` and the first tenant-owned
tables. It is shared foundation for all three planes (ingestion,
automation, intelligence).

## Source docs
- `docs/ROADMAP.md` — §"01 — Core tenancy & RLS"
- `docs/02-architecture/Multi-Tenancy.md` — §"Isolation Layers" (Layer 1,
  Layer 2), §"No Standing Privileged Role", §"Required Tests" (1–3)
- `docs/FINAL-ARCHITECTURE-REVIEW.md` — §8 (RLS)
- `docs/03-database/Database-Design.md` — Legend (GLOBAL / MERCHANT /
  LOCATION ownership), §"Design Principles Applied Throughout"
- `docs/03-database/Data-Dictionary.md` — preamble (UUID `id`,
  `created_at`, `updated_at` on every model)
- `docs/03-database/ERD.md` — §"Reading This Diagram" (`Merchant` is the
  tenant root)
- `docs/02-architecture/Security-Architecture.md` — §"Multi-Tenancy
  Isolation", §"Threat Model Highlights" (IDOR → UUIDs)
- `docs/09-security/Security-Controls.md` — §"Row-Level Security",
  §"Permissions & Access"
- `docs/02-architecture/SAD.md` — §3 (`core` app), §5 (Celery)
- `docs/10-development/Coding-Standards.md` — §2 (Tenant Scoping Rule)
- `docs/10-development/Testing-Strategy.md` — §"Tenant Isolation",
  §"Final Consistency Tests Added" → RLS (pooled connections)
- `docs/10-development/Development-Setup.md` — §"Local Services via
  Docker Compose", §"Environment Variables"

## Depends on
- Phase 00 — Project scaffold (Done): `config/` settings, Celery app,
  pytest + pytest-django against Postgres, `docker-compose.yml`.

## Roadmap Phase
- Phase: 01 — Core tenancy & RLS
- Completes entire phase: Yes

All five Phase 01 roadmap bullets are in scope: `core` app (base models,
`TenantScopedManager`, base exceptions); tenant contextvar + middleware;
transaction-local `SET LOCAL app.current_merchant_id`; Celery
tenant-context entry point with explicit `merchant_id`; RLS policy
pattern for direct and transitive tenant tables.

## Locked decisions touched
- Shared schema, `merchant_id` scoping (`Multi-Tenancy.md` §Model) —
  DEPENDS ON
- `TenantScopedManager` as primary application-layer scoping
  (`Multi-Tenancy.md` §Layer 1, `Coding-Standards.md` §2) — DEPENDS ON
- Tenant context in a contextvar, set per request / per task
  (`Multi-Tenancy.md` §Layer 1) — DEPENDS ON
- Transaction-local `SET LOCAL app.current_merchant_id` inside
  `transaction.atomic()` (`FINAL-ARCHITECTURE-REVIEW.md` §8) — DEPENDS ON
- RLS on every tenant table; direct `merchant_id` comparison, parent
  `EXISTS` check for transitive tables (`FINAL-ARCHITECTURE-REVIEW.md` §8,
  `Database-Design.md` §Design Principles) — DEPENDS ON
- No standing privileged role / no `BYPASSRLS` or superuser in normal
  application settings (`Multi-Tenancy.md` §No Standing Privileged Role,
  `FINAL-ARCHITECTURE-REVIEW.md` §8) — DEPENDS ON (this phase is what
  first makes it true locally)
- Celery tasks receive `merchant_id` explicitly and set tenant context
  first (`Multi-Tenancy.md` §Layer 1, `Coding-Standards.md` §2) —
  DEPENDS ON
- Externally exposed IDs are UUIDs (`Security-Architecture.md` §Threat
  Model) — DEPENDS ON

None are LOCKED DECISION CHANGE.

## Django apps
- `core` — **created**. Added to `INSTALLED_APPS`.
- No other app is created. `Merchant` is Phase 02 (`accounts`).

## Models & database changes
No tenant tables and no concrete models in this phase. `core` has no
migrations.

### `core.models.BaseModel` (abstract)
Per `Data-Dictionary.md` preamble:
- `id` — `UUIDField(primary_key=True, default=uuid.uuid4, editable=False)`
- `created_at` — `DateTimeField(auto_now_add=True)`
- `updated_at` — `DateTimeField(auto_now=True)`

Every later model inherits it. The tenant FK (`merchant`) is declared by
each tenant model in its own app, not in `core` (`Merchant` does not
exist yet).

### Tenant ID type
`app.current_merchant_id` holds a UUID, and policies cast it with
`::uuid`. **Phase 02 must give `Merchant` a UUID primary key** (via
`BaseModel`). This is consistent with `Data-Dictionary.md` ("UUID
recommended for externally-exposed models") and the UUID/hashid rule.

### RLS policy pattern (`core/rls.py`)
Helpers that return `migrations.RunSQL` operations. Later phases call
them in each tenant table's migration. The policy name is fixed:
`tenant_isolation`.

- **Direct tenant tables** (`merchant_id` column) —
  `rls_direct(table)`:
  ```sql
  ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
  ALTER TABLE <table> FORCE ROW LEVEL SECURITY;
  CREATE POLICY tenant_isolation ON <table>
    USING (merchant_id = NULLIF(current_setting('app.current_merchant_id', true), '')::uuid)
    WITH CHECK (merchant_id = NULLIF(current_setting('app.current_merchant_id', true), '')::uuid);
  ```
- **Transitive tenant tables** (tenant reached through a parent FK) —
  `rls_via_parent(table, fk_column, parent_table)`:
  ```sql
  ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
  ALTER TABLE <table> FORCE ROW LEVEL SECURITY;
  CREATE POLICY tenant_isolation ON <table>
    USING (EXISTS (SELECT 1 FROM <parent_table> p WHERE p.id = <table>.<fk_column>))
    WITH CHECK (EXISTS (SELECT 1 FROM <parent_table> p WHERE p.id = <table>.<fk_column>));
  ```
  The `EXISTS` subquery reads the parent under the parent's own RLS
  policy, so it works for chains of any depth
  (e.g. `GoogleReview → GoogleLocation → Location`). The parent table
  must itself have RLS.
- Both provide `reverse_sql` (`DROP POLICY` + `DISABLE ROW LEVEL
  SECURITY` + `NO FORCE ROW LEVEL SECURITY`).
- `NULLIF(..., '')` is required: once a custom setting has been set on a
  pooled connection, `current_setting(..., true)` returns `''` (not NULL)
  after the transaction ends, and `''::uuid` would raise an error instead
  of returning zero rows.
- `FORCE ROW LEVEL SECURITY` is required because the application role
  owns the tables it migrates, and table owners bypass RLS without
  `FORCE`.
- No context set → the comparison is NULL → zero rows visible, and every
  insert or update fails `WITH CHECK`. This fails closed.
- Table and column names come only from migration code, never from user
  input.

### Database role (fixes the Phase 00 superuser constraint)
- New `docker/postgres/init/01-app-role.sql`, mounted into the Postgres
  container's `/docker-entrypoint-initdb.d`. It creates role
  `reviewflow_app` with `LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE
  CREATEDB` (CREATEDB only so pytest-django can create the test
  database), password `local_dev_only`. It then runs
  `ALTER DATABASE reviewflow OWNER TO reviewflow_app`, so the role can
  create tables in `public` (Postgres 15+).
- `POSTGRES_USER: reviewflow` stays the container superuser for manual
  admin only. No application setting uses it.
- `DATABASE_URL` in `.env.example` and `Development-Setup.md` points at
  `reviewflow_app`.
- Init scripts run only on a fresh data directory. Developers recreate
  the container once: `docker compose down` then `docker compose up -d`
  (no `-v` needed; compose uses an anonymous volume). Then
  `python manage.py migrate` again, and update the local `.env`
  `DATABASE_URL` user by hand.

## API endpoints
No new endpoints.

## Services & background tasks
`core/tenancy.py` holds the tenancy primitives. They are core
infrastructure, not app business logic, so there is no `services.py`
in `core`.

- `get_current_merchant_id() -> uuid.UUID | None`
- `tenant_context(merchant_id)` — context manager. Normalizes to
  `uuid.UUID` (raises `ValueError` on invalid input), sets the contextvar,
  and resets it on exit via the contextvar token. Re-entering with the
  same merchant is allowed. Entering with a different merchant while one
  is active raises `TenantContextError`.
- `tenant_atomic()` — context manager. Requires an active tenant context
  (otherwise raises `TenantContextError`). Opens `transaction.atomic()`
  and, as its first statement, executes
  `SET LOCAL app.current_merchant_id = '<uuid>'`. `<uuid>` is
  `str(uuid.UUID(...))` of the contextvar value; canonical UUID
  formatting makes the literal injection-safe. It is transaction-local
  and never session-level (`FINAL-ARCHITECTURE-REVIEW.md` §8). Each new
  transaction must use `tenant_atomic()` again. A nested
  `tenant_atomic()` is a savepoint and re-issues the same `SET LOCAL`,
  which is harmless.
- `tenant_task` — Celery entry-point decorator, applied under
  `@shared_task`. The wrapped function's first positional argument must
  be `merchant_id`. The wrapper enters `tenant_context(merchant_id)`
  before the function body runs; the task body then uses
  `tenant_atomic()` for each database transaction. It never derives the
  merchant from another argument. Usage:
  ```python
  @shared_task(queue="events")
  @tenant_task
  def process_event(merchant_id, event_id): ...
  ```
- `core/managers.py` → `TenantScopedManager(models.Manager)`:
  `get_queryset()` raises `TenantContextError` if no tenant context is
  active; otherwise it filters `**{model.tenant_field: current_merchant_id}`.
  `tenant_field` is a model class attribute, default `"merchant_id"`.
  Transitive models set a lookup path, e.g. `"location__merchant_id"`.
  The manager does not set `merchant_id` on create; services pass it
  explicitly, and RLS `WITH CHECK` rejects a mismatch.
- `core/middleware.py` → `TenantMiddleware`: if `request.merchant_id`
  was set by an earlier authentication layer, it wraps the rest of the
  request in `tenant_context(request.merchant_id)` + `tenant_atomic()`.
  Otherwise it does nothing. Nothing sets `request.merchant_id` yet —
  Phase 02 adds session → merchant resolution. Placed after
  `AuthenticationMiddleware` in `MIDDLEWARE`.
  - Note for Phase 05: DRF authenticates API keys inside the view, after
    middleware has run. The API-key path must enter
    `tenant_context` + `tenant_atomic` from its DRF authentication/view
    layer, using these same primitives.
- `core/exceptions.py` → `ReviewFlowError(Exception)` (base) and
  `TenantContextError(ReviewFlowError)`. The API error shape
  (`{error: {code, message, field_errors}}`) arrives with DRF in Phase 02.
- No Celery tasks, no Beat schedules, no adapter/provider interfaces.

## Admin
No admin changes. Note for Phase 16: `TenantScopedManager` raises
without a tenant context, so cross-tenant Django Admin views need the
audited privileged path (`Multi-Tenancy.md` §No Standing Privileged
Role). That is Phase 16 scope.

## Files to change
- `config/settings.py` — add `"core"` to `INSTALLED_APPS`; add
  `"core.middleware.TenantMiddleware"` after `AuthenticationMiddleware`
- `docker-compose.yml` — mount `./docker/postgres/init` into
  `/docker-entrypoint-initdb.d` (read-only)
- `.env.example` — `DATABASE_URL` user `reviewflow` → `reviewflow_app`
- `docs/10-development/Development-Setup.md` — §Local Services: note the
  init script / app role; §Environment Variables: `DATABASE_URL` uses
  `reviewflow_app` (`docs/` edit prompts for permission)
- `config/tests/test_scaffold.py` — none expected. If any existing test
  hard-codes the `reviewflow` DB user in a way that breaks, stop and
  report; do not weaken it.

## Files to create
- `core/__init__.py`
- `core/apps.py`
- `core/models.py` — `BaseModel`
- `core/managers.py` — `TenantScopedManager`
- `core/tenancy.py` — contextvar, `get_current_merchant_id`,
  `tenant_context`, `tenant_atomic`, `tenant_task`
- `core/middleware.py` — `TenantMiddleware`
- `core/exceptions.py` — `ReviewFlowError`, `TenantContextError`
- `core/rls.py` — `rls_direct`, `rls_via_parent`
- `core/tests/__init__.py`
- `core/tests/test_tenancy.py`, `core/tests/test_rls.py` (written by
  `/test-feature`)
- `docker/postgres/init/01-app-role.sql`

## New dependencies
No new dependencies. Everything uses the standard library (`contextvars`,
`uuid`, `functools`, `contextlib`) and the six pinned packages.

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

Phase-specific rules:
- Tenant context is set only through `tenant_context()`. Never use a
  session-level `SET` or `set_config(..., false)`, and never keep
  merchant state in a module global or thread-local.
- `tenant_atomic()` fails closed: no context → `TenantContextError`,
  never a silent unscoped transaction.
- The RLS SQL must be exactly the pattern above (ENABLE + FORCE +
  `USING` + `WITH CHECK`, `NULLIF` guard). Do not add a permissive
  bypass policy, an "admin" policy, or a `current_user` exemption.
- No application setting may connect as a superuser or `BYPASSRLS`
  role. The test suite must prove the connected role is neither.
- `core` stays thin: no `Merchant`, no role permission classes, no DRF,
  no audit log. Those belong to Phase 02.
- Do not create concrete tenant models in production code for testing.
  RLS/manager tests build throwaway tables inside the test (e.g.
  unmanaged test models plus `schema_editor.create_model`, then the
  `core/rls.py` SQL), so the real helper SQL is what gets tested.

## Definition of done
- [ ] After recreating the Postgres container, `SELECT rolsuper,
      rolbypassrls FROM pg_roles WHERE rolname = current_user` returns
      `false, false` for the application connection (pytest asserts
      this)
- [ ] `python manage.py check` reports no issues;
      `python manage.py makemigrations --check --dry-run` reports no
      changes; `python manage.py migrate` succeeds as `reviewflow_app`
- [ ] `pytest` passes, including the Phase 00 tests
- [ ] `tenant_context`: sets and resets the contextvar; same-merchant
      nesting allowed; different-merchant nesting raises
      `TenantContextError`; invalid UUID raises `ValueError`
- [ ] `tenant_atomic`: without context raises `TenantContextError`;
      inside it, `current_setting('app.current_merchant_id')` equals the
      merchant UUID
- [ ] **Pooled connection / SET LOCAL** (Testing-Strategy §Final
      Consistency Tests → RLS): after a committed `tenant_atomic()`
      block ends, the same connection's
      `current_setting('app.current_merchant_id', true)` is empty, and
      a following transaction without context sees zero tenant rows
- [ ] `TenantScopedManager`: raises without context; returns only the
      current merchant's rows (direct `merchant_id` and a transitive
      `tenant_field` path)
- [ ] **Tenant isolation, application layer** (Testing-Strategy §Tenant
      Isolation; Multi-Tenancy Required Test 1): under merchant A's
      context the manager never returns merchant B's rows
- [ ] **Tenant isolation, RLS backstop** (Multi-Tenancy Required Test 2):
      a raw SQL `SELECT` that bypasses the manager, run in
      `tenant_atomic()` as merchant A, returns only A's rows on a
      `rls_direct` table and on a `rls_via_parent` child table; with no
      context set, it returns zero rows
- [ ] RLS `WITH CHECK`: inserting or updating a row with merchant B's
      `merchant_id` (or a child pointing at B's parent) while in A's
      context fails with a database error
- [ ] **Celery tenant context** (Multi-Tenancy Required Test 3): a
      `@tenant_task` task called with `merchant_id=A` (eager mode) cannot
      read or write merchant B's rows, even when passed the ID of a row
      that belongs to B
- [ ] `tenant_task` rejects a missing/invalid `merchant_id`
- [ ] `TenantMiddleware`: with `request.merchant_id` set, the view runs
      inside the tenant context and a transaction with the `SET LOCAL`
      value; without it, the request passes through unchanged; the
      contextvar is reset after the response
- [ ] `rls_direct` / `rls_via_parent` reverse SQL removes the policy and
      disables RLS (apply → reverse → table readable without context)
- [ ] `.env.example` and `Development-Setup.md` use `reviewflow_app` in
      `DATABASE_URL`; `.env.example` contains no real secrets
