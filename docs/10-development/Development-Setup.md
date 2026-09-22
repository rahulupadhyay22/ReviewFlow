# Development Setup

## Prerequisites

- Python 3.12 (pinned in `.python-version` and `pyproject.toml`)
- PostgreSQL (local via Docker recommended, matching the Supabase Postgres version used in production)
- Redis (local via Docker)
- Node.js (for the Next.js frontend, separate repo/package)

## Local Services via Docker Compose

Run Postgres and Redis locally so the Django app and Celery workers connect to real services rather than mocks:

```yaml
# docker-compose.yml (illustrative — finalize at project init)
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: reviewflow
      POSTGRES_USER: reviewflow
      POSTGRES_PASSWORD: local_dev_only
    ports: ["5432:5432"]
    volumes: ["./docker/postgres/init:/docker-entrypoint-initdb.d:ro"]
  redis:
    image: redis:7
    ports: ["6379:6379"]
```

`POSTGRES_USER` is a superuser and is for manual admin only. `docker/postgres/init/01-app-role.sql` creates `reviewflow_app` (no superuser, no `BYPASSRLS`; `CREATEDB` only for the test database), which the application connects as so RLS applies (see `../02-architecture/Multi-Tenancy.md` §"No Standing Privileged Role"). Init scripts run only on a fresh data directory: after pulling this change, run `docker compose down` then `docker compose up -d`, then `python manage.py migrate`.

**One-time reset for the custom user model (Phase 02).** `AUTH_USER_MODEL = "accounts.User"` cannot be applied to a database where `auth` already migrated. Reset a pre-Phase-02 local database once — `docker compose exec postgres psql -U reviewflow -d postgres -c "DROP DATABASE reviewflow;" -c "CREATE DATABASE reviewflow OWNER reviewflow_app;"` (or `docker compose down` + `docker compose up -d`) — then `python manage.py migrate`. Set `DJANGO_SECURE_COOKIES=False` in `.env` for local http.

## Environment Variables

See `../02-architecture/Security-Architecture.md` for what must never be committed. Minimum local `.env`:

```
DJANGO_SECRET_KEY=...
DATABASE_URL=postgres://reviewflow_app:local_dev_only@localhost:5432/reviewflow
REDIS_URL=redis://localhost:6379/0
FERNET_KEY=...
META_APP_ID=...
META_APP_SECRET=...
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
```

Payment gateway keys are added once the provider is confirmed (see the blueprint's "Remaining Decisions").

## Running the App Locally

```bash
pip install -r requirements.txt   # or poetry/uv equivalent
python manage.py migrate
python manage.py createsuperuser  # for Django Admin access
python manage.py runserver
```

## Running Background Workers Locally

Celery worker and Beat must run as **separate processes** from the web server, mirroring production (see `../02-architecture/SAD.md` §9):

```bash
celery -A config worker -Q events,whatsapp,google_sync,default -l info
celery -A config beat -l info
```

## Seed Data

```bash
python manage.py seed_dev [--password <pw>]
```

Creates one test `Merchant` ("Seed Cafe") with an accepted OWNER, 2 active
`Location`s, and an accepted `MANAGER` assigned to the first location — enough
for MANAGER location scoping and every other Phase 03+ feature that needs a
merchant + location to exist. It goes through the ordinary service functions
(`create_merchant_with_owner`, `create_location`, `invite_team_member`,
`accept_invite`, `set_team_member_locations`), never raw ORM writes.

- **`DEBUG`-only**: refuses to run (raises `CommandError`, writes nothing)
  unless `settings.DEBUG` is true.
- **Idempotent**: if the seed owner (`owner@seed.reviewflow.local`) already
  exists, it prints "already seeded" and exits without creating anything new.
- **Password**: `--password` if given, else a random one generated with
  `secrets.token_urlsafe`. Printed once to stdout, never hardcoded or logged
  elsewhere.

The `SHARED_POOL` `WhatsAppAccount` fixture (Phase 08) and the
`Plan`/`Subscription` pair (Phase 07) are not created by this command yet —
those later phases extend it.

## Testing Locally

See `Testing-Strategy.md` in this folder for what to run and when; at minimum, `pytest` (or Django's test runner) with Celery in eager mode for integration tests.
