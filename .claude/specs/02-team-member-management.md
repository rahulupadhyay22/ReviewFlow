# Spec: Team Member Management

## Overview
Adds merchant team management on top of the Phase 02 account
foundation. OWNERs and ADMINs can list team members, invite new ones by
email, change a member's role and revoke a member. Every successful
state-changing team-management action writes an `AuditLog` row; no-op
role changes do not. An invite is delivered as a one-time signed link that the
inviter shares themselves; there is no email infrastructure in V1's
stack. The invitee accepts through a new public
`POST /auth/accept-invite` endpoint. A brand-new user sets their password
there; an existing user confirms with their current password. This is
the second Phase 02 spec. It is shared foundation for all three planes
(ingestion, automation, intelligence): every later role-gated feature
assumes a merchant can have more than one person with a role. Optional
TOTP 2FA is the remaining Phase 02 work and gets its own spec.

Decisions settled with the user on 2026-09-22, when this spec was
scoped:
- **Invite delivery:** share a link, no email. `POST /team-members`
  returns a signed `invite_token`, and the frontend builds the link.
- **TOTP 2FA:** a separate spec (`02-totp-2fa`).
- **Audit writes:** explicit `auditlog.services.record()` calls from
  services, with no AuditLog middleware. The roadmap, SAD and CLAUDE.md
  wording is updated to match.

## Source docs
- `docs/ROADMAP.md`: §"02 — Accounts, roles & audit log"
- `docs/04-api/API-Specification.md`: preamble (base path, `merchant_id`
  from principal), §Auth, §Team, §Locations (`GET /locations/{id}`:
  cross-tenant → `404`, never `403`), §"General Conventions" (error
  shape)
- `docs/04-api/Authentication.md`: §1 (Dashboard Users: session + CSRF)
- `docs/02-architecture/Security-Architecture.md`: §Authorization (role
  table, DRF permission classes), §"Threat Model Highlights" (IDOR)
- `docs/01-product/PRD.md`: §7 (User Roles)
- `docs/02-architecture/Multi-Tenancy.md`: §Layer 1, §Layer 2
  (`self_membership` policy, `user_lookup_atomic`), §"Required Tests"
- `docs/03-database/Data-Dictionary.md`: §User, §TeamMember
- `docs/03-database/Database-Design.md`: §User (a login identity
  independent of any one merchant), §TeamMember
- `docs/09-security/Audit-Logging.md`: §"What Gets Logged" (team member
  invited / role changed / removed), §"Access to Audit Logs"
- `docs/09-security/Security-Controls.md`: §"Permissions & Access"
- `docs/02-architecture/SAD.md`: §3 (`accounts`, `auditlog`)
- `docs/10-development/Coding-Standards.md`: §1, §2, §5, §6
- `docs/10-development/Testing-Strategy.md`: §"Tenant Isolation",
  Permission tests, API tests, Security tests, Database constraint tests
- `.claude/specs/02-merchant-account-foundation.md`: the "Roadmap Phase"
  remaining work and the Phase 03 exclusions

## Depends on
- Phase 00, Project scaffold (Done)
- Phase 01, Core tenancy & RLS (Done): `tenant_context`,
  `tenant_atomic`, `user_lookup_atomic`, `TenantScopedManager`,
  `TenantMiddleware`
