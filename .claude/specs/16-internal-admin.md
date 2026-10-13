# Spec: Internal Admin

**Status: LC-1 APPROVED by the user on 2026-10-13, with exactly the two
uses in LC-1 item 4. Successful TOTP logins and failed admin logins write
no AuditLog rows in this phase (Q1 and Q2 answered: no). Implemented on
`feature/internal-admin`.**
The plan is at `.claude/plans/16-internal-admin-plan.md`.

## Overview
This spec adds the cross-cutting internal-admin controls of Phase 16
to the existing Django Admin:

- `/admin/` is reachable only from allowlisted IPs (fail closed).
- Staff log in to admin with a password plus a mandatory TOTP or
  recovery code.
- Admin requests are never wrapped in a dashboard tenant context.
- Staff get one audited, privileged path for reading one merchant's
  tenant data at a time, with no RLS bypass.
- The first consumer of that path is a read-only dead-letter
  `IntegrationEvent` review page.

It exists now because Phases 02 and 04 are Done. Every tenant table is
deliberately unregistered in admin (`accounts/admin.py`,
`billing/admin.py`, `whatsapp/admin.py`) "until the audited Phase 16
path" exists. Dead-letter events are documented as "visible in the
admin panel for manual review" but have no viewer yet. Plane: platform
operations, supporting the ingestion plane.

### Decisions recorded for this spec
- **D1: cross-tenant mechanism (user brief, locked).**
  - Staff work on one merchant at a time and select that merchant
    explicitly.
  - Every privileged tenant-data read runs inside the existing
    `tenant_context` and `tenant_atomic`.
  - Every privileged read records an audit entry before the data is
    returned.
  - No new RLS policy, no `BYPASSRLS`, no superuser connection and no
    standing privileged database role. PostgreSQL RLS remains the final
    boundary.
  - Tenant context resets correctly on success and on exceptions.
- **D2: staff TOTP is required (user, 2026-10-13).**
  - Admin login needs the password plus a TOTP or recovery code.
  - A staff user without 2FA enabled cannot use admin.
  - Staff enroll through a management command, because
    `POST /auth/2fa/setup` needs a merchant session.
- **D3: the payload is hidden (user, 2026-10-13).** The dead-letter
  review never selects or shows `IntegrationEvent.payload`.
- **D4: scope is the four ROADMAP bullets only (user, 2026-10-13).**
  These items move to a follow-up Phase 16 spec:
  - the staff 2FA reset;
  - merchant suspension and reactivation;
  - manual quota adjustment;
  - an AuditLog admin view;
  - admin registration of other tenant models.
- **D5: an empty or invalid IP allowlist denies everyone (user,
  2026-10-13).** The client IP follows the existing
  `DJANGO_NUM_PROXIES` rule. A raw forwarding header is never trusted.
- The user's original brief was truncated after D1. Any further locked
  decisions it contained are not in this spec.

### Verification record (checked against code on `feature/internal-admin`, 2026-10-13)
Verified, with file references:
- **Platform write scopes.**
  - `core/tenancy.py` `_PLATFORM_WRITE_SCOPES == {"whatsapp_shared_pool"}`.
  - `platform_write_atomic` refuses to run inside a tenant context, is
    `durable=True`, and resets its contextvar in `finally`.
- **Platform audit writes.** `auditlog/services.py` `record_platform()`
  raises `TenantContextError` unless `get_current_platform_write()` is
  set. It inserts `merchant_id=None` through `_base_manager`, so the
  insert is still RLS-gated.
- **RLS policies** (`core/rls.py`):
  - `auditlog_auditlog` has `tenant_isolation` plus INSERT-only
    `platform_insert`:
    `merchant_id IS NULL AND app.platform_write IS NOT NULL`. Any
    non-empty scope passes it, and no read, update or delete policy
    exists for platform rows (`rls_platform_insert`;
    `auditlog/tests/test_platform_audit.py`).
  - On `whatsapp_whatsappaccount`, `platform_insert` and
    `platform_update` require `app.platform_write = 'whatsapp_shared_pool'`
    **exactly** (`_SHARED_POOL_ROW`).
  - `accounts_user` has no RLS (`accounts/models.py`: `User` is
    global).
- **Staff are rejected by `record()`.** `auditlog/services.py`
  `record()` raises `ActorNotMember` unless the actor has an accepted
  `TeamMember` row in the current merchant, so it cannot record staff
  access.
