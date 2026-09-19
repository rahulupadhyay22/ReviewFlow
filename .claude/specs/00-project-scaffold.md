# Spec: Project Scaffold

## Overview
Creates the empty, runnable Django project that every later ReviewFlow
phase builds on: the `config/` package (env-driven settings, urls,
asgi/wsgi, `celery.py`), a `docker-compose.yml` for local Postgres and
Redis, Celery worker/Beat wiring with the four locked queues
(`events`, `whatsapp`, `google_sync`, `default`), and pytest +
pytest-django running against real PostgreSQL. It exists first because
no app, model, or test can be written without it. It belongs to no
single plane — it is the shared foundation for ingestion, automation,
and intelligence. It adds no Django apps, no models, and no business
logic.

## Source docs
- `docs/ROADMAP.md` — §"00 — Project scaffold & dev environment"
- `docs/10-development/Development-Setup.md` — Prerequisites, Local
  Services via Docker Compose, Environment Variables, Running the App
  Locally, Running Background Workers Locally, Seed Data, Testing Locally
- `docs/02-architecture/SAD.md` — §3 (Django App Structure), §5
  (Background Processing), §9 (Deployment Architecture)
- `docs/02-architecture/Security-Architecture.md` — §"Encryption & Secrets"
- `docs/09-security/Security-Controls.md` — §"Encryption & Secrets"
- `docs/02-architecture/Multi-Tenancy.md` — §"No Standing Privileged Role"
  (constraint noted for Phase 01; see Rules)
- `docs/10-development/Testing-Strategy.md` — §"Tooling"
- `docs/10-development/Coding-Standards.md` — §5, §6

## Depends on
Nothing. This is the first phase (`docs/ROADMAP.md`: Depends on "—").

## Roadmap Phase
- Phase: 00 — Project scaffold & dev environment
- Completes entire phase: Yes

This spec covers all five bullets of the Phase 00 section: `config/`
package, `docker-compose.yml`, env-based settings, Celery worker/Beat
wiring with the four queues, and pytest + pytest-django configuration.

Note: `Development-Setup.md` §"Seed Data" says to build the seed command
"early (Phase 0)", but a seed command needs `Merchant`/`Location` models.
`docs/ROADMAP.md` (the sequencing authority) places it in Phase 03 and
extends it in Phases 07 and 08. It is out of scope here.

## Locked decisions touched
- Poll-based dispatch, no broker-side `eta` scheduling (`Architecture.md`
  §Foundational Decisions 4, `SAD.md` §5) — NO CHANGE (no tasks yet;
  Celery is wired only)
- Celery queues `events` / `whatsapp` / `google_sync` / `default`, worker
  and Beat as separate processes (`SAD.md` §5, §9) — DEPENDS ON
- Redis as broker/result backend only, never source of truth (`SAD.md` §5)
  — DEPENDS ON
- PostgreSQL as single source of truth (`Architecture.md` §Executive
  Summary) — DEPENDS ON
- Secrets from environment only, never committed (`Security-Architecture.md`
  §Encryption & Secrets) — DEPENDS ON
- Customized Django Admin, no bespoke admin app (`Architecture.md`
  §Foundational Decisions 6) — DEPENDS ON (default admin URL only)
- Multi-tenancy / RLS / no standing privileged role
  (`FINAL-ARCHITECTURE-REVIEW.md` §8, `Multi-Tenancy.md`) — NO CHANGE
  (implemented in Phase 01)

None are LOCKED DECISION CHANGE.

Command change (not a locked decision; approved by the user during
spec creation): the Celery app lives in `config/celery.py` per `SAD.md`
§3 and `ROADMAP.md` Phase 00, so the documented worker/Beat commands
change from `celery -A reviewflow ...` to `celery -A config ...`.

## Django apps
- No Django apps are created. `config/` is the Django project package,
  not an app.
- `INSTALLED_APPS` contains only Django's defaults (`admin`, `auth`,
  `contenttypes`, `sessions`, `messages`, `staticfiles`). DRF,
  `core`, and every other app are added in the phase that first uses
  them.

## Models & database changes
No database changes. No models, no custom migrations. Django's built-in
migrations (auth, admin, sessions, contenttypes) apply unchanged.

## API endpoints
No new endpoints. `config/urls.py` exposes only Django Admin at
`admin/` (staff-only by Django default; IP allowlist is Phase 16).