- Spec `02-merchant-account-foundation` (merged in #3): `User`,
  `Merchant`, `TeamMember` with the `self_membership` RLS policy,
  `TeamMemberManager.for_lookup_user`, session login,
  `IsMerchantMember` / `IsOwnerOrAdmin`, `core.api.exception_handler`,
  `auditlog.services.record`

## Roadmap Phase
- Phase: 02 — Accounts, roles & audit log
- Completes entire phase: No
- If No, remaining phase work: optional TOTP 2FA (spec `02-totp-2fa`:
  new packages `pyotp` + `cryptography`, Fernet-encrypted User TOTP
  secret, enroll/verify endpoints, second login step). With this spec,
  the "AuditLog model + middleware" bullet is complete through
  explicit `record()` calls (user decision above).

## Locked decisions touched
- Shared schema, `merchant_id` scoping (`Multi-Tenancy.md` §Model) —
  DEPENDS ON
- `TenantScopedManager` as primary application-layer scoping
  (`Multi-Tenancy.md` §Layer 1) — DEPENDS ON
- Transaction-local `SET LOCAL app.current_merchant_id`, RLS on every
  tenant table (`FINAL-ARCHITECTURE-REVIEW.md` §8) — DEPENDS ON
- `self_membership` SELECT-only policy + `user_lookup_atomic` for
  pre-tenant membership reads (`Multi-Tenancy.md` §Layer 2) — DEPENDS ON
  (accept-invite reuses it read-only; no new policy)
- No standing privileged role / no `BYPASSRLS` (`Multi-Tenancy.md` §No
  Standing Privileged Role) — NO CHANGE
- Three authentication mechanisms, never mixed; dashboard = session +
  CSRF (`Authentication.md`) — DEPENDS ON
- Roles OWNER/ADMIN/MANAGER/VIEWER via DRF permission classes
  (`Security-Architecture.md` §Authorization) — DEPENDS ON
- `TeamMemberLocation` explicit through-model (`Multi-Tenancy.md`) — NO
  CHANGE (Phase 03, not built here)
- Externally exposed IDs are UUIDs (`Security-Architecture.md` §Threat
  Model) — DEPENDS ON
- Privileged actions write an `AuditLog` row (`Audit-Logging.md`) —
  DEPENDS ON

None of these is a LOCKED DECISION CHANGE. The removal of "middleware"
from the `auditlog` description is a wording sync. The user confirmed
it, and `Audit-Logging.md` never required middleware.

## Django apps
- `accounts`: **touched**. Team services, serializers, views, URLs and
  exceptions.
- `auditlog`: **used**. Called through `record()` only; the code does
  not change.
- `config`: **touched**. Adds a throttle rate.

## Models & database changes
No database changes.

`TeamMember` already has every field this spec needs: `role`,
`invited_at`, `accepted_at`, and `UNIQUE(merchant, user)`. Its RLS is
already `tenant_isolation` + `self_membership`. No invite-token column
is added:
- `TimestampSigner` `max_age` gives expiry.
- `accepted_at IS NULL` is the single-use guard.
- Matching `invited_at` in the payload means a re-invite kills older
  links.
- Revoking deletes the row, which kills the link.

Idempotency and concurrency:
- `UNIQUE(merchant, user)` (`uniq_teammember_merchant_user`) is the
  duplicate-membership guard. Catch `IntegrityError`; never
  check-then-insert.
- Every team mutation (invite, role change, revoke, accept) first runs
  `Merchant.objects.select_for_update().get(pk=current_merchant_id)`
  inside its `tenant_atomic()`. That serializes team changes per
  merchant, so the last-OWNER rule holds when two requests race.
  `Merchant` is GLOBAL with no RLS, so the lock is legal in tenant
  context.

## API endpoints
All endpoints are under `/api/v1/`. `merchant_id` always comes from the
session (or, for accept, from the `TeamMember` row that the verified
token points to). It is never taken from request data. Every error uses
`{ "error": { "code", "message", "field_errors"? } }`.

Member body (`TeamMemberSerializer`):
`{ id, user: { id, email }, role, invited_at, accepted_at }`.
There is no `locations` field; it arrives in Phase 03.

- `GET /team-members`: list the current merchant's members (pending and
  accepted), ordered by `created_at`. Returns `200 { results: [member] }`.
  Session auth. OWNER, ADMIN.
  - No pagination. `# ponytail:` teams are small; add the cursor
    pagination used by `/locations` if teams grow.
  - **Resolved: OWNER and ADMIN only.** MANAGER and VIEWER get `403`.
    `API-Specification.md` §Team gets an explicit `Permissions` line
    under `GET /team-members` so the ambiguity is gone.
- `POST /team-members` (invite): `{ email, role }`, where `role` is one
  of `OWNER`, `ADMIN`, `MANAGER`, `VIEWER`. Returns
  `201 { ...member, invite_token }`. Session auth. OWNER, ADMIN.
  - The response shape is identical whether or not the email already
    had a `User` elsewhere, so there is no global-account enumeration.
  - Re-inviting a **still-pending** member refreshes `invited_at` and
    `role` and returns a new `invite_token` (`201`); older links stop
    working.
  - `409 already_member` if the email is already an accepted member of
    this merchant.
  - `403 team_permission_denied` if an ADMIN invites with `role=OWNER`.
  - `422` for an invalid email or role.