- **Replay protection exists.** `accounts/services.py` `_verify_totp`
  skips any step at or before `totp_last_used_step` and saves the
  accepted step. It runs under `_lock_user`
  (`select_for_update`). Two concurrent logins with the same code are
  serialized on the `User` row, and the second sees the updated step
  and fails.
- **Recovery codes are consumed atomically.** `_consume_recovery_code`
  removes the matched hash and saves, under the same row lock. A
  concurrent second use re-reads the row after the lock is released,
  finds no match and fails.
- **A failed verification changes no state.** `_verify_totp` saves
  only on a match. `_consume_recovery_code` saves only on a match.
- **Recovery codes are never six digits.** `_generate_recovery_codes`
  hashes `secrets.token_hex(10)`, 20 hex characters.
  `_normalize_recovery_code` strips spaces, lowercases and removes
  dashes. A `^[0-9]{6}$` input normalizes to 6 characters, so it can
  never match a recovery hash, and a TOTP code and a recovery code can
  be told apart by format alone.
- **The dashboard writes no audit row for a TOTP-code login or a
  failed attempt.** `complete_totp_login` audits only
  `user.totp_recovery_code_used`, while `confirm_totp_setup` and
  `disable_totp` audit enable and disable. `Audit-Logging.md` lists
  "enabled / disabled / a recovery code used". It does not list
  successful or failed logins.
- **The tenancy helpers.**
  - `tenant_context` resets its contextvar in `finally`.
  - `tenant_atomic()` currently takes no arguments and wraps
    `transaction.atomic()`.
  - `SET LOCAL` ends with the outermost transaction. Inside a savepoint
    it persists until that outer transaction ends, as
    `tenant_atomic`'s docstring warns.
- **Durable blocks.** In Django 5.2.17 (`requirements.txt`),
  `Atomic(durable=True)` raises `RuntimeError` when nested, except
  inside a test-case atomic (`_from_testcase`;
  `django/db/transaction.py`). The repository already relies on this:
  `core/tests/test_number_lookup.py`, `core/tests/test_platform_write.py`.
- **No request-wide transaction under admin.**
  - `ATOMIC_REQUESTS` is not set (Django defaults it to `False`).
  - A request is tenant-wrapped only by `TenantMiddleware`, and only
    when `SessionMerchantMiddleware` sets `request.merchant_id` from
    `session["merchant_id"]`, which it currently does for `/admin/`
    too.
  - Django Admin's `admin_view` is not atomic.
- **DRF client IP.** `BaseThrottle.get_ident`
  (`rest_framework/throttling.py`) reads only `request.META` and
  `request.headers`, so it works on a plain Django request:
  - `NUM_PROXIES=0` or no `X-Forwarded-For`: `REMOTE_ADDR`;
  - `NUM_PROXIES=N`: the Nth-from-last `X-Forwarded-For` entry.

  `settings.py` always sets `NUM_PROXIES` from `DJANGO_NUM_PROXIES`
  (default 0).
- **Test conventions.** Properties that need a real commit, or that
  `SET LOCAL` does not leak, are tested with
  `pytest.mark.django_db(transaction=True)` (`core/tests/test_platform_write.py`
  module docstring). Existing admin tests log in with `client.login` or
  `force_login` (`accounts/tests/test_permissions.py:79`, `:87`;
  `billing/tests/test_admin.py` `staff_client`).

Assumptions, not yet verified because they need the implementation:
- A small override of `AdminSite.login` can set the session flag after
  Django's `LoginView` has run `auth_login()`. The plan's step 4 checks
  this against Django 5.2's `AdminSite.login` source first.
- Rendering the dead-letter template after the tenant block exits is
  safe, because the template receives only plain dicts.

## Source docs
- `docs/ROADMAP.md`, §16 and the phase table (depends on 02 and 04).
- `docs/02-architecture/Multi-Tenancy.md`:
  - §Layer 1 and §Layer 2;
  - §"No Standing Privileged Role";
  - §"Shared WhatsApp sender" (`platform_write_atomic`,
    `record_platform`).
- `docs/02-architecture/Architecture.md`, §"Foundational Decisions"
  item 6.