## Services & background tasks
- No `services.py` functions.
- `config/celery.py`: `app = Celery("reviewflow")`,
  `app.config_from_object("django.conf:settings", namespace="CELERY")`,
  `app.autodiscover_tasks()`. `config/__init__.py` imports `app` so it
  loads with Django.
- Celery settings in `config/settings.py`:
  - `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND` from `REDIS_URL`
  - `CELERY_TASK_QUEUES`: exactly `events`, `whatsapp`, `google_sync`,
    `default`
  - `CELERY_TASK_DEFAULT_QUEUE = "default"`
  - `CELERY_BEAT_SCHEDULE = {}` — Beat entries (`dispatch_due_executions`,
    `retry_failed_events`, etc.) are added by the phases that build those
    tasks
  - `CELERY_TIMEZONE = "UTC"`
- No Celery tasks. No adapter/provider interfaces.

## Admin
No admin changes (default Django Admin site mounted at `admin/`).

## Files to change
- `CLAUDE.md` — §Commands: `celery -A reviewflow` → `celery -A config`;
  Python version pinned (3.12)
- `.claude/commands/ship-feature.md` — same Celery command fix (lines
  ~177–178)
- `docs/10-development/Development-Setup.md` — §"Running Background
  Workers Locally": same Celery command fix; §Prerequisites: pinned
  Python 3.12 (user-approved; `docs/` edits prompt for permission)

## Files to create
- `manage.py`
- `config/__init__.py` — imports the Celery `app`
- `config/settings.py` — single env-driven settings module
- `config/urls.py`
- `config/wsgi.py`
- `config/asgi.py`
- `config/celery.py`
- `config/tests/__init__.py`
- `config/tests/test_scaffold.py`
- `conftest.py` — root pytest conftest (Celery eager mode for tests)
- `docker-compose.yml` — `postgres:16` and `redis:7`, as in
  `Development-Setup.md`
- `.env.example` — every variable from `Development-Setup.md`
  §Environment Variables with placeholder values, plus `DJANGO_DEBUG`
  and `DJANGO_ALLOWED_HOSTS`
- `requirements.txt` — pinned versions
- `pyproject.toml` — `requires-python` and `[tool.pytest.ini_options]`
  (`DJANGO_SETTINGS_MODULE = "config.settings"`)
- `.python-version` — `3.12`

## New dependencies
All new (the repo has no dependencies yet). `requirements.txt` contains
exactly these six direct dependencies, each pinned with `==`. Versions
were checked against PyPI on 2026-09-19 for Python 3.12 + Django 5.2:

| Package | Pin | Why | Compatibility |
|---|---|---|---|
| `Django` | `==5.2.17` | web framework (locked stack) | latest 5.2 LTS patch; supports 3.12 |
| `celery[redis]` | `==5.6.3` | worker/Beat + Redis transport (locked stack) | supports 3.12 |
| `psycopg[binary]` | `==3.3.6` | PostgreSQL driver (locked stack) | supports 3.12 and Django 5.2 |
| `django-environ` | `==0.14.0` | reads `.env`, parses `DATABASE_URL` / `REDIS_URL` (one package instead of `python-dotenv` + `dj-database-url`) | classifiers list Django 5.2, Python 3.12 |
| `pytest` | `==9.1.1` | test runner (`Testing-Strategy.md` §Tooling) | supports 3.12 |
| `pytest-django` | `==4.14.0` | Django integration for pytest | classifiers list Django 5.2, Python 3.12; requires `pytest>=7` |

Transitive dependencies (e.g. `kombu`, `redis`, `billiard`,
`psycopg-binary`) are resolved by pip from these pins and are not listed
in `requirements.txt` in this phase.

Dependency policy:
- Every entry in `requirements.txt` uses an exact `==` pin. No ranges, no
  unpinned packages.
- Django stays on the 5.2.x LTS line. Do not install Django 6.x.
- No dependency beyond the six above may be added during implementation.
  Adding one requires updating this section and getting explicit user
  approval first.
- If a pinned version fails to install or turns out to be incompatible,
  stop and report it. Do not swap in another version on your own.

Deliberately not added yet: `djangorestframework` (Phase 02, first
endpoints), `cryptography`/Fernet (first encrypted field),
`factory_boy` (first models), `pip-audit` (Phase 18 CI).