- `PATCH /team-members/{id}` (role change): `{ role }`. Returns `200`
  with the member body. Session auth. OWNER, ADMIN.
  - `404` if `{id}` is not in the current merchant (never `403`).
  - `403 team_permission_denied` if the target is the actor's own
    membership, or an ADMIN targets an OWNER or grants `OWNER`.
  - The last accepted OWNER is protected by the `403` self-target rule
    above: only an accepted OWNER can target another OWNER, so an OWNER
    always remains. No legitimate request returns `409 last_owner`.
  - `422` for an invalid role.
- `DELETE /team-members/{id}` (revoke): returns `204`. Hard-deletes the
  `TeamMember` row, pending or accepted. The `User` is kept. Session
  auth. OWNER, ADMIN.
  - Same `404` / `403` rules as `PATCH` (these also protect the last
    OWNER).
  - The revoked member loses access on their next request, because
    `IsMerchantMember` re-checks membership every request.
- `POST /auth/accept-invite`: `{ token, password }`. Returns `204`; the
  invitee then logs in through `POST /auth/login`. Public, CSRF
  enforced, per-IP throttle `invite_accept`. **New endpoint**, added to
  `API-Specification.md` §Auth.
  - `400 invalid_invite`, one generic body for every token failure:
    bad signature, expired, revoked (row gone), already accepted,
    superseded by a re-invite, merchant not `ACTIVE`, or wrong current
    password for an existing user. No enumeration.
  - `422 validation_error` only when a new user's password fails
    `validate_password`.
  - `403` when the CSRF token is missing. `429` when throttled.

### Invite link transport
The `invite_token` is a credential. Its transport is fixed as follows:
- **URL fragment, never a query parameter.** The frontend builds the
  link as `https://<frontend>/accept-invite#token=<invite_token>`.
  Browsers do not send the fragment in the navigation request, so the
  token never reaches server access logs, CDN/WAF logs or `Referer`
  headers.
- **Token in the POST body only.** The accept page reads
  `location.hash`, removes it from the address bar
  (`history.replaceState`), and sends the token only in the
  `POST /auth/accept-invite` JSON body. The API never accepts the token
  from a query string or header.
- **Strip the token before anything else sees the URL.** The
  accept-invite page removes the token from `location.hash` immediately,
  before any analytics, telemetry, error-reporting or other third-party
  script can observe the URL.
  - Never send the token to analytics, telemetry or error-reporting.
  - The page must stay compatible with the application's CSP/security
    policy.
  - No new analytics or reporting system is added by this spec.
- **HTTPS required in production.** The frontend and the API are served
  only over HTTPS in production (Cloudflare "Always Use HTTPS"), and the
  frontend builds invite links with `https://` only. The existing
  `CSRF_COOKIE_SECURE` / `SESSION_COOKIE_SECURE` (`DJANGO_SECURE_COOKIES`,
  default `True`) already mean the CSRF cookie is never set over plain
  HTTP, so `POST /auth/accept-invite` cannot pass CSRF there. Only local
  development sets `DJANGO_SECURE_COOKIES=False`.
- **CSRF bootstrap.** The accept page is public, with no session.
  1. `GET /auth/login` sets the `csrftoken` cookie (`204`).
  2. `POST /auth/accept-invite` sends that value as `X-CSRFToken`.

  In this flow, `GET /auth/login` is used only to obtain the CSRF
  cookie. It does not authenticate the invitee or create a login
  session. No new CSRF endpoint is added. Document this sequence in
  `API-Specification.md` §Auth under `POST /auth/accept-invite`.

## Services & background tasks

### `accounts/exceptions.py` (additions)
All are `ReviewFlowError` subclasses with `http_status` and `code`, so
`core.api.exception_handler` maps them.
- `InvalidInvite`: `400`, `invalid_invite`. Generic message.
- `AlreadyMember`: `409`, `already_member`
- `LastOwner`: `409`, `last_owner`
- `TeamPermissionDenied`: `403`, `team_permission_denied`

### `accounts/services.py` (additions)
All of these except `accept_invite` require a tenant context. `actor` is
the acting `TeamMember` (`request.team_member`).

- `list_team_members() -> QuerySet[TeamMember]`:
  `TeamMember.objects.select_related("user").order_by("created_at")`.