- `docs/02-architecture/SAD.md`, §3.
- `docs/FINAL-ARCHITECTURE-REVIEW.md`, §1 and §8.
- `docs/09-security/Security-Controls.md`, §"Privileged Admin
  Operations", §"Permissions & Access" and §"Row-Level Security".
- `docs/09-security/Audit-Logging.md`, §"What Gets Logged" and
  §"Access to Audit Logs".
- `docs/04-api/Authentication.md`, §1 (TOTP).
- `docs/06-automation/Event-Processing.md` (Retry cap exceeded →
  `DEAD_LETTER`).
- `docs/04-api/Webhook-Specification.md`, §Retry.
- `docs/03-database/Database-Design.md` and `Data-Dictionary.md`,
  §IntegrationEvent and §AuditLog.
- `docs/10-development/Testing-Strategy.md`, §"Tenant Isolation" and
  §RLS (pooled connections).
- `docs/10-development/Coding-Standards.md`, §1, §2, §5 and §7.
- `.claude/specs/02-totp-2fa.md` ("Out of scope (Phase 16)").

## Depends on
- Phase 02 (Done):
  - `User.is_staff` and the TOTP fields;
  - the `accounts.services` TOTP helpers;
  - `AuditLog`, `record()` and `record_platform()`;
  - `UserAdmin` and `MerchantAdmin`.
- Phase 04 (Done): `IntegrationEvent`, `Status.DEAD_LETTER` and
  `SAFE_ERRORS`.
- Phase 01: `core.tenancy`.

## Roadmap Phase
- Phase: 16 — Internal admin (Django Admin)
- Completes entire phase: No
- Remaining phase work: the follow-up Phase 16 spec chosen in D4:
  - the staff 2FA reset;
  - merchant suspension and reactivation;
  - manual quota adjustment;
  - a per-merchant AuditLog view;
  - admin views for the tenant models left unregistered by Phases 02,
    07 and 08.

## Locked decisions touched
- Shared schema, `merchant_id` scoping and `TenantScopedManager`
  (`Multi-Tenancy.md` §Layer 1): DEPENDS ON.
- PostgreSQL RLS with transaction-local `SET LOCAL`
  (`FINAL-ARCHITECTURE-REVIEW.md` §8): DEPENDS ON. No policy is added or
  changed.
- No standing privileged role, and cross-tenant access only through an
  audited privileged path (`Multi-Tenancy.md` §"No Standing Privileged
  Role"): DEPENDS ON. This spec builds the path.
- Customized Django Admin, no bespoke admin app (`Architecture.md`
  §Foundational 6; `SAD.md` §3): DEPENDS ON.
- `IntegrationEvent.merchant_id` is resolved before creation
  (`FINAL-ARCHITECTURE-REVIEW.md` §1): NO CHANGE.
- The four lookup policies, `self_membership` and the shared-sender
  policies: NO CHANGE.
- `platform_write_atomic`'s single scope, sole caller and "never from
  admin" rule (`Multi-Tenancy.md` §"Shared WhatsApp sender";
  `core/tenancy.py` docstring): LOCKED DECISION CHANGE.

LOCKED DECISION CHANGE — USER SIGN-OFF REQUIRED

### LC-1: `staff_auth` platform-write scope. Status: **APPROVED 2026-10-13** (user sign-off, as narrowed in item 4).

**1. Why staff auth audit rows need platform-level writes.**
`Audit-Logging.md` requires "2FA enabled / a recovery code used" to be
logged. A staff user acting in admin has no merchant, so the row must
have `merchant_id` NULL.

- `record()` cannot write it: it needs a tenant context and a
  `TeamMember` actor.
- An insert of a NULL-merchant row passes RLS only under the
  `platform_insert` policy, which needs `app.platform_write` set. That
  is set only by `platform_write_atomic(scope)`, whose allowlist is
  `{"whatsapp_shared_pool"}`.
- Borrowing that scope would let staff-auth code satisfy the shared
  sender's write policies. That is why a separate scope is proposed.

**2. Support without policy changes: yes (verified).** The audit
`platform_insert` policy accepts any non-empty `app.platform_write` for
a NULL-merchant row. No migration and no new policy are needed.

**3. `staff_auth` cannot touch the shared WhatsApp sender.**
- That table's `platform_insert` and `platform_update` require the
  literal `'whatsapp_shared_pool'`, and there is no platform DELETE
  policy.
