# Spec: TOTP Two-Factor Authentication

## Overview
Adds optional, per-user TOTP two-factor authentication to dashboard
login. A logged-in user enrolls by re-entering their password, scanning
a provisioning URI into an authenticator app, and confirming one code.
They then receive 10 one-time recovery codes, shown once. From then on
`POST /auth/login` checks the password but does **not** log the user in.
It stores a short-lived, unauthenticated pending marker in the session,
and `POST /auth/login/totp` completes the login with a TOTP code or a
recovery code. The TOTP secret is Fernet-encrypted at rest. This spec
wires up `FERNET_KEY` and adds the first shared encryption helper
(`core/crypto.py`), which Phase 09 reuses for Google OAuth tokens. This
is the last remaining Phase 02 work. It is shared foundation for all
three planes (ingestion, automation, intelligence), because every
dashboard endpoint sits behind this login.

`docs/` specifies only "Optional TOTP 2FA per user"
(`Authentication.md` §1, `Security-Architecture.md` §Authentication).
The user settled these decisions on 2026-09-22, when this spec was
scoped:
- **Recovery:** 10 one-time recovery codes, generated at enrollment,
  shown once, stored hashed. Each one works once in place of a TOTP
  code, and each use writes an `AuditLog` row. There is no OWNER/ADMIN
  reset of another member's 2FA. Staff recovery waits for Phase 16.
- **Login flow:** two-step. `POST /auth/login` returns
  `{ totp_required: true }` for an enrolled user, and
  `POST /auth/login/totp` completes the login.
- **Enforcement:** per-user opt-in only. A merchant cannot require 2FA.
- **Dependencies:** `pyotp` + `cryptography`.

