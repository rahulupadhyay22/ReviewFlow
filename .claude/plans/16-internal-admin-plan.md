# Plan: 16-internal-admin

**Spec:** `.claude/specs/16-internal-admin.md`. The spec wins on any
conflict.

**Status:** steps 1–9 implemented on `feature/internal-admin` and awaiting
the user's review. Nothing is committed.

**Gate:** LC-1 was **APPROVED 2026-10-13**, so the steps marked
**[LC-1]** are unblocked and implemented.

**Convention note:** no `.claude/plans/` file existed before this one,
so this plan follows the spec's own structure and checklist style.

Each step is one reviewable unit with its own tests. The steps run in
dependency order. Run focused tests after each step; the full suite
runs only in step 9.

---

## Step 1: Admin IP allowlist (fail closed)
**Files:**
- `core/middleware.py`: `AdminIPAllowlistMiddleware`;
- `config/settings.py`: register the middleware right after
  `SecurityMiddleware`, and add `ADMIN_ALLOWED_IPS`;
- `.env.example`.

**Tests:** `core/tests/test_admin_ip_allowlist.py`.

**Accept when:**
- An allowlisted `REMOTE_ADDR` reaches `/admin/login/` (200).
- A non-allowlisted IP gets `404` on `/admin/`, `/admin/login/` and an
  admin sub-URL.
- `ADMIN_ALLOWED_IPS=[]` answers `404`, and so does `["10.0.0.0/8", "nonsense"]`.
- `NUM_PROXIES=0` with a spoofed `X-Forwarded-For` uses `REMOTE_ADDR`.
- `NUM_PROXIES=1` uses the right-most entry.
- `/api/v1/...` is unaffected by the setting.

**Note:** right after this step, the existing admin tests fail because
the default allowlist is empty. Step 7 fixes their fixtures. Until
then, run only the step's own tests.

## Step 2: Keep admin requests out of the dashboard tenant wrapper
**Files:** `accounts/middleware.py` (`SessionMerchantMiddleware` skips
the admin prefix, with the reason in its docstring).

**Tests:** extend `accounts/tests/test_middleware*` or the nearest
existing file. A staff session holding `session["merchant_id"]` must
reach an admin URL with `get_current_merchant_id() is None` inside the
view, checked with a spy.

**Accept when:**
- Dashboard API requests are still tenant-wrapped. The existing
  `test_authenticated_request_runs_with_session_merchant_tenant_context`
  passes unchanged.

## Step 3: `tenant_atomic(durable=)`, then the staff TOTP services
**3a (unblocked):**
- `core/tenancy.py`: `tenant_atomic(*, durable=False)`.
- `core/tests/test_tenant_atomic_durable.py`:
  - nested `durable=True` inside `transaction.atomic()` raises
    `RuntimeError`;
  - the default behavior is unchanged.

**3b:** `accounts/services.py`:
- `begin_staff_totp_setup` (unblocked);
- `verify_staff_admin_login`, TOTP branch (unblocked);
- **[LC-1]** the recovery branch of `verify_staff_admin_login`. Without
  sign-off it returns False and writes nothing;
- **[LC-1]** `confirm_staff_totp_setup`;
- **[LC-1]** `core/tenancy.py`: add `"staff_auth"` to
  `_PLATFORM_WRITE_SCOPES` and update the docstring.

**3c [LC-1]:** `accounts/management/__init__.py`,
`commands/__init__.py` and `commands/enroll_staff_totp.py`.

**Tests:**
- `accounts/tests/test_staff_totp_services.py`:
  - a valid code passes;
  - a wrong code fails;
  - a replayed code fails (same step);
  - non-staff, inactive or unenrolled users get False;
  - a six-digit input never opens `platform_write_atomic` (spy);
  - concurrency (`transaction=True`, two threads with the same TOTP
    code): exactly one True.
- **[LC-1]** tests:
  - a recovery code works once;
  - exactly one platform audit row, metadata
    `{recovery_codes_remaining}` only;
  - a forced `record_platform` failure leaves the code valid;
  - concurrent recovery use: exactly one True.
- **[LC-1]** `accounts/tests/test_enroll_staff_totp.py`:
  - the happy path prints 10 codes and writes one `user.totp_enabled`
    platform row;
  - it refuses non-staff, inactive or enrolled users;
  - a forced audit failure leaves 2FA disabled;
  - no secret appears in the captured logs or the audit metadata.
- **[LC-1]** `core/tests/test_staff_auth_scope.py`:
  - inserting the shared WhatsApp row inside the scope fails, and so
    does updating it;
  - grepping the codebase finds `platform_write_atomic("staff_auth"`
    only in the two named functions.

## Step 4: Admin site, login form, throttle, session flag
**Before coding:** read Django 5.2's `AdminSite.login` and `LoginView`
source to confirm the override point. This is the spec's unverified
assumption. If the post-`super()` flag-setting approach does not hold,
stop and report it. Do not work around it.

**Files:**
- `config/admin.py`: `ReviewFlowAdminSite` with `login()`,
  `has_permission()`, `get_urls()` and `AdminLoginThrottle`;
- `config/apps.py`: `ReviewFlowAdminConfig`;
- `config/settings.py`: the `INSTALLED_APPS` swap and the
  `admin_login` throttle rate;
- `accounts/admin.py`: `StaffAdminLoginForm`.