- Inside a `staff_auth` block no merchant context is set (the helper
  refuses to run in one), so every tenant write policy fails too.
- A test asserts that an insert and an update of the shared row inside
  `platform_write_atomic("staff_auth")` both fail.

**4. The only permitted callers** (enforced by a test that greps the
codebase for `platform_write_atomic("staff_auth"` call sites):
- `accounts.services.confirm_staff_totp_setup`;
- `accounts.services.verify_staff_admin_login`, and only for the
  **recovery-code branch**. A six-digit TOTP code is verified in a plain
  `transaction.atomic()` and never opens a `staff_auth` block. This is
  safe because a six-digit input can never match a recovery hash
  (verification record).

Nothing else may use `staff_auth`: no view, task, tenant service or
`ModelAdmin`.

**5. If the audit write fails:**
- The audit insert runs in the **same** `platform_write_atomic`
  transaction as the state change it records.
- If enrollment confirmation's audit fails, everything rolls back. 2FA
  stays not enabled, and the command exits non-zero with a fixed
  message.
- If the recovery-code audit fails, the code's consumption rolls back,
  so the code stays valid and the login fails with the generic error.
  There is no state where a code is consumed but not audited.

**6. Which events are audited:**

| Event | AuditLog row? | Why |
|---|---|---|
| Staff 2FA enrollment confirmed | Yes: `user.totp_enabled`, platform row, no metadata | Required by `Audit-Logging.md` |
| Recovery code used at admin login | Yes: `user.totp_recovery_code_used`, metadata `{recovery_codes_remaining}` | Required by `Audit-Logging.md` |
| Successful TOTP-code admin login | **No** (proposal) | Not listed in `Audit-Logging.md`. The dashboard does not audit it either. Auditing it would put `staff_auth` on every login. See open question Q1 |
| Failed admin login (password or second factor) | **No AuditLog row** (proposal) | Not listed in the docs, and the dashboard does not audit it. Bounded by the IP allowlist and the throttle. See Q2 |
| Enrollment begin (secret issued) | No | Nothing is enabled yet, the same as dashboard `begin_totp_setup` |

No row ever contains a code, secret, `otpauth` URI or recovery hash.

**7. Failed-attempt auditing would need a separate design.** If Q2 is
later answered yes:

- The audit cannot be written in the verification transaction, because
  a failure must not commit partial state there.
- The safe shape is a **separate**, later `staff_auth` transaction that
  writes only the audit row, after the verification transaction has
  rolled back or finished with no change.
- The verification path itself never writes anything on failure
  (verified).
- That widens LC-1 to every failed attempt, so it is not proposed here.

**Recommendation:** approve LC-1 as narrowed in item 4 (two callers, the
recovery branch only).

**If LC-1 is not approved.**
- Still implementable:
  - the IP allowlist;
  - the middleware exemption;
  - `staff_merchant_read` and the dead-letter page;
  - the admin site gate;
  - admin login with **TOTP codes only**. This writes no audit row, the
    same as the dashboard.
- Blocked:
  - `enroll_staff_totp`, whose confirmation must be audited;
  - recovery codes at admin login, which must be audited. These are
    rejected instead.
- Consequence: only staff who already enrolled 2FA through the
  dashboard can reach admin, and that only works for staff who are
  merchant members.
- Any other route would need an explicit, documented exception to
  `Audit-Logging.md`. This spec does not invent one.

## Django apps
- **Created:** none.
- **Touched:**
  - `config`: the admin site, its `AdminConfig` and the settings;
  - `core`: the IP middleware; `tenant_atomic(durable=)`; the
    `staff_auth` scope (LC-1);
  - `accounts`: the login form, staff TOTP services, the command, the
    middleware exemption and `MerchantAdmin`;
  - `auditlog`: `staff_merchant_read`;
  - `events`: the dead-letter service, view and template.

## Models & database changes
No database changes: no model, field, constraint, index, RLS policy or
migration.

- Staff-read audit rows use the existing `AuditLog` columns inside the
  selected merchant's tenant transaction (the `tenant_isolation`
  policy):
  - `merchant_id`: from the tenant context;
  - `actor_user`: the staff user;
  - target: the `Merchant`;
  - `metadata_json`: `{"resource": ...}`.
- Staff 2FA rows (LC-1) are NULL-merchant rows under the existing
  `platform_insert` policy.

