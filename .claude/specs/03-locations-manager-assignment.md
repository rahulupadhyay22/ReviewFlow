# Spec: Locations, Manager Assignment & Seed Data

## Overview
Adds the `Location` model, which later phases attach to: campaigns,
the WhatsApp sender mapping, the Google mapping, QR codes, integration
mappings and analytics. It also adds `/locations` CRUD, the explicit
`TeamMemberLocation` through-model that scopes a `MANAGER` to specific
locations, `PUT /team-members/{id}/locations`, and a local seed command
that creates a test merchant with two locations. With this spec,
MANAGER location scoping actually works: a MANAGER sees and edits only
their assigned locations. This is shared foundation for all three
planes (ingestion, automation, intelligence). Phase 04 resolves sales
to a `Location`, Phases 08/09/10 hang their per-location configuration
off it, and Phase 14 aggregates by it. It is the only spec for
Phase 03.

Decisions this spec makes where the docs are silent. Each one is
written into `API-Specification.md` so nothing is left ambiguous:
- **Per-location settings** means the `Location` fields themselves,
  with `timezone` as the only override of a merchant default. There is
  no `LocationSettings` model. Location-level campaign, WhatsApp and
  Google settings (`Business-Rules.md` §7) belong on `ReviewCampaign`,
  `WhatsAppLocationMapping` and `GoogleLocation`, in Phases 08–10.
- **A MANAGER who is not assigned to a location gets `404`, never
  `403`**, on `GET`/`PATCH /locations/{id}`. This is the same rule as
  cross-merchant ids, and it matches "MANAGER sees only assigned
  locations".
- **`PUT /team-members/{id}/locations` is OWNER/ADMIN only**, like the
  rest of §Team. The *target* must have role `MANAGER`; any other
  target returns `422`.
- **When a member's role changes away from `MANAGER`, their
  assignments are deleted** in the same transaction. A later change
  back to `MANAGER` starts with no locations, so old access never
  comes back silently.
- **An assignment change is audited** as
  `team_member.locations_changed`. It grants or removes data access, so
  it counts as a privileged action (`Audit-Logging.md` purpose). Location
  create/update/deactivate is not audited: it is not in
  `Audit-Logging.md` §"What Gets Logged".
- **Deactivated locations stay visible.** `GET /locations` lists active
  and inactive locations and includes `is_active`. There is no
  reactivation endpoint, because the API spec defines none.
- **Member body field name:** `location_ids` (a list of UUIDs), which
  mirrors the `PUT` request.

## Source docs
- `docs/ROADMAP.md`: §"03 — Locations, manager assignment & seed data"
- `docs/04-api/API-Specification.md`: preamble (`merchant_id` from the
  principal), §Locations, §Team (member body, the
  `PUT /team-members/{id}/locations` note, "arrive with Phase 03"),
  §"General Conventions" (cursor pagination, `?cursor=`/`?limit=` max
  100, error shape, UUIDs)
- `docs/02-architecture/Multi-Tenancy.md`: §Layer 1, §Layer 2,
  §"`TeamMember` → `Location` — explicit through-model", §"Required
  Tests" 1, 2, 6
- `docs/03-database/Database-Design.md`: §TeamMember, §TeamMemberLocation,
  §Location, §"Design Principles Applied Throughout"
- `docs/03-database/Data-Dictionary.md`: §TeamMemberLocation, §Location
- `docs/03-database/ERD.md`: `Merchant 1───* TeamMemberLocation *───1 Location`
- `docs/02-architecture/Security-Architecture.md`: §Authorization
  (MANAGER = assigned locations only; `TeamMemberLocation`), §"Threat
  Model Highlights" (IDOR)
- `docs/01-product/PRD.md`: multi-location management with
  per-location settings; §7 User Roles (Manager)
- `docs/01-product/Business-Rules.md`: §7 (Multi-Location Rules)
- `docs/01-product/Feature-Scope.md`: V1 "multi-location management"
- `docs/09-security/Audit-Logging.md`: §Purpose, §"What Gets Logged"
- `docs/10-development/Development-Setup.md`: §"Seed Data"
- `docs/02-architecture/SAD.md`: §3 (`locations/`, `accounts/` owns
  `TeamMemberLocation`)