**Tests:** `accounts/tests/test_staff_admin_login.py`. These go through
the real login flow, with no helper:
- password plus a valid TOTP reaches the index;
- a wrong, replayed or missing code fails;
- a non-staff or unenrolled user fails;
- every failure shows the identical error text;
- `force_login` without the flag is redirected to the login page;
- a dashboard `/auth/login` staff session is redirected;
- a correct login on a session that belonged to another user reaches
  the index;
- the sixth POST from one IP within a minute answers `429` (with a
  local-memory cache override);
- **[LC-1]** a recovery-code login works; without LC-1 it is rejected.

## Step 5: Privileged per-merchant read path
**Files:** `auditlog/services.py` (`StaffAccessDenied`,
`AUDIT_STAFF_DATA_VIEWED`, `staff_merchant_read`).

**Tests:** `auditlog/tests/test_staff_merchant_read.py`, with
`transaction=True` where noted:
- one `staff.merchant_data_viewed` row, with merchant A, the staff
  actor and `{"resource": ...}`, committed before the body runs. Check
  it from the body through a second connection, or by checking that the
  row exists after the body raises (`transaction=True`);
- the read body raises → the row persists (`transaction=True`);
- the audit insert is forced to fail (monkeypatch) → the body never
  runs;
- it refuses non-staff and inactive users, an active tenant context,
  and an unknown merchant (`Merchant.DoesNotExist`);
- inside `transaction.atomic()` → `RuntimeError`, with no audit row and
  no read;
- `get_current_merchant_id() is None` after success and after an
  exception;
- after the block, `current_setting('app.current_merchant_id', true)`
  is empty and raw SQL on `events_integrationevent` returns no rows
  (`transaction=True`, pooled-connection scenario);
- inside A's block, the ORM and raw SQL return only A's rows, never
  B's (tenant isolation, both layers).

## Step 6: Dead-letter service, view, template, Merchant link
**Files:**
- `events/services.py`: `list_dead_letter_events`;
- `events/admin.py`: the view;
- `events/templates/admin/events/dead_letter_events.html`;
- `config/admin.py`: the route in `get_urls()`;
- `accounts/admin.py`: the `MerchantAdmin` link column.

**Tests:** `events/tests/test_dead_letter_admin.py`.
- **Service:**
  - only `DEAD_LETTER` rows are returned (FAILED, PROCESSED, CANCELLED
    and RECEIVED are excluded);
  - each row's keys are exactly the nine safe fields;
  - the total count is correct;
  - the 200-row cap holds;
  - it needs a tenant context.
- **View** (`admin_session_client`):
  - A's rows render, checked on the rendered content and not only the
    status code;
  - B's rows never appear;
  - a phone number stored in the payload is absent from the response;
  - POST and PUT answer `405`;
  - every GET adds one audit row;
  - an unknown merchant answers `404`, not `500`;
  - a session that also holds `merchant_id` for B works for A;
  - a non-allowlisted IP answers `404`.
- **MerchantAdmin:** the changelist links to the dead-letter URL.

## Step 7: Existing admin test fixtures (no assertion changes)
**Files:**
- `conftest.py` (or an app-level conftest): the `admin_session_client`
  fixture;
- `billing/tests/test_admin.py` (`staff_client`);
- `accounts/tests/test_permissions.py` (the tests at `:79` and `:87`).

**Accept when:**
- the diff touches only the login and setup lines;
- every assertion line is byte-identical;
- there are no skips or xfails;
- `test_teammember_and_auditlog_not_registered_in_admin` and
  `test_apikey_is_not_registered_in_django_admin` pass unchanged.

## Step 8: Docs (same branch, during implementation)
- `docs/10-development/Development-Setup.md`: set `ADMIN_ALLOWED_IPS`,
  and run `enroll_staff_totp` after `createsuperuser` (**[LC-1]**; the
  fallback text applies otherwise).
- `docs/09-security/Audit-Logging.md`: `staff.merchant_data_viewed`,
  plus **[LC-1]** the staff `user.totp_enabled` and
  `user.totp_recovery_code_used` platform rows.
- `docs/02-architecture/Multi-Tenancy.md`:
  - name `staff_merchant_read` under "No Standing Privileged Role";
  - **[LC-1]** the `staff_auth` scope, its two callers and the sign-off
    date.

## Step 9: Final verification (once)
- `pytest`, the full suite against PostgreSQL. It must be all green
  with no new skips.
- `python manage.py makemigrations --check`: no changes.
- `python manage.py check`.
- `git diff --stat`: no file outside the spec's "Files to change" and
  "Files to create", and no migration.
- RLS sanity:
  - `pg_policies` for `auditlog_auditlog`, `events_integrationevent`
    and `whatsapp_whatsappaccount` match `main`;
  - the app role is still `NOSUPERUSER NOBYPASSRLS`.
- Then `/test-feature 16-internal-admin` and
  `/code-review-feature 16-internal-admin`, per the workflow. Fix only
  the confirmed findings, then re-run once. There is no open-ended
  hardening loop.

---

## Blocked by LC-1 (summary)
- Step 3b: the recovery branch and `confirm_staff_totp_setup`.
- Step 3b: the `staff_auth` scope.
- Step 3c: the `enroll_staff_totp` command.
- Step 4: the recovery-code login test.
- Step 8: the LC-1 doc lines.

Everything else is unblocked:
- steps 1, 2, 3a, 5, 6 and 7;
- the TOTP branch of step 3b;
- step 4 except recovery.

## Open questions (from the spec)
- Q1: Should a successful TOTP admin login be audited?
- Q2: Should failed admin logins be audited?
- Q3: Did the truncated brief contain locked decisions after D1?

None of them blocks the unblocked steps.
