---
description: Create a spec file and feature branch for the next ReviewFlow step
argument-hint: "Step number and feature name e.g. 1 tenant-core"
allowed-tools: Read, Write, Glob, Grep, Bash(git:*)
---

You are a senior Django developer spinning up a new feature for
ReviewFlow (WhatsApp review-request automation + Google review sync).
The `docs/` folder is the single source of truth. If `CLAUDE.md`
exists, follow it too — but where it conflicts with `docs/`, `docs/` wins.

User input: $ARGUMENTS

## Step 1 — Check working directory is clean
Run `git status`.
- If this is not a git repository, stop and tell the user to run
  `git init` and make an initial commit on `main` first.
- If there are uncommitted, unstaged, or untracked files, stop
  immediately and tell the user to commit or stash them.
DO NOT CONTINUE until the working directory is clean.

## Step 2 — Parse the arguments
From $ARGUMENTS extract:

1. `step_number` — zero-padded to 2 digits: 0 → 00, 2 → 02, 11 → 11

2. `feature_title` — human readable title in Title Case
   - Example: "Tenant Core" or "Shopify Integration"

3. `feature_slug` — git and file safe slug
   - Lowercase, kebab-case
   - Only a-z, 0-9 and -
   - Maximum 40 characters
   - Example: tenant-core, shopify-integration

4. `branch_name` — format: `feature/<feature_slug>`
   - Example: `feature/tenant-core`

If you cannot infer these from $ARGUMENTS, ask the user
to clarify before proceeding.

## Step 3 — Check branch name is not taken
Run `git branch` to list existing branches.
If `branch_name` is already taken, append a number:
`feature/tenant-core-01`, `feature/tenant-core-02` etc.

## Step 4 — Switch to main and pull latest
Run:
```
git checkout main
git pull origin main
```
If there is no `origin` remote, skip the pull and say so.

## Step 5 — Create and switch to the feature branch
Run:
```
git checkout -b <branch_name>
```

## Step 6 — Research the docs and codebase
Always read, in this order:
- `docs/README.md` — doc map and locked decisions
- `docs/ROADMAP.md` — phase numbers, dependencies, and status
- `docs/FINAL-ARCHITECTURE-REVIEW.md` — locked decisions (never contradict)
- `docs/01-product/Feature-Scope.md` — V1 vs V2 source of truth
- `docs/02-architecture/SAD.md` — Django app structure, Celery queues
- `docs/02-architecture/Multi-Tenancy.md` — tenant scoping and RLS
- `docs/10-development/Coding-Standards.md`
- `docs/10-development/Testing-Strategy.md`
- All files in `.claude/specs/` — avoid duplicating existing specs

Then read every doc relevant to the feature, e.g.:
- Models/fields → `docs/03-database/Database-Design.md`, `Data-Dictionary.md`, `ERD.md`
- Endpoints → `docs/04-api/API-Specification.md`, `Authentication.md`, `Webhook-Specification.md`
- Integrations → `docs/05-integrations/Integration-Architecture.md`
- Sale pipeline, campaigns, WhatsApp, Google → `docs/06-automation/*`
- Quota, plans, Razorpay → `docs/08-billing/Billing-Specification.md`
- Security, audit, retention → `docs/09-security/*`
- Roles, flows, rules → `docs/01-product/*`

Also inspect existing code (Django apps, `models.py`, `services.py`,
migrations) so the spec builds on what exists rather than re-creating it.

Stop and warn the user if:
- `step_number` is not a phase in `docs/ROADMAP.md`, or the feature
  does not belong to that phase.
- That phase is already marked Done in `docs/ROADMAP.md`.
- Any phase it depends on (per the ROADMAP "Depends on" column) is not
  marked Done. Never spec work from a later phase ahead of its
  dependencies.
- The feature is marked **V2 — Planned. V1 Implementation: NO.**
  in `Feature-Scope.md` (a field existing in the Data Dictionary
  does NOT make it V1 — e.g. `GoogleReview.reply_text`).
- A spec for this feature already exists in `.claude/specs/`.

Do not stop — but warn the user — if the feature would change a
locked decision in `FINAL-ARCHITECTURE-REVIEW.md` or `Architecture.md`.
Record it in the spec as `LOCKED DECISION CHANGE` (see Step 7). Never
silently reinterpret or modify a locked decision.

## Step 7 — Write the spec
Generate a spec document with this exact structure:

---
# Spec: <feature_title>