## API endpoints
No new endpoints under `/api/v1/`. The new admin URLs below use admin
session auth plus a verified second factor, and are IP-allowlisted.

- `GET /admin/events/dead-letter/<uuid:merchant_id>/`:
  - Dead-letter events for one merchant.
  - Allowed: an active staff user who passes `has_permission`.
  - Any other method answers `405`.
  - An unknown merchant answers `404`.
- `/admin/login/` gains a required `otp_code` field and a per-IP
  throttle.

## Services & background tasks
- **`core.tenancy.tenant_atomic(*, durable: bool = False)`.**
  - It passes `durable` to `transaction.atomic`.
  - Existing callers are unchanged (the default is `False`).
- **`auditlog.services.staff_merchant_read(*, staff_user, merchant_id, resource: str)`**
  is a context manager and the only privileged tenant-read path. In
  order, it:
  1. raises `StaffAccessDenied` (a new `ReviewFlowError`, 403) unless
     `staff_user.is_active and staff_user.is_staff`;
  2. raises `TenantContextError` if a tenant context is active;
  3. raises `Merchant.DoesNotExist` for an unknown merchant (`Merchant`
     is a global table);
  4. inside `tenant_context(merchant_id)`, creates the audit row with
     `AuditLog.objects.create(...)` (not `record()`) in its own
     `tenant_atomic(durable=True)`, using action
     `staff.merchant_data_viewed`. **That transaction commits before
     the read transaction opens.** If the insert raises, the exception
     propagates and no read happens;
  5. opens a second `tenant_atomic(durable=True)` and yields. The
     caller reads and **materializes** its data inside this block;
  6. on exit or exception: the read transaction ends, which clears its
     `SET LOCAL`, and `tenant_context`'s `finally` clears the
     contextvar.

  Both blocks are durable. Calling the function inside any other atomic
  block raises `RuntimeError` before the audit or the read, so neither
  can become a savepoint whose `SET LOCAL` outlives it. A read that
  raises leaves the committed audit row in place, which is intended:
  the access was attempted.
- **`events.services.list_dead_letter_events(*, limit: int = 200) -> tuple[int, list[dict]]`**
  - It requires a tenant context.
  - It returns `(total_count, rows)` for `status=DEAD_LETTER`, ordered
    by `-updated_at`.
  - It uses `.values()` with exactly these fields: `id`,
    `integration_id`, `source`, `external_event_id`, `attempt_count`,
    `error_code`, `error_message`, `received_at`, `updated_at`. **The
    payload is never selected.**
  - `# ponytail: fixed 200-row cap with the total shown; paginate when
    a merchant exceeds it.`
- **`accounts.services.begin_staff_totp_setup(*, user) -> tuple[str, str]`**
  - It requires staff and active, and the user must not be enrolled.
  - It locks the user in `transaction.atomic()` and stores the
    encrypted secret.
  - It writes no audit row.
- **`accounts.services.confirm_staff_totp_setup(*, user, code) -> list[str]`**
  (needs LC-1) runs inside one `platform_write_atomic("staff_auth")`
  block:
  - it locks the user;
  - `_verify_totp` (with replay protection);
  - it generates the recovery codes and saves;
  - it calls `record_platform("user.totp_enabled", actor=user, target=user)`.

  It raises `TotpAlreadyEnabled`, `TotpSetupRequired` or
  `InvalidTotpCode`, as the dashboard does.
- **`accounts.services.verify_staff_admin_login(*, user, code) -> bool`**
  returns False unless the user is active, staff and enrolled.
  - **A `^[0-9]{6}$` code:** `transaction.atomic()`, lock the user,
    `_verify_totp`. No audit row.
  - **Any other code** (needs LC-1; rejected without it): one
    `platform_write_atomic("staff_auth")` block that locks the user and
    calls `_consume_recovery_code`. On a match it calls
    `record_platform("user.totp_recovery_code_used", actor=user, target=user, metadata={"recovery_codes_remaining": n})`.
  - Every failure returns False and writes nothing.
  - The existing dashboard functions are unchanged.
- **Command `enroll_staff_totp <email>`:**
  - It prints the secret and the `otpauth` URI once and reads the
    confirming code from stdin.
  - It prints the 10 recovery codes once.
  - It never logs them. Any failure exits non-zero with a fixed
    message.
- No Celery tasks, no Beat schedules and no adapters.