- `invite_team_member(*, actor, email, role) -> tuple[TeamMember, str]`:
  returns the member and its `invite_token`.
  - Normalizes the email with `UserManager.normalize_email`.
  - Raises `TeamPermissionDenied` if `role == OWNER` and
    `actor.role != OWNER`.
  - Inside `tenant_atomic()`:
    1. Lock the `Merchant` row.
    2. Get the `User` by email, or create it with
       `User.objects.create_user(email, None)`. That gives an unusable
       password with `is_active=True`; the unusable password already
       blocks login until accept.
    3. Create the `TeamMember` with `invited_at=now` and
       `accepted_at=None`, inside a savepoint.
    4. On `IntegrityError` (`uniq_teammember_merchant_user`), re-read the
       existing row. If accepted, raise `AlreadyMember`. If pending,
       update `role` and `invited_at=now`.
    5. Call `auditlog.record("team_member.invited", actor=actor.user,
       target=member, metadata={"role": role})`.
  - Returns `(member, make_invite_token(member))`.
  - **Concurrent invite semantics.** Invites of the same email are
    serialized by the `Merchant` row lock.
    - Every successful invite or re-invite writes exactly one
      `team_member.invited` row. N successful calls give N rows.
    - The final state has exactly one `TeamMember` for that
      `(merchant, user)`.
    - Only the token from the most recent successful call is valid.
      Each call sets a new `invited_at`, and accept requires the
      token's `iat` to equal the stored `invited_at`.
    - A call that fails (`AlreadyMember`, `TeamPermissionDenied`, `422`)
      writes no audit row.
- `change_team_member_role(*, actor, member_id, role) -> TeamMember`:
  - Inside `tenant_atomic()`, lock the `Merchant` row, then
    `TeamMember.objects.select_for_update().get(pk=member_id)`.
    A missing id raises `TeamMemberNotFound` (`404`).
  - Apply `_check_can_manage(actor, target, new_role=role)`. Then run
    the defensive `_would_orphan_owners` check (raises `LastOwner`).
    It is unreachable under the current rules and is kept only as
    invariant protection.
  - Save `role`, then call `record("team_member.role_changed",
    actor=actor.user, target=member, metadata={"role_from": old,
    "role_to": role})`.
  - A no-op (same role) returns the member without writing an audit row.
- `revoke_team_member(*, actor, member_id) -> None`:
  - Same lock, `404` and `_check_can_manage` flow, plus the same
    defensive `_would_orphan_owners` check.
  - Calls `record("team_member.removed", actor=actor.user, target=member,
    metadata={"role": member.role})` **before** `member.delete()`, in
    the same transaction.
- `_check_can_manage(actor, target, *, new_role=None)`: raises
  `TeamPermissionDenied` if any of these holds:
  - `target.pk == actor.pk` (no self role change or self revoke)
  - `actor.role != OWNER` and either `target.role == OWNER` or
    `new_role == OWNER` (only an OWNER touches OWNER)
- `make_invite_token(member) -> str`:
  `TimestampSigner(salt="accounts.invite").sign_object({"tm":
  str(member.pk), "u": str(member.user_id), "iat":
  member.invited_at.isoformat()})`. It carries UUIDs and a timestamp
  only, never the email.
- `accept_invite(*, token, password) -> None`:
  1. `unsign_object(token, max_age=INVITE_MAX_AGE)`, where
     `INVITE_MAX_AGE = timedelta(days=7)` is a module constant.
     `BadSignature`, `SignatureExpired` or a malformed payload raises
     `InvalidInvite`.
  2. **Read**, with no merchant context. Inside
     `user_lookup_atomic(user_id)`, read
     `TeamMember.objects.for_lookup_user(user_id).filter(id=tm,
     accepted_at__isnull=True, merchant__status=ACTIVE)` to get
     `merchant_id`. This is visible through the existing
     `self_membership` SELECT policy. Exit the lookup transaction
     before step 3 (`user_lookup_atomic` must never nest with a merchant
     context).
  3. **Write**, in `tenant_context(merchant_id)` + `tenant_atomic()`:
     - Lock the `Merchant` row. `select_for_update()` the `TeamMember`
       and re-check that `accepted_at IS NULL` and `invited_at` matches
       `iat`.
     - The password branch depends only on
       `user.has_usable_password()`, not on whether the `User` row was
       created by this invite:
       - **Unusable password.** This is either a new invitee, or an
         existing `User` created by an earlier invite (from any
         merchant) that was never accepted. Treat it as an
         uninitialized invite account: run
         `validate_password(password, user)` (`ValidationError` →
         `422`), then `set_password` and save.
       - **Usable password (an existing account).**
         `user.check_password(password)` must be true, else
         `InvalidInvite`. **Never change an existing user's password
         here.** Otherwise any merchant's OWNER/ADMIN could invite a
         known email and take over that account through the link they
         receive.
     - Known limit (no scope change): an uninitialized invite account
       is initialized by whichever of its pending invites is accepted
       first.
     - Set `accepted_at=now` and save.
     - **Then** call `record("team_member.accepted", actor=member.user,
       target=member)`. `record()` requires the actor to hold an
       accepted membership, so it must come after `accepted_at` is set.
  - Expected invite-state/security failures raise `InvalidInvite`.
    Unexpected exceptions, database errors, transaction failures, and
    programming errors must propagate to the standard 500 error handling
    path. Do not catch generic `Exception`.

