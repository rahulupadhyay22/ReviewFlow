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
RAZORPAY_KEY_ID=...
RAZORPAY_KEY_SECRET=...
RAZORPAY_WEBHOOK_SECRET=...
```

The payment gateway is Razorpay. The three `RAZORPAY_*` values are read by the billing app (Phase 07); use Razorpay **test-mode** keys locally. `RAZORPAY_KEY_SECRET` and `RAZORPAY_WEBHOOK_SECRET` are secrets; `RAZORPAY_KEY_ID` is the public key id. They default to empty: with them unset, selecting an offered plan at checkout returns `503 billing_not_configured` and the billing webhook rejects every request, while the rest of the API is unaffected. A plan that is not offered (unknown, retired, or without a `provider_plan_id`) is rejected first, with `422 validation_error`, whether or not the credentials are set. `RAZORPAY_SUBSCRIPTION_TOTAL_COUNT` is optional (default `1200`, the number of monthly cycles in Razorpay's 100-year maximum). The plan-change replacement is off by default: `BILLING_REPLACEMENT_UPGRADE_ENABLED` and `BILLING_REPLACEMENT_DOWNGRADE_ENABLED` (booleans, default `false`) enable each kind independently, and `BILLING_REPLACEMENT_UPGRADE_AUTH_WINDOW` and `BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN` are durations in **seconds** with no default (a kind that is enabled without its duration returns `503 billing_not_configured`). They are listed, unset, in `.env.example`; keep them off until the release gates in the replacement spec are closed. The webhook URL registered in Razorpay is `POST /api/v1/billing/webhooks/razorpay`, subscribed to the `subscription.*` events.

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
  exists, it prints "already seeded" and does not recreate the merchant,
  locations or users. The billing fixtures below are a separate step with
  their own check, so a database seeded before Phase 07 gains them on the
  next run (it then also prints that they were added), and a further run
  changes nothing.
- **Password**: `--password` if given, else a random one generated with
  `secrets.token_urlsafe`. Printed once to stdout, never hardcoded or logged
  elsewhere.

**Billing fixtures (Phase 07).** The command also creates four `Plan` rows
(`Starter` 100, `Growth` 500, `Pro` 2000, `Business` 10000 requests per
period, each with `monthly_price = 0` and no `provider_plan_id`) and, for
the seed merchant, an `ACTIVE` `Subscription` on `Growth` with a one-year
period and its `UsageRecord`. **These are development fixtures only. They
are not pricing and not quota decisions**, and nothing in them may be
copied into production data: production plans are Razorpay plans entered
in Django Admin with their `provider_plan_id`. The seed is the only code
that creates an `ACTIVE` subscription without a paid invoice, because it
has no provider; the row has no provider reference, so cancel on it
answers `409 subscription_provider_state_unsupported`, checkout of one of
the seed plans answers `422 validation_error` (they have no
`provider_plan_id`, so they are not offered), and `GET /merchant` shows
`plan: { id, name }` for it.

Phase 08 adds the platform's single `SHARED_POOL` `WhatsAppAccount`
(obviously fake ids `DEV-SHARED-PHONE-ID` / `DEV-SHARED-WABA-ID`, status
`ACTIVE`) and maps both seed locations to it. These are development
values only: the account has no real Meta credentials, so no template
submission or send works against it. The shared row is written through
the audited platform path (`core.tenancy.platform_write_atomic`), never
by a tenant. In production, set the real sender with
`python manage.py configure_shared_pool --phone-number-id <id>
--business-account-id <id> [--status ACTIVE]`; it is idempotent, writes
one platform `AuditLog` row per real change, and reads the Meta access
token from `META_SHARED_POOL_ACCESS_TOKEN` (an environment variable, never
an argument).

## Testing Locally

See `Testing-Strategy.md` in this folder for what to run and when; at minimum, `pytest` (or Django's test runner) with Celery in eager mode for integration tests.