## Admin
- **`config/admin.py`, `ReviewFlowAdminSite(AdminSite)`**, installed
  through `config/apps.py` `ReviewFlowAdminConfig(AdminConfig)` with
  `default_site`. It replaces `django.contrib.admin` in
  `INSTALLED_APPS`.
  - `login_form = StaffAdminLoginForm` (`accounts/admin.py`):
    - It extends `AdminAuthenticationForm` with `otp_code`.
    - After the password check it calls `verify_staff_admin_login`.
    - Every failure raises the form's single generic error.
    - On success it marks the request
      (`request._admin_2fa_user_id = user.pk`). The form never writes
      the session.
  - The `login()` override calls `super().login(request)`. Then, if the
    request was a POST and `request.user.pk` equals the marked id, it
    sets `session["admin_2fa_user_id"] = str(user.pk)`. This runs
    after `auth_login()`, so a session flush caused by switching users
    cannot drop the flag.
  - The `login()` override first applies `AdminLoginThrottle` on POST
    and answers `429` when the limit is exceeded. That throttle is a
    DRF `SimpleRateThrottle` with scope `admin_login`, keyed on
    `get_ident`.
  - `has_permission(request)` requires `is_active and is_staff` and
    `session.get("admin_2fa_user_id") == str(request.user.pk)`. A
    dashboard-only staff session does not satisfy it.
  - `get_urls()` adds the dead-letter route, wrapped in
    `self.admin_view()`.
- **`events/admin.py`, `dead_letter_events_view`:**
  - `require_GET`.
  - It runs `with staff_merchant_read(..., resource="events.integrationevent.dead_letter")`,
    calls `list_dead_letter_events()` inside the block, and renders
    `admin/events/dead_letter_events.html` afterwards with plain dicts
    only.
  - It converts `Merchant.DoesNotExist` to `Http404`, because admin
    bypasses the DRF exception handler.
  - It has no forms and no actions.
  - `IntegrationEvent` is not registered as a `ModelAdmin`.
- **`MerchantAdmin`:** adds a `list_display` link column to each
  merchant's dead-letter page. This is the explicit merchant selection.
  The page stays read-only.
- **Unchanged:** `UserAdmin` and `PlanAdmin`. The tenant models stay
  unregistered (D4).

## Files to change
- `config/settings.py`:
  - in `INSTALLED_APPS`, the admin config swap;
  - `core.middleware.AdminIPAllowlistMiddleware`, right after
    `SecurityMiddleware`;
  - `ADMIN_ALLOWED_IPS = env.list(..., default=[])`;
  - `"admin_login": env("ADMIN_LOGIN_RATE", default="5/min")` in
    `DEFAULT_THROTTLE_RATES`.
- `.env.example` (tracked): `ADMIN_ALLOWED_IPS=127.0.0.1/32`.
- `core/middleware.py`: `AdminIPAllowlistMiddleware`.
  - Admin prefix only (`reverse("admin:index")`).
  - The ident comes from `BaseThrottle().get_ident(request)`.
  - Parse every `ADMIN_ALLOWED_IPS` entry with
    `ipaddress.ip_network(entry, strict=False)`. **Any unparsable entry,
    an empty list, or an unparsable ident gives `404` for everyone**
    (D5). Otherwise allow only if the ident is in one of the networks.
  - Settings are read per request.
- `core/tenancy.py`:
  - `tenant_atomic(*, durable=False)`;
  - `"staff_auth"` in `_PLATFORM_WRITE_SCOPES`, plus the docstring
    (**LC-1 only**).
- `accounts/middleware.py`: `SessionMerchantMiddleware` skips the admin
  prefix.
- `accounts/services.py`: the three staff TOTP functions.
- `accounts/admin.py`: `StaffAdminLoginForm` and the `MerchantAdmin`
  link.
- `auditlog/services.py`: `StaffAccessDenied`, `staff_merchant_read`
  and `AUDIT_STAFF_DATA_VIEWED`.
- `events/services.py`: `list_dead_letter_events`.
- Existing admin tests, fixture changes only, with every assertion kept
  (see the Rules):
  - `billing/tests/test_admin.py` (`staff_client`);
  - `accounts/tests/test_permissions.py:79` and `:87`.