### Canonical audit actions
These four strings are canonical. Use them verbatim (a module-level
constant in `accounts/services.py`), and never add variants:

| Action | Written by | Metadata |
|---|---|---|
| `team_member.invited` | `invite_team_member` (every successful invite or re-invite) | `{"role"}` |
| `team_member.role_changed` | `change_team_member_role` (not on a no-op) | `{"role_from", "role_to"}` |
| `team_member.removed` | `revoke_team_member` | `{"role"}` |
| `team_member.accepted` | `accept_invite` | none |

### Views and permissions
- `TeamMemberListView` (`GET`, `POST`) and `TeamMemberDetailView`
  (`PATCH`, `DELETE`): `permission_classes = [IsOwnerOrAdmin]`. They are
  thin: serializer validation, then a service call.
- `AcceptInviteView`: mirrors `LoginView` exactly:
  - `@method_decorator(csrf_protect, name="dispatch")`
  - `authentication_classes = []`
  - `permission_classes = [AllowAny]`
  - throttle scope `invite_accept`
- No new permission classes. `_check_can_manage` holds the target-aware
  rules, because a DRF permission class cannot see the target's role
  before the locked read.

### Tasks, adapters and schedules
- No Celery tasks (no email is sent)
- No Beat schedules
- No adapters or providers

## Admin
No admin changes. `TeamMember` and `AuditLog` stay unregistered until the
Phase 16 audited privileged path exists.

## Files to change
- `accounts/services.py`: the team and invite services above
- `accounts/exceptions.py`: `InvalidInvite`, `AlreadyMember`,
  `LastOwner`, `TeamPermissionDenied`
- `accounts/serializers.py`: `TeamMemberSerializer`,
  `TeamMemberInviteSerializer` (`email`, `role`),
  `TeamMemberRoleSerializer` (`role`), `AcceptInviteSerializer`
  (`token`, `password` with `trim_whitespace=False`)
- `accounts/views.py`: `TeamMemberListView`, `TeamMemberDetailView`,
  `AcceptInviteView`, `InviteAcceptRateThrottle`
- `accounts/urls.py`: `team-members`, `team-members/<uuid:pk>`,
  `auth/accept-invite`
- `config/settings.py`: add `"invite_accept": "5/min"` to
  `DEFAULT_THROTTLE_RATES`
- `docs/04-api/API-Specification.md`:
  - §Auth: add `POST /auth/accept-invite`, including:
    - the CSRF bootstrap (`GET /auth/login` first)
    - the fragment-only link format (`#token=`)
    - token in the POST body only
    - HTTPS required in production
  - §Team:
    - an explicit `Permissions: OWNER, ADMIN` line under
      `GET /team-members`
    - request/response bodies and the `invite_token` field
    - the re-invite, self, OWNER and last-OWNER rules
    - the error codes
- `docs/ROADMAP.md` §02: change the "`AuditLog` model + middleware"
  bullet to "`AuditLog` model + explicit `record()` from services".
  Bullet wording only; the Status column and phase state are not
  touched.
- `docs/02-architecture/SAD.md` §3: change the `auditlog/` comment to
  `# AuditLog model + record() service`
- `CLAUDE.md`, Project Structure block: the same `auditlog/` wording
  change. Do not touch the Current State snapshot.

