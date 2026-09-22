# Multi-Tenancy Architecture

## Model: Shared Schema

One PostgreSQL schema, every tenant-owned table carries `merchant_id` directly, or transitively through a single FK hop (e.g. `Location.merchant_id`). This was chosen over schema-per-tenant because migrations, cross-tenant admin reporting, and billing rollups stay simple at the scale ReviewFlow targets (SMB merchants, not enterprises demanding hard schema isolation).

## Isolation Layers

### Layer 1 — Application-level: `TenantScopedManager`

Every tenant-owned model's default manager inherits from `core.TenantScopedManager`, which refuses to return a queryset without an active tenant context.

- Tenant context is set per-request by middleware after auth resolves `request.merchant`, stored in a **contextvar** (not global state — safe under async/threaded workers).
- **Celery tasks must receive `merchant_id` as an explicit argument** and set the same contextvar at the start of the task — never derived implicitly from another argument, so logs and error tracking are unambiguous.
- Views/serializers never accept a client-supplied `merchant_id`/`location_id` as the source of truth — it is always derived from the authenticated principal (session user's merchant, or the API key's merchant).

### Layer 2 — Database-level: Postgres Row-Level Security

- `SET LOCAL app.current_merchant_id = <id>` inside the database transaction that performs tenant work. The value is transaction-scoped and cannot leak across pooled requests.
- RLS policies on every tenant table compare its direct `merchant_id` to that transaction-local setting, or use an `EXISTS` check through its documented tenant parent for transitive tables.
- Django request flow: resolve merchant → `transaction.atomic()` → execute `SET LOCAL` → perform queries/writes → commit/rollback.
- Celery flow: task receives explicit `merchant_id` → enters `transaction.atomic()` for tenant DB work → sets the same `SET LOCAL` value before queries. Each new transaction repeats this step.
- This is a backstop that catches bugs where a raw query bypasses the manager — it is not the primary mechanism, application-level scoping is.

#### Login membership lookup — `self_membership` (signed off 2026-09-19)

At login no merchant is known yet, so the user's `TeamMember` rows must be read before any tenant context exists. `TeamMember` therefore carries a second, **SELECT-only** policy, `self_membership`: `user_id = app.current_user_id`, alongside the standard `tenant_isolation` policy.

- `app.current_user_id` is set only by `SET LOCAL` inside `core.tenancy.user_lookup_atomic()` — transaction-local, never session-level, fail-closed when unset.
- There is no write policy keyed on `app.current_user_id`; every insert/update/delete still needs `tenant_isolation`.
- PostgreSQL ORs PERMISSIVE policies, so `user_lookup_atomic()` refuses to run while a merchant context is active — combining both settings would expose the user's memberships in other merchants to that merchant's transaction.
- The application layer mirrors it: `TeamMember.objects.for_lookup_user(user_id)` works only inside `user_lookup_atomic()` for that same user.

### No Standing Privileged Role

The application's normal database role has RLS enforced on it with **no bypass** — there is no `BYPASSRLS`/superuser connection string sitting in ordinary settings, even for the admin panel. Cross-merchant admin/billing-rollup access goes through an explicit, narrowly-scoped, audited privileged service path (a management command or admin-only endpoint with staff auth and audit logging on every use) — never a different, permanently-unrestricted connection.

## Two Points Hardened After Architecture Review

### `IntegrationEvent` — merchant identified before creation, not after

Originally, `IntegrationEvent` could be created with `merchant_id = NULL` and resolved during normalization, with idempotency scoped to `(source, external_event_id)`. This was **not tenant-safe**: the same `external_event_id` (e.g. an invoice number) can legitimately recur across two different merchants both using the same POS provider, so a `source`-scoped uniqueness could theoretically collide across tenants.

**Fixed ingestion order:**

```
External Webhook/API → Identify Integration → Identify Merchant from Integration.merchant_id
   → Create IntegrationEvent (merchant_id already known, non-null)
   → Idempotency check: (integration_id, external_event_id)
   → Normalize → Resolve Location → Create/Update Transaction
```

`merchant_id` is never written as `NULL` and resolved later. The invariant `IntegrationEvent.merchant_id == Integration.merchant_id` is enforced at creation time in the service layer, not just documented as an expectation. See `../03-database/Database-Design.md` and `../04-api/Webhook-Specification.md`.

### `TeamMember` → `Location` — explicit through-model, not a default M2M

A Django default M2M between `TeamMember` and `Location` gives no natural place to enforce that the location belongs to the same merchant as the team member. `TeamMemberLocation` is an explicit through-model carrying its own `merchant_id`, with a service-layer invariant checked before every insert/update: `TeamMemberLocation.merchant_id == TeamMember.merchant_id == Location.merchant_id`. RLS filters rows by `TeamMemberLocation.merchant_id`; it does not independently prove equality against both referenced parent rows. A `TeamMember` from Merchant A can never be assigned to a `Location` owned by Merchant B — this is rejected at creation, not merely blocked by a permission check at read time. See `../03-database/Database-Design.md`. The role model itself (`OWNER`/`ADMIN`/`MANAGER`/`VIEWER`) is unchanged.

## Required Tests

See `../10-development/Testing-Strategy.md` for full detail; the tenant-isolation-specific cases are:

1. **Cross-tenant access attempt**: Merchant A's authenticated session/API key attempts to read or write Merchant B's `Transaction`/`Location`/etc. → must return 403/404, never the data.
2. **Manager bypass**: a raw SQL query without tenant scoping is blocked by RLS even if application code has a bug.
3. **Celery task tenant context**: a task given `merchant_id=A` cannot read/write rows belonging to `merchant_id=B`, even if another argument (e.g. a `transaction_id`) happens to be reachable.
4. **Cross-merchant event isolation**: two different merchants' integrations each receive a webhook with the *same* `external_event_id` → both `IntegrationEvent` rows are created successfully (no false-positive idempotency collision), each correctly attributed to its own merchant.
5. **Duplicate external event ID across integrations**: the same `external_event_id` posted twice to the *same* integration → no-op on the second (true idempotency); posted once each to *two different* integrations (even under the same merchant) → two separate events, since uniqueness is scoped to `integration_id`.
6. **Cross-tenant location assignment attempt**: an attempt to create a `TeamMemberLocation` row where the team member and location belong to different merchants → rejected before insert.

## Data Model Note

`Merchant` itself, and any global/reference tables (e.g. `Plan`), are the only tables that are **not** tenant-scoped — everything else attaches to `Merchant` directly or transitively.