- Documentation, done during implementation and not in this
  spec-and-plan task:
  - `docs/10-development/Development-Setup.md`;
  - `docs/09-security/Audit-Logging.md`;
  - `docs/02-architecture/Multi-Tenancy.md` (LC-1 only, with its
    sign-off date).

## Files to create
- `config/admin.py` and `config/apps.py`.
- `accounts/management/__init__.py`,
  `accounts/management/commands/__init__.py` and
  `accounts/management/commands/enroll_staff_totp.py` (LC-1).
- `events/admin.py`, and
  `events/templates/admin/events/dead_letter_events.html`.
- Tests:
  - `core/tests/test_admin_ip_allowlist.py`;
  - `core/tests/test_tenant_atomic_durable.py`;
  - `accounts/tests/test_staff_admin_login.py`;
  - `accounts/tests/test_staff_totp_services.py`;
  - `accounts/tests/test_enroll_staff_totp.py` (LC-1);
  - `core/tests/test_staff_auth_scope.py` (LC-1);
  - `auditlog/tests/test_staff_merchant_read.py`;
  - `events/tests/test_dead_letter_admin.py`;
  - `conftest.py` or an app-level `conftest.py`: the
    `admin_session_client` fixture.

## New dependencies
No new dependencies. Everything uses `ipaddress` (stdlib), `pyotp`, DRF
and Django admin.

## Rules for implementation
- Django + DRF monolith. Business logic lives only in `services.py`.
  Admin views and forms call services and nothing else.
- Every tenant-owned model uses `core.TenantScopedManager`, with RLS
  enabled. In admin, tenant data is read only inside
  `staff_merchant_read`.
- Never trust a client-supplied `merchant_id`/`location_id`. The staff
  selection is validated against the `Merchant` table, audited and
  enforced by RLS. It is never written to `session["merchant_id"]`.
- Celery tasks take `merchant_id` explicitly. None are added.
- Idempotency via database unique constraints, not check-then-insert.
  There are no inbound events here, and every read writes exactly one
  audit row by design.
- Role checks via DRF permission classes (OWNER/ADMIN/MANAGER/VIEWER).
  Admin uses `is_staff` plus a verified second factor, never a
  merchant role, and creates no `TeamMember`.
- External IDs are UUIDs.
- Secrets and OAuth tokens are Fernet-encrypted, never hardcoded or
  committed. The TOTP secret, `otpauth` URI, codes and recovery codes
  are never logged, audited or rendered by admin.
- Status enums in `UPPER_SNAKE_CASE`; timestamps suffixed `_at`.
- No V2 features, no bespoke admin app and no `eta` scheduling. Nothing
  from D4's deferred list.
- Every new service function has a unit test.
- **Render safety.** Templates receive plain dicts, never a queryset. A
  test asserts that rows appear in the response, not only status `200`.
- **No nesting workarounds.**
  - Never wrap `staff_merchant_read` or `staff_auth` code in an outer
    atomic block.
  - Never catch the durable `RuntimeError` to fall back to a savepoint.
- **Fail closed.**
  - A bad IP or allowlist: `404`.
  - No verified second factor: redirect to the admin login.
  - Non-staff: no access.
  - Unknown merchant: `404`.
  - Audit insert fails: no read, no consumption, no enrollment.
- **Test fixtures.**
  - **Login-flow tests** post the real `/admin/login/` with a
    pyotp-generated code or a real recovery code.
  - **Authenticated-session tests** use `admin_session_client`. It sets
    `ADMIN_ALLOWED_IPS=["127.0.0.1/32"]`, calls `force_login` for a
    staff user, and sets `session["admin_2fa_user_id"]`, the flag the
    real login sets. `has_permission` is unchanged and is exercised by
    both kinds of test. A dedicated test proves that `force_login`
    **without** the flag is redirected.
  - Existing admin tests switch to the helper. Their assertions are
    unchanged.
  - No skip or xfail is added.
- **Transactions in tests.** Commit-before-read, audit-survives-a-failed-read
  and the `SET LOCAL` no-leak tests use
  `django_db(transaction=True)`, following the `test_platform_write.py`
  convention.
- **Throttle tests.** They use a local-memory cache override, as the
  existing throttle tests do (`events/tests/conftest.py`
  `local_cache`).

### Open questions (do not block steps 1–3 and 5–7 of the plan)
- **Q1:** Should a successful TOTP-code admin login be audited? The
  proposal is no, for parity with the docs and the dashboard. Yes would
  widen LC-1 to every login.