## Files to create
Tests only, written by `/test-feature`:
- `accounts/tests/test_team_services.py`
- `accounts/tests/test_team_api.py`
- `accounts/tests/test_invite_accept.py`

## New dependencies
No new dependencies. The invite token uses `django.core.signing`
(stdlib Django).

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
- **Do not build these (Phase 03):**
  - `TeamMemberLocation`
  - `PUT /team-members/{id}/locations`
  - the `locations` field on `GET /team-members`

  A MANAGER invited now has no location assignment until Phase 03. Allow
  MANAGER invites; do not block them.
- **Do not build these (spec `02-totp-2fa`):** TOTP and any User 2FA
  fields.
- **Do not build these:** email sending, an invite-token column, an
  AuditLog middleware, a re-send endpoint (re-invite covers it), or a
  signup endpoint.
- The `invite_token` is a credential:
  - Never log it.
  - Never put it in `AuditLog.metadata_json`, exception messages or
    `__str__`.
  - Never log invitee emails or passwords.
  - Audit metadata holds roles only.
  - The token travels only in a URL fragment (`#token=`) and the
    `POST /auth/accept-invite` body. It never travels in a query
    parameter or a header, and never over plain HTTP in production.
- Audit action strings are exactly the four canonical ones in
  "Canonical audit actions".
- `accept_invite` never modifies the password of a `User` that already
  has a usable password.
- `accept_invite` reads through `user_lookup_atomic` +
  `for_lookup_user` only. It writes only inside
  `tenant_context(merchant_id)` + `tenant_atomic()`, where
  `merchant_id` comes from the row that the verified token points to.
  Never nest the two. Never add a write policy keyed on
  `app.current_user_id`.
- Every team mutation locks the `Merchant` row first, inside the same
  `tenant_atomic()`. The last-OWNER check counts accepted OWNERs after
  that lock.
- Cross-tenant `{id}` returns `404` via `TenantScopedManager` (RLS
  backstop), never `403`.
- CSRF is enforced on `POST /team-members`, `PATCH`/`DELETE
  /team-members/{id}` and `POST /auth/accept-invite`. Tests use
  `APIClient(enforce_csrf_checks=True)`.
- The audit row is written in the same transaction as the change. If
  `record()` fails, the change rolls back.

## Definition of done
- [ ] `python manage.py check` is clean.
- [ ] `makemigrations --check --dry-run` reports no changes.
- [ ] `pytest` passes, including all Phase 00, 01 and
      `02-merchant-account-foundation` tests.
- [ ] **Permissions:**
  - [ ] `GET`, `POST`, `PATCH` and `DELETE` on the team endpoints
        succeed for OWNER and ADMIN.
  - [ ] Each returns `403` for MANAGER and VIEWER.
  - [ ] Each returns `403` when unauthenticated.
- [ ] `GET /team-members` returns only the current merchant's members,
      pending and accepted, with no `locations` field.
- [ ] **Invite:**
  - [ ] A new email creates a `User` with an unusable password and a
        pending `TeamMember` (`invited_at` set, `accepted_at` null).
        Returns `201` with an `invite_token`.
  - [ ] An email that already has a `User` (in another merchant) reuses
        that `User`. The response shape is identical to the new-email
        case.
  - [ ] Re-inviting a pending member returns a new token and refreshes
        `role` and `invited_at`.
  - [ ] **Re-invite token rotation:** after a re-invite, accepting with
        the old token returns `400 invalid_invite` and leaves
        `accepted_at` null. Accepting with the new token then returns
        `204` and sets `accepted_at`.
  - [ ] Inviting an accepted member returns `409 already_member`, and
        there is still exactly one `TeamMember` row.
  - [ ] Two concurrent invites of the same email create exactly one
        `TeamMember` (`UNIQUE(merchant, user)`). Each successful call
        writes one `team_member.invited` row, and only the token from
        the call that committed last is accepted.
  - [ ] An ADMIN inviting with `role=OWNER` gets `403`. An OWNER
        inviting an OWNER succeeds.
  - [ ] An invalid role or email returns `422` in the standard shape.
