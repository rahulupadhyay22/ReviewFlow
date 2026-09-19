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

For local development, a management command should create: one test `Merchant` with 2+ `Location`s, a `SHARED_POOL` `WhatsAppAccount` fixture (no real Meta credentials needed for most local work — mock the provider in tests), and a `Plan`/`Subscription` pair. Build this command early (Phase 0 of the roadmap) since almost every other feature needs a merchant + location to exist.

## Testing Locally

See `Testing-Strategy.md` in this folder for what to run and when; at minimum, `pytest` (or Django's test runner) with Celery in eager mode for integration tests.