## Rules for implementation
- Django + DRF monolith; business logic only in `services.py`, never in
  views/serializers (no business logic exists in this phase)
- Every tenant-owned model uses `core.TenantScopedManager`; RLS enabled
  on its table (no models in this phase — Phase 01)
- Never trust client-supplied `merchant_id`/`location_id` — derive from
  the authenticated principal (no endpoints in this phase)
- Celery tasks take `merchant_id` explicitly and set tenant context first
  (no tasks in this phase)
- Idempotency via database unique constraints, not check-then-insert
- Role checks via DRF permission classes (OWNER/ADMIN/MANAGER/VIEWER)
- External IDs are UUIDs/hashids, never sequential integers
- Secrets/OAuth tokens encrypted (Fernet), never hardcoded or committed
- Status enums in `UPPER_SNAKE_CASE`; timestamps suffixed `_at`
- No V2 features, no bespoke admin app, no broker-side `eta` scheduling
- Every new service function has a unit test; every adapter/webhook has
  a fixture-based contract test

Phase-specific rules:
- One settings module (`config/settings.py`), configured entirely from
  environment variables via `django-environ`. No `base/dev/prod` split.
- `DJANGO_SECRET_KEY`, `DATABASE_URL`, and `REDIS_URL` are required: no
  default values, settings fail to load if missing. `DJANGO_DEBUG`
  defaults to `False`.
- `DATABASES` must be PostgreSQL. No SQLite fallback, including in tests
  (RLS and concurrency tests in later phases require Postgres).
- `USE_TZ = True`, `TIME_ZONE = "UTC"`.
- `.env` is never committed (already in `.gitignore`); `.env.example`
  contains only placeholders, never real secrets.
- Tests run Celery in eager mode (`CELERY_TASK_ALWAYS_EAGER = True`,
  `CELERY_TASK_EAGER_PROPAGATES = True`) via the root `conftest.py`, not
  in production settings.
- Do not add `FERNET_KEY`, `META_*`, or `GOOGLE_*` to settings yet — they
  are listed in `.env.example` only, and read in the phase that uses them.
- Phase 01 constraint to preserve: the docker-compose `POSTGRES_USER`
  is a Postgres superuser, and superusers bypass RLS. Phase 00 does not
  change this. Phase 01 must connect the app through a non-superuser,
  non-`BYPASSRLS` role (`Multi-Tenancy.md` §No Standing Privileged Role).
  Do not build anything here that assumes superuser access.

## Definition of done
- [ ] `requirements.txt` lists exactly the six direct dependencies from
      §New dependencies, each with an exact `==` pin; `pip install -r
      requirements.txt` succeeds on Python 3.12 and
      `python -m django --version` prints `5.2.17`
- [ ] `docker compose up -d` starts Postgres 16 and Redis 7;
      `docker compose ps` shows both running
- [ ] With a `.env` copied from `.env.example` (real values filled in),
      `python manage.py check` reports no issues
- [ ] `python manage.py makemigrations --check --dry-run` reports no
      changes
- [ ] `python manage.py migrate` applies Django's built-in migrations to
      Postgres
- [ ] `python manage.py runserver` serves `/admin/` login page
- [ ] `celery -A config worker -Q events,whatsapp,google_sync,default -l info`
      starts and lists all four queues in its banner
- [ ] `celery -A config beat -l info` starts as a separate process
- [ ] `pytest` passes, including `config/tests/test_scaffold.py`:
  - [ ] the test database connection vendor is `postgresql`
  - [ ] the Celery app's configured queue names are exactly
        `{events, whatsapp, google_sync, default}` and the default queue
        is `default`
  - [ ] the Celery broker URL comes from `REDIS_URL`
  - [ ] Celery runs in eager mode under pytest
  - [ ] loading settings without `DJANGO_SECRET_KEY` fails (no default
        secret key)
  - [ ] `DEBUG` is `False` when `DJANGO_DEBUG` is unset
- [ ] `git status` shows no `.env` file tracked; `.env.example` contains
      no real secrets
- [ ] `CLAUDE.md`, `.claude/commands/ship-feature.md`, and
      `docs/10-development/Development-Setup.md` use `celery -A config`
- [ ] Testing-Strategy priority scenarios touched: none (no tenant data,
      webhooks, campaigns, or quota exist yet)