- [ ] **Role change:**
  - [ ] An OWNER can change any other member's role.
  - [ ] An ADMIN can change ADMIN/MANAGER/VIEWER roles, but gets `403`
        targeting an OWNER or granting OWNER.
  - [ ] Changing one's own role returns `403`.
  - [ ] The last accepted OWNER can never be demoted: self role change
        returns `403`. `_would_orphan_owners` has a direct unit test.
  - [ ] Two OWNERs demoting each other concurrently leave at least one
        OWNER (the `Merchant` row lock serializes them).
- [ ] **Revoke:**
  - [ ] Returns `204` and deletes the row; the `User` still exists.
  - [ ] The revoked member's next request with their existing session
        gets `403`.
  - [ ] Revoking a pending member makes their token fail with
        `400 invalid_invite`.
  - [ ] Self-revoke returns `403`. An ADMIN revoking an OWNER returns
        `403`. The last OWNER is protected by the self-revoke `403`.
- [ ] **Tenant isolation, API:** Merchant A's OWNER sending `PATCH` or
      `DELETE /team-members/{B's member id}` gets `404`, and B's row is
      unchanged.
- [ ] **Tenant isolation, principal-derived merchant:** Merchant A's OWNER sending
      `POST /team-members` with `merchant_id` of B in the body creates
      the member in A only.
- [ ] **Accept invite:**
  - [ ] A new user with a valid token and a strong password gets `204`.
        `accepted_at` is set and the password is set. `POST /auth/login`
        then succeeds for that merchant.
  - [ ] A new user with a weak password gets `422`. `accepted_at` stays
        null and the password stays unusable.
  - [ ] **Account-takeover guard:** an existing user (usable password)
        with a valid token and the wrong password gets
        `400 invalid_invite`. Their password hash is unchanged, and
        `accepted_at` stays null.
  - [ ] An existing user with the correct current password gets `204`,
        `accepted_at` is set, and the password hash is unchanged.
  - [ ] **Existing `User` with an unusable password:** a `User` created
        by Merchant B's still-pending invite, then invited by Merchant
        A, accepts A's token with a strong password. The result is
        `204`, the password is set, and A's `accepted_at` is set. B's
        membership stays pending. The same flow with a weak password
        returns `422` and leaves the password unusable.
  - [ ] **Cross-merchant token (security):** a valid token for
        Merchant A's pending member does not accept or change any
        Merchant B membership, even when the same `User` has a pending
        B membership. After accept, only A's row has `accepted_at` set,
        B's row (role, `invited_at`, `accepted_at`) is unchanged, and no
        B audit row exists. A token whose payload is edited to point at
        B's member id (`tm`) returns `400 invalid_invite`, because the
        signature check fails.
  - [ ] A tampered token, an expired token (older than 7 days), a token
        already accepted (replay), a revoked member, a superseded token
        and a non-`ACTIVE` merchant all return the same
        `400 invalid_invite` body.
  - [ ] Accepting the same token twice results in one acceptance and
        one `team_member.accepted` audit row.
  - [ ] Missing CSRF returns `403`. The 6th rapid attempt from one IP
        returns `429`.
  - [ ] **CSRF bootstrap:** with `APIClient(enforce_csrf_checks=True)`,
        `GET /auth/login`, then `POST /auth/accept-invite` with
        `X-CSRFToken` from the cookie, succeeds.
  - [ ] A token sent only as a query parameter (`?token=`), with no body
        token, returns `422` (the token field is required) and nothing
        is accepted.
  - [ ] No `SET LOCAL app.current_user_id` is issued inside a merchant
        context (the lookup and the write are separate transactions).
- [ ] **Audit:** every successful state-changing team action writes
      exactly one `AuditLog` row in the actor's merchant. The `action` is
      exactly one of the canonical `team_member.invited`,
      `team_member.role_changed`, `team_member.removed` or
      `team_member.accepted`, with the correct `actor_user`,
      `target_type`/`target_id` and role-only metadata.
  - [ ] A failed action writes no row.
  - [ ] No row contains the invite token or an email.
  - [ ] A no-op role change writes no row.
  - [ ] If `record()` raises, the team change is rolled back.
- [ ] Every error response matches `{error: {code, message,
      field_errors?}}`.
- [ ] Docs updated per "Files to change":
  - [ ] `API-Specification.md` §Auth: accept-invite with CSRF bootstrap,
        fragment transport and HTTPS
  - [ ] `API-Specification.md` §Team: `GET` is OWNER/ADMIN
  - [ ] the ROADMAP, SAD and CLAUDE.md `auditlog` wording