## Source docs
- `docs/ROADMAP.md`: §"02 — Accounts, roles & audit log" ("optional
  TOTP 2FA")
- `docs/04-api/Authentication.md`: §1 (Dashboard Users: "Optional TOTP
  2FA per user"), §"Session vs. API Key"
- `docs/04-api/API-Specification.md`: §Auth (`GET`/`POST /auth/login`,
  `/auth/logout`, `/auth/refresh`, `/auth/accept-invite`), §"General
  Conventions" (error shape, rate limits)
- `docs/02-architecture/Security-Architecture.md`: §Authentication
  (Dashboard Users), §"Encryption & Secrets" (Fernet, `FERNET_KEY`)
- `docs/09-security/Security-Controls.md`: secrets checklist
  (`FERNET_KEY` from env, read once at settings load), "never log
  tokens", pre-launch auth-bypass pass
- `docs/09-security/Audit-Logging.md`: §"What Gets Logged"
  (merchant-side), §"Access to Audit Logs"
- `docs/03-database/Data-Dictionary.md`: §User
- `docs/03-database/Database-Design.md`: Legend (GLOBAL), §User
- `docs/02-architecture/Multi-Tenancy.md`: §"Isolation Layers",
  §"No Standing Privileged Role"
- `docs/10-development/Development-Setup.md`: environment variables
  (`FERNET_KEY`)
- `docs/10-development/Coding-Standards.md`: §5–§6 (naming, migrations)
- `docs/10-development/Testing-Strategy.md`: API tests, Security tests,
  Database constraint tests (concurrency)
- `.claude/specs/02-merchant-account-foundation.md` and
  `.claude/specs/02-team-member-management.md`: the "Roadmap Phase"
  remaining work (this spec), the GLOBAL `User` carve-out,
  `SessionMerchantMiddleware` pre-tenant paths, and throttle
  conventions

## Depends on
- Phase 00, Project scaffold (Done)
- Phase 01, Core tenancy & RLS (Done): `tenant_context`,
  `tenant_atomic`, `user_lookup_atomic`
- Spec `02-merchant-account-foundation` (merged): `User`, `Merchant`,
  `TeamMember`, `authenticate_login`, `resolve_login_membership`,
  `session_body`, `LoginView`, `SessionMerchantMiddleware`,
  `IsMerchantMember`, `auditlog.services.record`, and the `login`
  throttle scope
- Spec `02-team-member-management` (merged): `AcceptInviteView` and the
  `_pre_tenant_paths` set that this spec extends

## Roadmap Phase
- Phase: 02 — Accounts, roles & audit log
- Completes entire phase: Yes
- Reason: both earlier Phase 02 specs name optional TOTP 2FA as the only
  remaining Phase 02 work. The team-member spec also records the
  "AuditLog" bullet as complete through explicit `record()` calls.

## Locked decisions touched
- Shared schema, `merchant_id` scoping (`Multi-Tenancy.md` §Model) —
  NO CHANGE. The new fields are on the GLOBAL `User`.
- `TenantScopedManager` + PostgreSQL RLS as the final boundary
  (`FINAL-ARCHITECTURE-REVIEW.md` §8, `Multi-Tenancy.md`) — DEPENDS ON.
  `AuditLog` writes stay merchant-scoped under RLS. `User` stays GLOBAL
  with no RLS.
- Transaction-local `SET LOCAL app.current_merchant_id`
  (`FINAL-ARCHITECTURE-REVIEW.md` §8) — DEPENDS ON. Audit writes happen
  inside `tenant_atomic()`.
- No standing privileged role / no `BYPASSRLS`
  (`Multi-Tenancy.md` §"No Standing Privileged Role") — NO CHANGE. There
  is no staff 2FA reset path.
- Three auth mechanisms, never mixed (`Authentication.md`) — DEPENDS ON.
  TOTP is a second factor of the dashboard session mechanism only.
- Session auth + CSRF for the dashboard (`Authentication.md` §1,
  `Security-Architecture.md`) — DEPENDS ON.
- Secrets encrypted with Fernet, key from env only
  (`Security-Architecture.md` §"Encryption & Secrets") — DEPENDS ON.
  This spec is the first implementation of it.

None of these is a LOCKED DECISION CHANGE. The `POST /auth/login`
response gains a second shape (`{ totp_required: true }`), and the
session body gains `user.totp_enabled`. Both are documented extensions
of `API-Specification.md` §Auth, not changes to a locked decision.

## Django apps
- `accounts`: touched (User fields, services, views, serializers, urls,
  middleware, exceptions, admin, migration, tests)
- `core`: touched (new `core/crypto.py`)
- `config`: touched (`FERNET_KEY` setting, throttle scopes)
- `auditlog`: no code change (existing `record()` is called)

## Models & database changes

### `accounts.User` (GLOBAL, with no `merchant_id`, no RLS and a plain manager)
`User` is GLOBAL (`Database-Design.md` Legend). The new columns add no
`merchant_id` and no RLS policy. This is intentional, not a missed RLS
table, and mirrors `02-merchant-account-foundation.md`. The fields go
on `User`, not on a new model: they are 1:1 with the user, and a single
row lock on `User` then serializes every 2FA state change.

| Field | Type | Nullable | Notes |
|---|---|---|---|
| `totp_secret_encrypted` | text | Yes | Fernet token of the base32 TOTP secret (`core.crypto.encrypt`). Set by setup, while pending or enabled. `NULL` when 2FA is off and no setup is pending. Never logged, serialized, or shown in admin. |
| `totp_confirmed_at` | datetime | Yes | 2FA is enabled iff this is non-null. Set by confirm, cleared by disable. |
| `totp_last_used_step` | bigint | Yes | The last accepted TOTP time step (`unix_time // 30`). A code whose step is ≤ this value is rejected (replay protection). |
| `totp_recovery_code_hashes` | jsonb | No, default `[]` | A list of `sha256` hex digests of the unused recovery codes. A code is removed from the list when it is used. |

- A model property `is_totp_enabled` returns `totp_confirmed_at is not None`.
- No new unique constraints or indexes. There is no lookup by these
  columns.
- Migration `accounts/0003_user_totp.py` adds only these four columns.
  It is additive and non-destructive: nullable columns, plus a jsonb
  column with a default.
- Update `docs/03-database/Data-Dictionary.md` §User with these four
  fields. `password_hash` / `is_active` are unchanged.

### Recovery codes
- 10 codes per enrollment, each `secrets.token_hex(10)` (80 bits). They
  are displayed as `xxxxx-xxxxx-xxxxx-xxxxx`.
- Normalized before hashing and checking: lowercase, then strip `-` and
  whitespace. Stored as `hashlib.sha256(normalized).hexdigest()`. The
  high entropy makes unsalted sha256 appropriate (same reasoning as
  API keys, `Authentication.md` §2), so a slow KDF is not needed.
- Compare with `hmac.compare_digest` against each stored hash.
- There is no regenerate endpoint. A user who runs low disables 2FA and
  enrolls again, which issues a fresh set.

### Pending-login marker (session, not the database)
Step 1 of an enrolled user's login stores only this in the session:
`request.session["pending_totp"] = {"u": "<user uuid>", "iat": <unix
seconds>, "n": 0}`. It holds no merchant id and no `_auth_user_id`.
- Lifetime: 5 minutes (`TOTP_PENDING_MAX_AGE`).
- `n` counts failed step-2 attempts. At 5 failures the marker is
  deleted, and the user must re-enter the password.

## API endpoints
All paths are under `/api/v1/`. Error bodies use the standard error
shape (`core.api.exception_handler`).

- `POST /auth/login` (**changed**): public, CSRF required, `login`
  throttle (unchanged)
  - Not enrolled: unchanged. `200` session body, and the user is logged
    in.
  - Enrolled: password and membership are checked exactly as today
    (`authenticate_login`, with the same generic `401
    invalid_credentials` for every failure). Then the session is
    **flushed**, `pending_totp` is stored, and the response is
    `200 { "totp_required": true }`. **Django `login()` is not called**,
    and no `merchant_id` is put in the session.
- `POST /auth/login/totp` (**new**): completes an enrolled user's login.
  Auth: the pending marker only (no authentication classes), CSRF
  required, new `login_totp` throttle (per-IP, 5/min). Roles: n/a
  (pre-tenant).
  - Request: `{ "code": "<6-digit TOTP or recovery code>" }`
  - `200` session body (same shape as login). Django `login()` cycles
    the session key, the marker is removed, and `merchant_id` is set.
  - `401 invalid_credentials`: one generic response for every failure
    reason (no marker, expired marker, wrong code, replayed code,
    attempt cap reached, user inactive, membership gone). `403` missing
    CSRF. `429` throttled.
- `POST /auth/2fa/setup` (**new**): start or restart enrollment. Session
  required, with the `IsMerchantMember` default. Roles: all
  (OWNER/ADMIN/MANAGER/VIEWER), because 2FA is per user. Uses the
  `totp_manage` throttle (per user, 5/min).
  - Request: `{ "password": "<current password>" }`
  - `200 { "secret": "<base32>", "otpauth_uri": "otpauth://totp/ReviewFlow:<email>?secret=…&issuer=ReviewFlow" }`.
    It replaces any earlier unconfirmed secret. The frontend renders the
    QR code from `otpauth_uri`.
  - `400 reauthentication_failed` for a wrong password.
    `409 totp_already_enabled` if already enrolled.
- `POST /auth/2fa/confirm` (**new**): finish enrollment. Session
  required, all roles, `totp_manage` throttle.
  - Request: `{ "code": "<6-digit TOTP>" }`
  - `200 { "recovery_codes": ["xxxxx-xxxxx-xxxxx-xxxxx", … 10] }`. This
    is shown once and never retrievable again.
  - `400 invalid_totp_code` for a wrong code. `409 totp_setup_required`
    if there is no pending secret. `409 totp_already_enabled` if already
    enrolled.
- `POST /auth/2fa/disable` (**new**): turn 2FA off. Session required,
  all roles, `totp_manage` throttle.
  - Request: `{ "password": "<current password>", "code": "<TOTP or recovery code>" }`
  - `204`. Clears all four TOTP fields.
  - `400 reauthentication_failed`: one generic response for a wrong
    password **or** a wrong code. `409 totp_not_enabled`.
- Session body (`POST /auth/login`, `POST /auth/login/totp`,
  `POST /auth/refresh`, **changed**): `user` gains `totp_enabled: bool`.
  This lets the frontend show 2FA state without a status endpoint.

Update `docs/04-api/API-Specification.md` §Auth with the changed login
response, the four new endpoints above, and the `totp_enabled` field.
Update `docs/04-api/Authentication.md` §1 with a two-sentence
description of the two-step flow and recovery codes.

## Services & background tasks
`accounts/services.py`. All functions take and return model objects,
and none takes `merchant_id` from the caller.

- `begin_login(*, request, email, password) -> tuple[TeamMember, bool]`:
  calls the existing `authenticate_login` (raises `InvalidCredentials`)
  and returns `(membership, totp_required)`. The view decides between
  `login()` and storing the pending marker. The session writes stay in
  the view, like today's `login()` call.
- `make_pending_totp(user) -> dict`: builds the marker `{u, iat, n: 0}`.
- `complete_totp_login(*, pending: dict | None, code: str) -> TeamMember`:
  raises `InvalidCredentials` for every failure. The steps run in this
  order, and none of them is nested inside another transaction (see
  "Transaction sequencing" and the "step-2 auth race" fix below):
  1. Validate the marker: present, `iat` within 5 minutes, `n < 5`.
  2. Load the `User` by id (unlocked) and check `is_active`.
  3. Call `resolve_login_membership(user)` at top level (its own durable
     `user_lookup_atomic`) **only to discover which merchant's tenant
     context to open next** — its result is never trusted for the login
     decision, because it is unlocked and can go stale before the next
     step runs (see "step-2 auth race").
  4. `with tenant_context(merchant_id), tenant_atomic():`
     - Lock and re-read the `TeamMember` row itself
       (`select_for_update(of=("self",))`, filtered on
       `accepted_at__isnull=False` and `merchant__status=ACTIVE`); a
       revoke or role change racing this step now serializes on the
       same row a real revoke locks, and a row that no longer matches
       raises `InvalidCredentials`.
     - Lock the `User` row (`select_for_update`) and re-check
       `is_active` and `is_totp_enabled`.
     - Verify the code (TOTP first, then recovery code). For a recovery
       code, remove the hash and write the
       `user.totp_recovery_code_used` audit row in this **same**
       transaction, so the consumption and its audit row commit
       together.
     - Return the locked `TeamMember`.
- `record_failed_totp_attempt(pending: dict) -> dict | None`: returns
  the marker with `n + 1`, or `None` once the cap of 5 is reached. The
  attempt-cap rule lives here, not in the view. The view only persists
  the result: it stores the dict, or deletes the marker on `None` and on
  success.
- `begin_totp_setup(*, user, password) -> tuple[str, str]`: re-checks
  the password (`ReauthenticationFailed`) and raises `TotpAlreadyEnabled`
  if enrolled. It generates `pyotp.random_base32()`, stores it encrypted
  under a `User` row lock, and returns `(secret, otpauth_uri)`. There is
  no audit row, because nothing is enabled yet.
- `confirm_totp_setup(*, user, code) -> list[str]`: under a `User` row
  lock, raises `TotpAlreadyEnabled` / `TotpSetupRequired` /
  `InvalidTotpCode`. It sets `totp_confirmed_at`, `totp_last_used_step`
  and 10 fresh recovery-code hashes, then `record("user.totp_enabled",
  actor=user, target=user)` in the current tenant context. It returns
  the plaintext codes.
- `disable_totp(*, user, password, code) -> None`: under a `User` row
  lock, raises `TotpNotEnabled`. It raises `ReauthenticationFailed` for
  a wrong password or a wrong or replayed code. It clears all four
  fields, then `record("user.totp_disabled", actor=user, target=user)`.
- Private helpers:
  - `_verify_totp(user, code, now) -> bool`: tries offsets −1, 0, +1
    against `pyotp.TOTP(secret)` (`totp.at(now, counter_offset=o)` and
    step `totp.timecode(now) + o`, verified against the pyotp 2.10.0
    source). It accepts only a step greater than `totp_last_used_step`,
    then stores that step. It requires the caller to hold the `User` row
    lock. `now` must always be `django.utils.timezone.now()` (aware
    UTC): pyotp's `timecode()` reads a naive datetime as local time.
  - `_consume_recovery_code(user, code) -> bool`: removes the matching
    hash. It requires the caller to hold the row lock.
  - `_generate_recovery_codes() -> tuple[list[str], list[str]]`: returns
    `(plaintext, hashes)`.
- Canonical audit actions (new constants next to the existing
  `AUDIT_*`): `AUDIT_TOTP_ENABLED = "user.totp_enabled"`,
  `AUDIT_TOTP_DISABLED = "user.totp_disabled"`,
  `AUDIT_TOTP_RECOVERY_USED = "user.totp_recovery_code_used"`. Metadata
  is `None` for enable and disable, and
  `{"recovery_codes_remaining": <int>}` for recovery use. Codes, hashes
  and secrets are never included. The `target` is the `User` (id is a
  UUID). Add these three to the merchant-side list in
  `docs/09-security/Audit-Logging.md` §"What Gets Logged".
- New exceptions in `accounts/exceptions.py` (subclasses of
  `ReviewFlowError`): `ReauthenticationFailed` (400
  `reauthentication_failed`), `InvalidTotpCode` (400
  `invalid_totp_code`), `TotpAlreadyEnabled` (409
  `totp_already_enabled`), `TotpSetupRequired` (409
  `totp_setup_required`), `TotpNotEnabled` (409 `totp_not_enabled`).
  Step-2 login failures reuse `InvalidCredentials` (401).

`core/crypto.py` (new, about 10 lines):
- `encrypt(plaintext: str) -> str` / `decrypt(token: str) -> str` over
  `cryptography.fernet.Fernet(settings.FERNET_KEY)`.
- An empty or invalid key raises `ImproperlyConfigured` at first use.
  `decrypt` of a tampered token raises `InvalidToken`, which is not
  swallowed.
- No key rotation (`MultiFernet`) yet. Add it when a rotation procedure
  is needed.

Celery tasks: none. Beat schedules: none. Adapters/providers: none.

## Admin
- `accounts/admin.py` `UserAdmin`: add a read-only "Two-factor" fieldset
  showing `totp_confirmed_at`. Keep `totp_secret_encrypted`,
  `totp_last_used_step` and `totp_recovery_code_hashes` out of every
  fieldset, list and form. `UserEditForm` uses `fields = "__all__"`, so
  add these three to `exclude` (or list fields explicitly) so the admin
  never renders or posts them.
- No admin action to reset 2FA. A staff reset is a Phase 16 audited
  privileged action.

## Files to change
- `accounts/models.py`: four `User` fields and `is_totp_enabled`
- `accounts/services.py`: the services, helpers and audit constants
  above
- `accounts/exceptions.py`: five new exceptions
- `accounts/serializers.py`: `session_body` adds `totp_enabled`. Add
  `TotpCodeSerializer`, `TotpSetupSerializer` and
  `TotpDisableSerializer`.
- `accounts/views.py`: the `LoginView` two-step branch, plus
  `LoginTotpView`, `TotpSetupView`, `TotpConfirmView` and
  `TotpDisableView`, with the `LoginTotpRateThrottle` (Anon) and
  `TotpManageRateThrottle` (User) classes
- `accounts/urls.py`: `auth/login/totp`, `auth/2fa/setup`,
  `auth/2fa/confirm`, `auth/2fa/disable`
- `accounts/middleware.py`: add `reverse("auth-login-totp")` to
  `_pre_tenant_paths`
- `accounts/admin.py`: fieldset and form exclusions
- `accounts/tests/test_auth_api.py`: the existing login tests assert the
  session body, so update them for `user.totp_enabled`
- `config/settings.py`: `FERNET_KEY = env("FERNET_KEY", default="")`
  (read once at load, validated at first use in `core.crypto`), and
  throttle rates `"login_totp": "5/min"` and `"totp_manage": "5/min"`
- `.env.example`: a `FERNET_KEY` comment with the generate command
  (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
  Move it out of the "later phases" block.
- `requirements.txt`: add `pyotp==2.10.0` and `cryptography==50.0.1`
- `docs/04-api/API-Specification.md`: §Auth
- `docs/04-api/Authentication.md`: §1
- `docs/03-database/Data-Dictionary.md`: §User
- `docs/09-security/Audit-Logging.md`: §"What Gets Logged"

## Files to create
- `core/crypto.py`
- `core/tests/test_crypto.py`
- `accounts/migrations/0003_user_totp.py`
- `accounts/tests/test_totp_services.py`
- `accounts/tests/test_totp_api.py`

## New dependencies
- `pyotp==2.10.0`: RFC 6238 TOTP generation and verification, and the
  `otpauth://` provisioning URI.
- `cryptography==50.0.1`: `Fernet` encryption of the TOTP secret. Phase
  09 reuses it for Google OAuth tokens.

Both are pinned in `requirements.txt`. The user approved both on
2026-09-22.

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
- **Step 1 must never call Django `login()` for an enrolled user.**
  `SessionMerchantMiddleware` sets `request.merchant_id` whenever the
  user is authenticated and the session has `merchant_id`, and
  `IsMerchantMember` is the default permission. A "half logged-in"
  session would therefore be fully authorized. Step 1 flushes the
  session, then stores only `pending_totp` (no `_auth_user_id`, no
  `merchant_id`). `login()` (which cycles the session key) runs only
  after step 2 verifies a code.
- `auth/login/totp` is added to `SessionMerchantMiddleware`'s
  pre-tenant paths, and `LoginTotpView` has `authentication_classes =
  []`, `AllowAny`, and `csrf_protect` like `LoginView`.
- Step 2 derives the user only from the server-side session marker,
  never from the request body. The membership is re-resolved at step 2,
  not carried from step 1.
- Replay protection is mandatory: `pyotp.TOTP.verify(valid_window=1)` on
  its own allows reuse inside the window. Persist and compare
  `totp_last_used_step` under the `User` row lock.
- Every read-check-write of TOTP state (verify, consume recovery code,
  setup, confirm, disable) holds `select_for_update()` on the `User`
  row, so two concurrent requests with the same code or recovery code
  cannot both succeed.
- **Transaction sequencing (`core/tenancy.py`):** `tenant_atomic()` must
  be the outermost transaction of its tenant context. Inside an
  unrelated `transaction.atomic()` it becomes a savepoint, and its
  `SET LOCAL` then outlives the block. `user_lookup_atomic()` is
  `durable=True` and refuses any enclosing atomic block or tenant
  context. Therefore:
  - The User row lock is always taken inside `tenant_atomic()`, never in
    a plain `transaction.atomic()` that wraps tenant or lookup work.
    `User` has no RLS, so locking it in a tenant transaction is legal.
  - setup/confirm/disable are authenticated requests, which
    `TenantMiddleware` already runs inside the session merchant's
    `tenant_context()` + `tenant_atomic()`. The services use
    `tenant_atomic()`, which nests as a same-merchant savepoint exactly
    like the existing team services, and call `record()` inside it.
  - Step 2 is pre-tenant. It runs `resolve_login_membership()` first, at
    top level, and only then opens `tenant_context(merchant_id)` +
    `tenant_atomic()` for the lock, verification, consumption and audit
    write. It never opens `user_lookup_atomic()` inside another
    transaction.
- **Step-2 auth race:** `resolve_login_membership()` takes no row lock
  and its transaction commits before returning, so its result can go
  stale before the next step runs — a concurrent `revoke_team_member` or
  `change_team_member_role` (both lock `Merchant` → `TeamMember`) could
  commit in between. Step 2 therefore never trusts that result for the
  login decision: inside its own `tenant_atomic()` it re-locks the exact
  `TeamMember` row (`select_for_update(of=("self",))`, re-filtered on
  `accepted_at__isnull=False` and `merchant__status=ACTIVE`) before
  locking `User`. Lock order is `TeamMember` → `User`, a subset of
  `accept_invite`'s `Merchant` → `TeamMember` → `User` and consistent
  with revoke's `Merchant` → `TeamMember`, so no deadlock cycle is
  introduced. No `Merchant` lock is taken here, only the `TeamMember` row
  a revoke or role change already contends on.
- Generic errors: every step-2 failure is `401 invalid_credentials`, and
  a wrong password or wrong code on disable is one
  `400 reauthentication_failed`. Do not leak which factor failed.
- Never log, serialize (other than the one-time setup/confirm
  responses), audit, or show in admin: the TOTP secret, the
  `otpauth_uri` (it embeds the secret), TOTP codes, recovery codes or
  their hashes. `metadata_json` carries only
  `recovery_codes_remaining`.
- Use `secrets` for recovery codes and `hmac.compare_digest` for hash
  comparison.
- The TOTP parameters are the pyotp defaults (SHA1, 6 digits, 30 s),
  which all common authenticator apps support. The issuer is
  `ReviewFlow` and the account name is the user's email.
- **Do not build these:** merchant-wide 2FA enforcement, an OWNER/ADMIN
  reset of another member's 2FA, a recovery-code regenerate endpoint, a
  `GET /auth/2fa` status endpoint (`session_body.totp_enabled` covers
  it), WebAuthn/SMS/email second factors, "remember this device", key
  rotation, or invalidating the user's other sessions on enable/disable.
- **Out of scope (Phase 16):** 2FA for Django Admin staff login, and a
  staff-side audited 2FA reset.
- `POST /auth/accept-invite` is unchanged. It does not log anyone in,
  so the invitee meets the second factor at their next login.

## Definition of done
- [ ] `pytest` passes, including all existing `accounts`, `auditlog` and
      `core` tests (the login session-body assertions are updated for
      `user.totp_enabled`).
- [ ] `python manage.py migrate` applies `accounts.0003_user_totp`
      cleanly on a database with existing users. Existing users have 2FA
      off and `totp_recovery_code_hashes == []`.
- [ ] `core/crypto.py`: an encrypt/decrypt round trip works. A tampered
      token raises `InvalidToken`, and an empty `FERNET_KEY` raises
      `ImproperlyConfigured`.
- [ ] Setup: `POST /auth/2fa/setup` with the right password returns
      `secret` + `otpauth_uri`, and the database stores only the
      encrypted secret (the column value is not equal to the secret). A
      wrong password returns `400 reauthentication_failed`, and an
      enrolled user gets `409 totp_already_enabled`. A second setup
      before confirm replaces the pending secret.
- [ ] Confirm: a valid code returns 10 unique recovery codes, sets
      `totp_confirmed_at`, and writes exactly one `user.totp_enabled`
      `AuditLog` row with the session merchant. A wrong code returns
      `400 invalid_totp_code`, and having no pending secret returns
      `409 totp_setup_required`.
- [ ] Login (not enrolled): unchanged behavior, and the session body
      includes `user.totp_enabled: false`.
- [ ] Login (enrolled), step 1: `200 { totp_required: true }`, with no
      `_auth_user_id` and no `merchant_id` in the session. A wrong
      password is still the generic `401 invalid_credentials` (no
      `totp_required` leak).
- [ ] **Security, pending session is not authorized:** after step 1,
      `GET /merchant`, `GET /team-members`, `POST /auth/refresh` and
      `POST /auth/2fa/setup` all return 403.
- [ ] **Security, session fixation:** a session that was logged in as
      user A, which then completes step 1 for enrolled user B, is no
      longer authenticated as A. After step 2 the session key differs
      from the step-1 key.
- [ ] Step 2: a valid TOTP code returns `200` with the session body and
      `user.totp_enabled: true`, and `GET /merchant` then succeeds. No
      marker, a marker older than 5 minutes, a wrong code, or an
      inactive user each return the same `401 invalid_credentials`.
- [ ] **Replay:** the same TOTP code accepted once is rejected on a
      second step-2 attempt (fresh step 1), and a code from an earlier
      step than `totp_last_used_step` is rejected.
- [ ] **Attempt cap:** after 5 wrong step-2 codes, the correct code also
      fails until step 1 is repeated.
- [ ] Recovery code: a valid recovery code (in any case, with or without
      hyphens) completes step 2, is removed from
      `totp_recovery_code_hashes`, writes one
      `user.totp_recovery_code_used` row with
      `{"recovery_codes_remaining": 9}`, and fails on reuse.
- [ ] **Concurrency (real PostgreSQL):** two concurrent step-2 requests
      with the same recovery code (and, separately, the same TOTP code)
      result in exactly one success.
- [ ] **Step-2 auth race:** an OWNER revoking the enrolled user's
      `TeamMember` (or changing their role) after step 1 but before step
      2 makes step 2 fail with `401 invalid_credentials`, and the session
      body's role, when step 2 does succeed, reflects a role change made
      between step 1 and step 2 rather than the role at step 1.
- [ ] A user deactivated (`is_active=False`) between step 1 and step 2
      makes step 2 fail with `401 invalid_credentials`.
- [ ] Disable: the right password plus a TOTP or recovery code returns
      `204`, clears all four fields, and writes one `user.totp_disabled`
      row. A wrong password or wrong code returns the same
      `400 reauthentication_failed`, and a user who is not enrolled gets
      `409 totp_not_enabled`. After disabling, login is single-step
      again.
- [ ] Permissions: setup/confirm/disable are allowed for OWNER, ADMIN,
      MANAGER and VIEWER, and return 403 for an anonymous caller. Each
      acts only on `request.user`: there is no user id in any request
      body.
- [ ] Throttles: the `login_totp` scope returns `429` after 5 requests
      per minute per IP, and the `totp_manage` scope after 5 per minute
      per user.
- [ ] **Tenant isolation:** 2FA audit rows are visible only in their
      merchant's tenant context (RLS), consistent with the existing
      `auditlog` isolation tests.
- [ ] Redaction: no response other than setup/confirm contains the
      secret or recovery codes, no `AuditLog.metadata_json` contains a
      code, hash or secret, and captured log output during the TOTP API
      tests contains none of them.
- [ ] Django Admin: the User change page shows `totp_confirmed_at`
      read-only and does not render the secret, last-used step or
      recovery-code hashes.
- [ ] `requirements.txt` pins `pyotp==2.10.0` and `cryptography==50.0.1`,
      and `.env.example` documents `FERNET_KEY` generation.
- [ ] `docs/04-api/API-Specification.md` §Auth,
      `docs/04-api/Authentication.md` §1,
      `docs/03-database/Data-Dictionary.md` §User and
      `docs/09-security/Audit-Logging.md` are updated to match this
      spec.