- **Q2:** Should failed admin logins be audited? The proposal is no
  AuditLog row. If yes, use the separate-transaction design in LC-1
  item 7. A structured log line holding only the user id would need
  its own decision, because no failed-login logging exists today.
- **Q3:** The truncated brief: were there locked decisions after D1?

## Definition of done
- [ ] `pytest` passes in full, including the existing admin tests with
      unchanged assertions. No skip or xfail.
- [ ] `python manage.py makemigrations --check` is clean. No RLS policy
      is added, and the existing `auditlog`, `events`, `whatsapp` and
      `core` RLS tests pass unchanged.
- [ ] IP allowlist:
  - [ ] An allowlisted IP reaches `/admin/login/`.
  - [ ] Otherwise, `/admin/`, `/admin/login/` and the dead-letter URL
        all answer `404`.
  - [ ] An empty list answers `404`, and so does a list with one
        invalid entry.
  - [ ] With `NUM_PROXIES=0`, a spoofed `X-Forwarded-For` is ignored.
  - [ ] With `NUM_PROXIES=1`, the right-most entry is the one checked.
  - [ ] Non-admin paths are unaffected.
- [ ] Admin login:
  - [ ] Password plus a valid TOTP reaches the admin index.
  - [ ] A wrong code fails, and so does a **replayed** code (the same
        code twice in one time step).
  - [ ] A missing code fails.
  - [ ] A staff user with no 2FA fails, and so does a non-staff user.
  - [ ] All failures show the same error.
  - [ ] A dashboard-only staff session, and `force_login` without the
        flag, are both redirected to the admin login.
  - [ ] A correct login from a browser session that belonged to another
        user reaches the index.
  - [ ] The sixth POST from one IP within a minute answers `429`.
- [ ] Recovery codes (LC-1):
  - [ ] A code works once. A second use fails.
  - [ ] Exactly one `user.totp_recovery_code_used` platform row is
        written, with `recovery_codes_remaining` only.
  - [ ] When the audit insert is forced to fail, the code remains
        valid and the login fails.
  - [ ] A six-digit input never opens a `staff_auth` block.
  - [ ] Without LC-1, recovery codes are rejected and nothing is
        written.
- [ ] Concurrency: two simultaneous verifications of the same TOTP code
      or recovery code (`transaction=True`, two threads) give exactly
      one success.
- [ ] `enroll_staff_totp` (LC-1):
  - [ ] It enrolls a staff user and prints 10 recovery codes.
  - [ ] It refuses a non-staff, inactive or enrolled user.
  - [ ] It writes one `user.totp_enabled` platform row.
  - [ ] When the audit insert fails, 2FA stays disabled.
  - [ ] No secret appears in logs or audit metadata.
- [ ] `staff_auth` scope (LC-1):
  - [ ] Inserting the shared WhatsApp row inside it fails, and so does
        updating it.
  - [ ] The only call sites are the two named functions.
- [ ] `staff_merchant_read`:
  - [ ] It commits exactly one `staff.merchant_data_viewed` row (the
        selected merchant, the staff actor) before the read.
  - [ ] The row persists when the read raises (`transaction=True`).
  - [ ] A forced audit failure means the read body never runs.
  - [ ] It refuses non-staff users, an active tenant context and an
        unknown merchant.
  - [ ] Inside `transaction.atomic()` it raises `RuntimeError`.
  - [ ] The context is `None` after success and after an exception.
- [ ] RLS: after the block, a raw query on the same connection returns
      no `IntegrationEvent` rows and the setting is empty
      (`transaction=True`).
- [ ] Isolation (Testing-Strategy, Tenant Isolation, both layers):
  - [ ] Inside merchant A's block, the ORM and raw SQL see only A's
        rows.
  - [ ] B's dead-letter events never render on A's page.
- [ ] Dead-letter page:
  - [ ] It shows only `DEAD_LETTER` events, with the safe columns and
        the total.
  - [ ] A phone number placed in a payload never appears in the
        response.
  - [ ] POST answers `405`, and so does PUT.
  - [ ] Every GET adds one audit row.
  - [ ] An unknown merchant answers `404`.
  - [ ] A staff user whose session also holds `merchant_id` for B
        can view A's page.
- [ ] The tenant models stay unregistered in admin.