- `docs/10-development/Coding-Standards.md`: §1, §2, §4, §5, §6
- `docs/10-development/Testing-Strategy.md`: §"Tenant Isolation",
  §"Cross-Tenant Location Assignment", Permission tests ("MANAGER cannot
  access unassigned locations"), API tests, Database constraint tests
- `.claude/specs/02-team-member-management.md`: the team-mutation
  locking pattern (`_lock_team`, `_lock_target`), the Phase 03
  exclusions, and the `# ponytail:` pagination forward reference
- `.claude/specs/02-merchant-account-foundation.md`:
  `create_merchant_with_owner`, "used by the Phase 03 seed command"

## Depends on
- Phase 00, Project scaffold (Done)
- Phase 01, Core tenancy & RLS (Done): `TenantScopedManager`,
  `tenant_context`, `tenant_atomic`, `core.rls.rls_direct`,
  `TenantMiddleware`
- Phase 02, Accounts, roles & audit log (Done): `Merchant`, `User`,
  `TeamMember`, `IsMerchantMember` / `HasRole` / `IsOwnerOrAdmin`,
  session auth + CSRF, `accounts.services` (`create_merchant_with_owner`,
  `validate_timezone`, `invite_team_member`, `accept_invite`,
  `_lock_team`, `_lock_target`, `change_team_member_role`,
  `revoke_team_member`), `auditlog.services.record`,
  `core.api.exception_handler`

### Boundary with Phase 02
- This spec **builds** what Phase 02 explicitly deferred:
  `TeamMemberLocation`, `PUT /team-members/{id}/locations`, and the
  assigned-locations field on the member body.
- This spec **modifies** two Phase 02 services, narrowly:
  - `change_team_member_role` clears assignments when the role leaves
    `MANAGER`.
  - `list_team_members` prefetches assignments.

  Neither changes any Phase 02 permission, lock, audit or invite rule.
- One Phase 02 test changes, because its requirement changed:
  `test_get_team_members_returns_only_current_merchant_pending_and_accepted_no_locations`
  (`accounts/tests/test_team_api.py:124`) asserts that there is no
  locations field. It is updated to assert that `location_ids` is
  present. It is not weakened: every other assertion stays.
- `revoke_team_member` does not change. `TeamMemberLocation.team_member`
  is `on_delete=CASCADE`, so `member.delete()` removes the member's
  assignments in the same transaction.

## Roadmap Phase
- Phase: 03 — Locations, manager assignment & seed data
- Completes entire phase: Yes
- It covers all three ROADMAP §03 bullets. The seed command's
  `Plan`/`Subscription` pair and its `SHARED_POOL` `WhatsAppAccount`
  fixture are assigned to Phases 07 and 08 by the roadmap itself, so
  they are not Phase 03 work.

## Locked decisions touched
- Shared schema, `merchant_id` scoping (`Multi-Tenancy.md` §Model) —
  DEPENDS ON
- `TenantScopedManager` as primary application-layer scoping
  (`Multi-Tenancy.md` §Layer 1) — DEPENDS ON
- Transaction-local `SET LOCAL app.current_merchant_id`, RLS on every
  tenant table (`FINAL-ARCHITECTURE-REVIEW.md` §8) — DEPENDS ON
- `TeamMemberLocation` explicit through-model with a three-way
  merchant-match service invariant (`Multi-Tenancy.md` §"Two Points
  Hardened"; `Database-Design.md` §TeamMemberLocation) — DEPENDS ON
  (built here exactly as documented)
- No standing privileged role / no `BYPASSRLS` (`Multi-Tenancy.md` §No
  Standing Privileged Role) — NO CHANGE
- Roles OWNER/ADMIN/MANAGER/VIEWER via DRF permission classes
  (`Security-Architecture.md` §Authorization) — DEPENDS ON
- Three authentication mechanisms, never mixed; dashboard = session +
  CSRF (`Authentication.md`) — DEPENDS ON
- Externally exposed IDs are UUIDs (`Security-Architecture.md` §Threat
  Model) — DEPENDS ON
- Privileged actions write an `AuditLog` row (`Audit-Logging.md`) —
  DEPENDS ON
- Location soft-delete, no cascade to history (`API-Specification.md`
  §Locations `DELETE`) — DEPENDS ON
- `GoogleConnection` merchant-level / `GoogleLocation` location-level,
  `IntegrationLocationMapping`, `WhatsAppLocationMapping` — NO CHANGE
  (later phases; `Location` is only their FK target)

None. No line is a LOCKED DECISION CHANGE.

## Django apps
- `locations`: **created**. Holds the `Location` model, services,
  serializers, views and URLs. Added to `INSTALLED_APPS`.
- `accounts`: **touched**. Adds the `TeamMemberLocation` model, the
  assignment service, the member-body `location_ids` field,
  `PUT /team-members/{id}/locations`, and the
  `IsOwnerAdminOrManager` permission class.
- `core`: **touched**. Adds the shared cursor pagination class and the
  `seed_dev` management command.
- `auditlog`: **used**. Called through `record()` only; its code does
  not change.
- `config`: **touched**. `INSTALLED_APPS` and URL include.

## Models & database changes

### `locations.Location` (new; tenant ownership MERCHANT)
Fields per `Data-Dictionary.md` §Location, on `core.BaseModel` (UUID
`id`, `created_at`, `updated_at`):

| Field | Django type | Null | Notes |
|---|---|---|---|
| `merchant` | `ForeignKey("accounts.Merchant", on_delete=PROTECT, related_name="locations")` | No | Merchant is soft-delete only |
| `name` | `CharField(max_length=255)` | No | |
| `address` | `CharField(max_length=500, null=True, blank=True)` | Yes | |
| `phone` | `CharField(max_length=32, null=True, blank=True)` | Yes | display contact, free-form; not a customer E.164 phone |
| `timezone` | `CharField(max_length=64, null=True, blank=True)` | Yes | IANA name; overrides `Merchant.timezone` when set |
| `is_active` | `BooleanField(default=True)` | No | `DELETE` sets `False` |

- `objects = TenantScopedManager()`. `tenant_field` is the default
  `merchant_id`.
- Index: `(merchant_id)`, from the FK's automatic index. No other
  index.
- RLS: `rls_direct("locations_location")` in its own migration
  (`tenant_isolation` policy, `ENABLE` + `FORCE`).
- Delete behavior: never hard-deleted by application code. `PROTECT`
  from `Merchant`.

### `accounts.TeamMemberLocation` (new; tenant ownership MERCHANT)
Fields per `Data-Dictionary.md` §TeamMemberLocation, on `core.BaseModel`:

| Field | Django type | Null | Notes |
|---|---|---|---|
| `merchant` | `ForeignKey(Merchant, on_delete=PROTECT, related_name="+")` | No | must equal `team_member.merchant_id` and `location.merchant_id` |
| `team_member` | `ForeignKey(TeamMember, on_delete=CASCADE, related_name="location_assignments")` | No | CASCADE so that revoking a member removes their assignments |
| `location` | `ForeignKey("locations.Location", on_delete=PROTECT, related_name="team_member_assignments")` | No | locations are never hard-deleted |

- `objects = TenantScopedManager()`.
- `UniqueConstraint(fields=["team_member", "location"],
  name="uniq_teammemberlocation_member_location")`. This is the
  duplicate-assignment guard.
- RLS: `rls_direct("accounts_teammemberlocation")`. It filters by the
  row's own `merchant_id`. It does **not** prove the three-way parent
  equality, so the service layer enforces that (see Services).
- FK string reference to `"locations.Location"` avoids an import cycle
  between `accounts.models` and `locations.models`.

### Migrations
One logical change each. No data is dropped:
1. `locations/migrations/0001_initial.py`: `Location`. Depends on
   `accounts` `0003_user_totp`.
2. `locations/migrations/0002_location_rls.py`:
   `rls_direct("locations_location")`.
3. `accounts/migrations/0004_teammemberlocation.py`:
   `TeamMemberLocation` + unique constraint. Depends on `locations`
   `0001_initial`.
4. `accounts/migrations/0005_teammemberlocation_rls.py`:
   `rls_direct("accounts_teammemberlocation")`.

### Idempotency and concurrency
- `UNIQUE(team_member_id, location_id)` is the real guard against
  duplicate assignments. The replace-set write runs under the
  `Merchant` row lock (below), so concurrent `PUT`s for the same member
  are serialized and never collide on the constraint.
- `PUT /team-members/{id}/locations` and `change_team_member_role` both
  take the `Merchant` row lock first (`_lock_team`). An assignment write
  and a concurrent role change away from `MANAGER` are therefore
  serialized, so a non-MANAGER can never end up with assignment rows.

## API endpoints
All endpoints are under `/api/v1/`, with session auth + CSRF on writes.
`merchant_id` always comes from the session. It is never taken from
request data, and a `merchant_id` in a request body is ignored. Every
error uses `{ "error": { "code", "message", "field_errors"? } }`.
Cross-merchant ids, and ids of unassigned locations for a MANAGER,
return `404`, never `403`.

Location body (`LocationSerializer`):
`{ id, name, address, phone, timezone, is_active, created_at, updated_at }`.

- `GET /locations`: lists locations visible to the caller, active and
  inactive. Session. Any role.
  - A MANAGER sees only assigned locations.
  - Returns `200 { results: [Location], next_cursor }`.
  - Cursor pagination: `?cursor=`, `?limit=` (default 25, max 100;
    larger values are clamped), ordered by `created_at`.
  - `next_cursor` is `null` on the last page.
- `POST /locations`: `{ name, address?, phone?, timezone? }`. Session.
  OWNER, ADMIN.
  - Returns `201` with the Location body. `is_active` is always `true`
    on create.
  - `422` for a missing or blank `name`, an invalid IANA `timezone`, or
    over-length fields.
  - Any `merchant_id`, `id` or `is_active` in the body is ignored.
- `GET /locations/{id}`: session. Any role.
  - Returns `200` with the Location body.
  - `404` for an id that does not exist, belongs to another merchant,
    or is not assigned to the calling MANAGER.
- `PATCH /locations/{id}`: partial `{ name?, address?, phone?,
  timezone? }`. Session. OWNER, ADMIN, or a MANAGER assigned to this
  location.
  - Returns `200` with the Location body.
  - `403` for VIEWER.
  - `404` as for `GET`. An unassigned MANAGER gets `404`.
  - `422` for validation errors. `address`, `phone` and `timezone`
    accept `null` to clear the value.
  - `is_active` is not writable here.
- `DELETE /locations/{id}`: soft-delete (`is_active = False`). Session.
  OWNER, ADMIN.
  - Returns `204`. It is idempotent: deleting an inactive location
    returns `204` again.
  - It never deletes or changes assignments or any other row.
  - `403` for MANAGER and VIEWER. `404` for a cross-merchant id.
- `PUT /team-members/{id}/locations`: `{ location_ids: [uuid, ...] }`.
  Replaces the member's assignment set atomically. An empty list
  clears it, and duplicate ids are collapsed. Session. OWNER, ADMIN.
  - Returns `200` with the member body, including the new
    `location_ids`.
  - `403` for MANAGER and VIEWER.
  - `404` if `{id}` is not a member of the current merchant.
  - `422` if the target's role is not `MANAGER`.
  - `422` if any `location_id` is unknown or belongs to another
    merchant. Both cases return the same body, so a response never
    reveals whether a location exists in another merchant.
  - `422` for a malformed UUID.
  - The target may be pending (not yet accepted). Assigning a pending
    MANAGER before they accept is allowed.
  - Inactive locations may be assigned.

Member body (`TeamMemberSerializer`, all `/team-members` responses)
gains `location_ids: [uuid]`:
`{ id, user: { id, email }, role, invited_at, accepted_at, location_ids }`.
It is `[]` for every non-MANAGER, because clearing on role change keeps
it empty.

## Services & background tasks

### `core/pagination.py` (new)
- `CursorPagination(rest_framework.pagination.CursorPagination)`:
  - `page_size = 25`
  - `page_size_query_param = "limit"`
  - `max_page_size = 100`
  - `ordering = "created_at"`
  - `get_paginated_response(data)` returns
    `{"results": data, "next_cursor": <cursor param parsed from
    get_next_link(), or None>}`.

  This is the shared list-pagination pattern for every later list
  endpoint (`API-Specification.md` §General Conventions).

### `locations/exceptions.py` (new)
- `LocationNotFound(ReviewFlowError)`: `http_status = 404`,
  `code = "not_found"`, message "Location not found.". It is used both
  for cross-merchant ids and for unassigned MANAGERs.

### `locations/services.py` (new)
Every function requires a tenant context. `actor` is the acting
`TeamMember` (`request.team_member`).
- `accessible_locations(actor: TeamMember) -> QuerySet[Location]`:
  - For `MANAGER`,
    `Location.objects.filter(team_member_assignments__team_member=actor)`.
  - For every other role, `Location.objects.all()`, which is still
    tenant-scoped.
  - This is **the** MANAGER location-scoping rule. Later phases reuse
    it (e.g. Phase 10 `POST /campaigns` "MANAGER (own locations)") and
    must not re-implement it.
- `get_accessible_location(actor: TeamMember, location_id) -> Location`:
  `accessible_locations(actor).get(pk=location_id)`. Raises
  `LocationNotFound` when the id is missing, belongs to another
  merchant, or is not assigned to a MANAGER.
- `create_location(*, name: str, address: str | None = None, phone:
  str | None = None, timezone: str | None = None) -> Location`:
  - Calls `accounts.services.validate_timezone` when `timezone` is set.
    It raises `ValidationError`, which returns `422`.
  - Creates the row with `merchant_id=get_current_merchant_id()`,
    inside `tenant_atomic()`.
- `update_location(location: Location, *, name=_UNSET, address=_UNSET,
  phone=_UNSET, timezone=_UNSET) -> Location`:
  - Follows the `update_merchant` pattern: only the given fields change,
    and `save(update_fields=[..., "updated_at"])`.
  - `timezone` is validated when it is not `None`.
- `deactivate_location(location: Location) -> None`: sets
  `is_active=False` if it is not already. It is idempotent, and it
  touches no other row.

### `accounts/exceptions.py` (no additions)
The assignment `422`s use Django `ValidationError` with
`field_errors`, which `core.api.exception_handler` already maps.
`TeamMemberNotFound` and `TeamPermissionDenied` are reused.

### `accounts/services.py` (additions and changes)
- `AUDIT_LOCATIONS_CHANGED = "team_member.locations_changed"`, next to
  the Phase 02 canonical constants.
- `set_team_member_locations(*, actor: TeamMember, member_id, location_ids:
  list[uuid.UUID]) -> TeamMember`:
  1. Inside `tenant_atomic()`, call `actor = _lock_team(actor)`. This
     locks the `Merchant` row and re-reads the actor under the lock.
  2. If `actor.role` is not `OWNER` or `ADMIN`, raise
     `TeamPermissionDenied`. This post-lock role check makes the call
     race-safe against a concurrent demotion.
  3. `member = _lock_target(member_id)`. A missing or other-merchant id
     raises `TeamMemberNotFound` (`404`).
  4. If `member.role` is not `MANAGER`, raise `ValidationError(
     {"location_ids": ["Only MANAGER team members can be assigned
     locations."]})`.
  5. `wanted = set(location_ids)`, then
     `locations = list(Location.objects.filter(pk__in=wanted))`. If
     `len(locations) != len(wanted)`, raise `ValidationError(
     {"location_ids": ["One or more locations were not found."]})`.
     The body is the same for unknown and cross-merchant ids.
  6. `_assert_same_merchant(member, locations)`.
  7. Compare with the current set. If it is unchanged, return the
     member without an audit row. Otherwise, delete the removed rows,
     then
     `TeamMemberLocation.objects.bulk_create([...])` the added ones
     with `merchant_id=member.merchant_id`.
  8. Call `record(AUDIT_LOCATIONS_CHANGED, actor=actor.user,
     target=member, metadata={"location_ids": sorted(str ids of the
     new set)})`, in the same transaction.
  9. Return the member, with assignments re-fetched for the response.
- `_assert_same_merchant(member: TeamMember, locations:
  Iterable[Location]) -> None`: **the documented three-way invariant,
  checked explicitly**.
  - Raises `ValidationError({"location_ids": ["One or more locations
    were not found."]})` unless
    `location.merchant_id == member.merchant_id ==
    get_current_merchant_id()` for every location.
  - It runs before any insert.
  - It must not rely on RLS or `TenantScopedManager` having filtered
    the rows already. `Multi-Tenancy.md` says RLS "does not
    independently prove equality against both referenced parent rows".
  - It has its own unit test that reaches it with a mismatched,
    unsaved `Location`. That test must not go through the scoped read,
    or the check would pass without ever running.
- `change_team_member_role` (**changed**): after the existing checks
  and before `record()`, if `role_from == MANAGER and role != MANAGER`,
  run
  `TeamMemberLocation.objects.filter(team_member=member).delete()` in
  the same transaction. The existing `team_member.role_changed` audit
  row and its metadata do not change.
- `list_team_members` (**changed**): add
  `.prefetch_related("location_assignments")`.

### Canonical audit actions (addition)
| Action | Written by | Metadata |
|---|---|---|
| `team_member.locations_changed` | `set_team_member_locations` (only when the set changes) | `{"location_ids": [sorted UUID strings]}` |

Location UUIDs only. Never names, addresses or phones.

### Views and permissions
- `accounts/permissions.py`: add `IsOwnerAdminOrManager(HasRole)` with
  roles `{OWNER, ADMIN, MANAGER}`. Update the module docstring:
  MANAGER location scoping now lives in
  `locations.services.accessible_locations`.
  - Role gates stay in permission classes.
  - Target-aware scoping ("assigned to this location") stays in the
    service. This follows the Phase 02 `_check_can_manage` precedent,
    because a permission class cannot see the assignment.
- `locations/views.py`:
  - `LocationListView`:
    - `GET`: `IsMerchantMember` (the default). Paginates
      `accessible_locations(request.team_member)` with
      `core.pagination.CursorPagination`.
    - `POST`: `IsOwnerOrAdmin`.
  - `LocationDetailView`:
    - `GET`: `IsMerchantMember`.
    - `PATCH`: `IsOwnerAdminOrManager`.
    - `DELETE`: `IsOwnerOrAdmin`.
    - Every method loads the row with
      `get_accessible_location(request.team_member, pk)`, then calls
      the service.
  - The views stay thin: serializer validation, then a service call.
- `accounts/views.py`: `TeamMemberLocationsView` (`PUT`),
  `permission_classes = [IsOwnerOrAdmin]`.

### Serializers
- `locations/serializers.py`:
  - `LocationSerializer` (read).
  - `LocationCreateSerializer`: `name` required, not blank; `address`,
    `phone` and `timezone` optional and nullable; max lengths as in the
    model.
  - `LocationUpdateSerializer`: partial, same fields.
- `accounts/serializers.py`:
  - `TeamMemberSerializer` gains `location_ids`
    (`SerializerMethodField` over the prefetched
    `location_assignments`).
  - New `TeamMemberLocationsSerializer`:
    `location_ids = ListField(child=UUIDField(), allow_empty=True)`.

### Management command: `core/management/commands/seed_dev.py` (new)
`python manage.py seed_dev [--password <pw>]`. For local development
only (`Development-Setup.md` §"Seed Data"). It lives in `core`, because
Phases 07 and 08 extend it with billing and WhatsApp fixtures.
- Refuses to run unless `settings.DEBUG` is true. It raises
  `CommandError` and writes nothing.
- Idempotent: if the seed owner email `owner@seed.reviewflow.local`
  already exists, it prints "already seeded" and exits `0`. It never
  creates a second merchant.
  - `# ponytail:` existence check on a dev-only command. The
    `User.email` unique constraint still guards a racing second run.
- Creates everything through existing services, never with raw ORM
  writes that bypass the invariants:
  1. `create_merchant_with_owner(name="Seed Cafe", timezone="Asia/Kolkata",
     owner_email=..., owner_password=pw)`.
  2. Inside `tenant_context(merchant.id)`, two `create_location` calls:
     "Seed Cafe — Indiranagar" and "Seed Cafe — Koramangala".
  3. A MANAGER, `manager@seed.reviewflow.local`, created with
     `invite_team_member(actor=owner, role=MANAGER)` followed by
     `accept_invite(token, pw)`.
  4. `set_team_member_locations(actor=owner, member_id=manager.id,
     location_ids=[first location])`, so MANAGER scoping can be
     checked by hand.
- Password: `--password` if given, else `secrets.token_urlsafe(16)`.
  The command prints the two emails and the password once to stdout.
  It never hardcodes a password in the repo and never writes one to a
  log.
- It does **not** create the `Plan`/`Subscription` pair (Phase 07) or
  the `SHARED_POOL` `WhatsAppAccount` (Phase 08).

### Tasks, adapters and schedules
- No Celery tasks
- No Beat schedules
- No adapters or providers

## Admin
No admin changes. This follows Phase 02: tenant-owned models
(`Location`, `TeamMemberLocation`) stay unregistered until the Phase 16
audited privileged cross-tenant path exists. A Django Admin changelist
runs without a tenant context, so `TenantScopedManager` would raise
and RLS would return nothing.

## Files to change
- `config/settings.py`: add `"locations"` to `INSTALLED_APPS`, after
  `"accounts"`.
- `config/urls.py`: `path("api/v1/", include("locations.urls"))`.
- `accounts/models.py`: `TeamMemberLocation`.
- `accounts/services.py`:
  - add `set_team_member_locations`, `_assert_same_merchant` and
    `AUDIT_LOCATIONS_CHANGED`
  - `change_team_member_role` clears assignments when the role leaves
    MANAGER
  - `list_team_members` prefetches assignments
- `accounts/serializers.py`: `location_ids` on `TeamMemberSerializer`;
  `TeamMemberLocationsSerializer`.
- `accounts/views.py`: `TeamMemberLocationsView`.
- `accounts/urls.py`: `team-members/<uuid:pk>/locations`
  (name `team-member-locations`).
- `accounts/permissions.py`: `IsOwnerAdminOrManager`; docstring update.
- `accounts/tests/test_team_api.py`: update
  `test_get_team_members_returns_only_current_merchant_pending_and_accepted_no_locations`
  so it asserts `location_ids` is present, and rename `_no_locations`
  to `_with_location_ids`. Every other assertion stays.
- `docs/04-api/API-Specification.md`:
  - §Locations:
    - Location response body
    - `{results, next_cursor}` shape
    - unassigned MANAGER → `404` (Resolved)
    - `PATCH` fields; `is_active` not writable
    - `DELETE` idempotent, no reactivation endpoint
    - list includes inactive locations
  - §Team:
    - replace the "There is no `locations` field yet…" paragraph with
      the `location_ids` member body field
    - `PUT /team-members/{id}/locations`:
      - Permissions: OWNER, ADMIN
      - response `200` member body
      - `404` / `403` / `422` rules (non-MANAGER target; unknown or
        cross-merchant ids, same body)
      - replace-set semantics; empty list clears
    - `PATCH /team-members/{id}`: a role change away from MANAGER
      clears the assignments
- `docs/09-security/Audit-Logging.md` §"What Gets Logged",
  merchant-side: add "Manager location assignments changed".
- `docs/10-development/Development-Setup.md` §"Seed Data": document
  `python manage.py seed_dev [--password]`: DEBUG-only, idempotent, and
  what it creates now vs. in Phases 07 and 08.

## Files to create
- `locations/__init__.py`
- `locations/apps.py`
- `locations/models.py`
- `locations/exceptions.py`
- `locations/services.py`
- `locations/serializers.py`
- `locations/views.py`
- `locations/urls.py`
- `locations/migrations/__init__.py`
- `locations/migrations/0001_initial.py`
- `locations/migrations/0002_location_rls.py`
- `accounts/migrations/0004_teammemberlocation.py`
- `accounts/migrations/0005_teammemberlocation_rls.py`
- `core/pagination.py`
- `core/management/__init__.py`
- `core/management/commands/__init__.py`
- `core/management/commands/seed_dev.py`

Tests, written by `/test-feature`:
- `locations/tests/__init__.py`
- `locations/tests/conftest.py`
- `locations/tests/test_services.py`
- `locations/tests/test_api.py`
- `locations/tests/test_rls.py`
- `accounts/tests/test_member_locations.py` (service, API, invariant,
  role-change clearing, revoke cascade, audit)
- `core/tests/test_pagination.py`
- `core/tests/test_seed_dev.py`

## New dependencies
No new dependencies. Pagination uses DRF's `CursorPagination`, and the
seed password uses stdlib `secrets`.

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
- **Do not build these (later phases):**
  - `ReviewCampaign` (Phase 10)
  - `GoogleConnection` / `GoogleLocation` (Phase 09)
  - `WhatsAppAccount` / `WhatsAppLocationMapping` (Phase 08)
  - `Integration` / `IntegrationLocationMapping` / `Transaction`
    (Phase 04)
  - `QRCode` (Phase 13)
  - `LocationDailyStats` (not scheduled in V1)
  - any seed fixture for Plan, Subscription or WhatsApp
- **Do not build these:**
  - a `LocationSettings` model
  - an "effective timezone" helper (the first consumer adds it)
  - a location reactivation endpoint
  - `?is_active` or other filters on `GET /locations` (none are
    specified)
  - pagination on `GET /team-members` (Phase 02 kept it unpaginated)
- MANAGER location scoping exists **only** in
  `locations.services.accessible_locations`. Views and later phases
  call it; they never re-filter by assignment inline.
- An unassigned MANAGER and a cross-merchant id both return `404`,
  never `403`, on `/locations/{id}`.
- The three-way invariant (`_assert_same_merchant`) runs explicitly
  before every `TeamMemberLocation` insert. Every insert goes through
  `set_team_member_locations`: the seed command and tests included,
  with no direct `TeamMemberLocation.objects.create` outside tests of
  the RLS layer itself.
- Every `TeamMemberLocation` write, and the assignment clearing in
  `change_team_member_role`, happens after `_lock_team` in the same
  `tenant_atomic()`. The actor's role is re-checked after the lock.
- Assignment `422` bodies must be identical for an unknown id and a
  cross-merchant id.
- Audit metadata holds location UUIDs only. There is no audit row for
  a no-op `PUT`, and none for location create/update/deactivate.
- `DELETE /locations/{id}` never deletes rows. It only flips
  `is_active`.
- CSRF is enforced on `POST /locations`, `PATCH`/`DELETE
  /locations/{id}` and `PUT /team-members/{id}/locations`. Tests use
  `APIClient(enforce_csrf_checks=True)`.
- `seed_dev` refuses to run when `DEBUG` is false, and never hardcodes
  or logs a password.

## Definition of done
- [ ] `python manage.py check` is clean.
- [ ] `python manage.py makemigrations --check --dry-run` reports no
      changes. `python manage.py migrate` applies `locations`
      0001–0002 and `accounts` 0004–0005 on a fresh database.
- [ ] `pytest` passes, including all Phase 00, 01 and 02 tests, with
      the one Phase 02 test updated as described in "Files to change".
- [ ] **Location CRUD:**
  - [ ] OWNER and ADMIN can `POST` (`201`, `is_active: true`), `GET`,
        `PATCH` and `DELETE` (`204`).
  - [ ] A missing or blank `name` or an invalid `timezone` returns
        `422` in the standard shape.
  - [ ] `DELETE` sets `is_active=False`, and a second `DELETE` returns
        `204` again.
  - [ ] After `DELETE`, the row and its `TeamMemberLocation` rows still
        exist, and the location still appears in `GET /locations` with
        `is_active: false`.
  - [ ] `PATCH` with `null` clears `address`, `phone` and `timezone`.
        `is_active` in a `PATCH` body is ignored.
- [ ] **Pagination:**
  - [ ] `GET /locations` returns `{results, next_cursor}` and a default
        page of 25.
  - [ ] `?limit=` works, and values over 100 are clamped to 100.
  - [ ] Following `next_cursor` returns the next page with no
        duplicates or gaps. The last page has `next_cursor: null`.
- [ ] **Permissions:**
  - [ ] VIEWER can `GET` the list and the detail, and gets `403` on
        `POST`, `PATCH` and `DELETE`.
  - [ ] MANAGER gets `403` on `POST` and `DELETE`.
  - [ ] Unauthenticated requests get `403` on every endpoint.
- [ ] **MANAGER scoping** ("MANAGER cannot access unassigned
      locations"):
  - [ ] A MANAGER assigned to L1 only sees only L1 in `GET /locations`.
  - [ ] That MANAGER gets `200` on `GET` and `PATCH /locations/{L1}`.
  - [ ] That MANAGER gets `404` on `GET` and `PATCH /locations/{L2}`,
        and L2 is unchanged.
  - [ ] A MANAGER with no assignments gets an empty list.
- [ ] **Tenant isolation, API:** Merchant A's OWNER gets `404` on
      `GET`, `PATCH` and `DELETE /locations/{B's id}`, and B's row is
      unchanged. `GET /locations` never includes B's rows.
- [ ] **Principal-derived merchant:** `POST /locations` with B's
      `merchant_id` in the body creates the location in A only.
- [ ] **Tenant isolation, both layers (`Multi-Tenancy.md` Required
      Tests 1–2):** for `locations_location` and
      `accounts_teammemberlocation`:
  - [ ] `TenantScopedManager` raises without a tenant context and
        returns only the current merchant's rows inside one.
  - [ ] A raw SQL `SELECT` in A's `tenant_atomic()` returns no B rows.
  - [ ] A raw `INSERT` or `UPDATE` with B's `merchant_id` is rejected
        by the RLS `WITH CHECK`.
  - [ ] With no merchant set, a raw query returns zero rows.

  These mirror `accounts/tests/test_rls.py`.
- [ ] **Assignment (`PUT /team-members/{id}/locations`):**
  - [ ] OWNER and ADMIN can set a MANAGER's locations (`200`, the body
        has `location_ids`).
  - [ ] Re-sending the same set changes nothing and writes no audit
        row.
  - [ ] An empty list clears the set.
  - [ ] Duplicate ids collapse to one row.
  - [ ] A pending (not yet accepted) MANAGER can be assigned.
  - [ ] MANAGER and VIEWER actors get `403`.
  - [ ] A non-MANAGER target (OWNER, ADMIN or VIEWER) gets `422`, and
        no rows are written.
  - [ ] A malformed UUID gets `422`.
  - [ ] An unknown member id, or B's member id, gets `404`.
- [ ] **Cross-tenant location assignment (Testing-Strategy priority
      scenario; `Multi-Tenancy.md` Required Test 6):**
  - [ ] Via the API, `PUT` with B's location id gets `422`. The body is
        identical to the one for a random unknown UUID, and no
        `TeamMemberLocation` row is created.
  - [ ] Via the service, `_assert_same_merchant` called directly with
        a MANAGER from A and a `Location` whose `merchant_id` is B
        raises `ValidationError` before any insert. This test does not
        go through the scoped read, so it proves the explicit
        invariant, not just RLS.
- [ ] **Uniqueness:** a direct duplicate
      `(team_member, location)` insert raises `IntegrityError`
      (`uniq_teammemberlocation_member_location`).
- [ ] **Concurrency:** two concurrent `set_team_member_locations`
      calls for the same member end in exactly one call's set, with no
      `IntegrityError`.
- [ ] **Role change and revoke:**
  - [ ] Changing a MANAGER to VIEWER deletes their assignments, and a
        later change back to MANAGER has `location_ids: []`.
  - [ ] A role change between two non-MANAGER roles touches no
        assignment rows.
  - [ ] Revoking a MANAGER deletes their assignment rows (CASCADE)
        and leaves the locations intact.
- [ ] **Member body:**
  - [ ] Every `/team-members` response includes `location_ids`. It is
        `[]` for non-MANAGERs.
  - [ ] `GET /team-members` has no N+1 queries: the query count does
        not grow with the member count.
- [ ] **Audit:**
  - [ ] A changed assignment set writes exactly one
        `team_member.locations_changed` row in the actor's merchant,
        with `actor_user`, `target` = the member, and metadata
        `{"location_ids": [...]}` holding UUIDs only.
  - [ ] No row is written for a no-op, a failed call, or any
        `/locations` call.
  - [ ] If `record()` raises, the assignment change is rolled back.
- [ ] **CSRF:** a missing CSRF token returns `403` on every new
      state-changing endpoint.
- [ ] **Seed command:**
  - [ ] With `DEBUG=True`, `python manage.py seed_dev --password <pw>`
        creates one ACTIVE merchant with an OWNER, two active
        locations, and an accepted MANAGER assigned to exactly the
        first location.
  - [ ] Both users can log in through `POST /auth/login`, and the
        MANAGER's `GET /locations` returns one location.
  - [ ] A second run exits `0` and creates nothing new.
  - [ ] With `DEBUG=False`, it raises `CommandError` and creates
        nothing.
  - [ ] Without `--password`, it prints a generated password.
- [ ] Every error response matches `{error: {code, message,
      field_errors?}}`.
- [ ] Docs updated per "Files to change": `API-Specification.md`
      §Locations and §Team, `Audit-Logging.md`, and
      `Development-Setup.md` §"Seed Data".