## Overview
One paragraph describing what this feature does, why it exists
at this stage of ReviewFlow's build, and which plane it belongs
to (ingestion / automation / intelligence).

## Source docs
Every `docs/` file (with section) this spec is derived from.

## Depends on
Which previous steps/specs must be complete (e.g. tenant core,
seed data command, `Merchant`/`Location` models).

## Locked decisions touched
Every locked architectural decision from `docs/` that this feature
relies on or affects (e.g. multi-tenancy model, `merchant_id`
isolation, `TenantScopedManager`, PostgreSQL RLS, `GoogleConnection`
merchant-level architecture, `GoogleLocation` location-level mapping,
one review request per transaction, quota reservation, WhatsApp
provider architecture, integration architecture, billing behavior,
Celery merchant context). One line each:
- `<decision>` (`<doc>` §) — DEPENDS ON | NO CHANGE | LOCKED DECISION CHANGE

- **DEPENDS ON** — feature uses the decision without changing it.
- **NO CHANGE** — feature does not modify the decision.
- **LOCKED DECISION CHANGE** — feature proposes changing it.

If any line is LOCKED DECISION CHANGE, this section must also contain,
on its own line:
`LOCKED DECISION CHANGE — USER SIGN-OFF REQUIRED`
followed by what would change and why.
If none apply, state "None".

## Django apps
Which apps from `SAD.md` §3 are created or touched
(e.g. `core`, `accounts`, `campaigns`).

## Models & database changes
New models, fields, constraints, indexes, and RLS policies.
Always verify names/types against `docs/03-database/Data-Dictionary.md`
and existing migrations before writing this. Call out:
- Unique constraints used for idempotency
- `merchant_id` scoping and the RLS policy per tenant-owned table
If none: state "No database changes".

## API endpoints
Every new endpoint, matching `docs/04-api/API-Specification.md`:
- `METHOD /path` — description — auth (session / API key / webhook signature / public) — roles allowed

If no new endpoints: state "No new endpoints".

## Services & background tasks
- `services.py` functions (name, signature, what they raise)
- Celery tasks (name, queue: `events` / `whatsapp` / `google_sync` / `default`)
- Celery Beat schedules, if any
- Adapter/provider interfaces implemented (`BaseAdapter`, `WhatsAppProvider`, `GoogleSyncProvider`)

## Admin
Django Admin registrations/customizations needed. If none: state "No admin changes".

## Files to change
Every file that will be modified.

## Files to create
Every new file that will be created.

## New dependencies
Any new pip packages. If none: state "No new dependencies".

## Rules for implementation
Specific constraints Claude must follow. Always include:
- Django + DRF monolith; business logic only in `services.py`, never in views/serializers
- Every tenant-owned model uses `core.TenantScopedManager`; RLS enabled on its table
- Never trust client-supplied `merchant_id`/`location_id` — derive from the authenticated principal
- Celery tasks take `merchant_id` explicitly and set tenant context first
- Idempotency via database unique constraints, not check-then-insert
- Role checks via DRF permission classes (OWNER/ADMIN/MANAGER/VIEWER)
- External IDs are UUIDs/hashids, never sequential integers
- Secrets/OAuth tokens encrypted (Fernet), never hardcoded or committed
- Status enums in `UPPER_SNAKE_CASE`; timestamps suffixed `_at`
- No V2 features, no bespoke admin app, no broker-side `eta` scheduling
- Every new service function has a unit test; every adapter/webhook has a fixture-based contract test

## Definition of done
A specific testable checklist. Each item must be verifiable by
running `pytest`, a management command, Django Admin, or an API
call. Always include the relevant priority scenarios from
`Testing-Strategy.md` (tenant isolation, duplicate webhook,
duplicate campaign execution, etc.) that this feature touches.
---

## Step 8 — Save the spec
Save to: `.claude/specs/<step_number>-<feature_slug>.md`

## Step 9 — Report to the user
Print a short summary in this exact format:
```
Branch:    <branch_name>
Spec file: .claude/specs/<step_number>-<feature_slug>.md
Title:     <feature_title>
```

If the spec contains `LOCKED DECISION CHANGE`, add this line:
```
⚠ LOCKED DECISION CHANGE — USER SIGN-OFF REQUIRED: <decision(s)>
```

Then tell the user:
"Review the spec at `.claude/specs/<step_number>-<feature_slug>.md`
then enter Plan Mode with Shift+Tab twice to begin implementation."

Do not print the full spec in chat unless explicitly asked.
