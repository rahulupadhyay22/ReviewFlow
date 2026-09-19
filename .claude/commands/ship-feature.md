---
description: Test, commit, push, and create a PR for a completed ReviewFlow feature; squash-merge and clean up only after explicit user confirmation
allowed-tools: Read, Glob, Grep, Bash, mcp__plugin_github_github__create_pull_request, mcp__plugin_github_github__merge_pull_request, mcp__plugin_github_github__delete_branch, mcp__github__create_pull_request, mcp__github__merge_pull_request, mcp__github__delete_branch
---

The `docs/` folder is the source of truth for ReviewFlow. Follow
`docs/10-development/Coding-Standards.md` §6–§8 (migrations, testing,
PR expectations) throughout.

## Step 1 — Identify current branch
```bash
git branch --show-current
```
Store this as CURRENT_BRANCH.
If CURRENT_BRANCH is `main`, stop: "Never ship from main. Run /create-spec first."

## Step 2 — Find the spec
Read `.claude/specs/` and find the spec for CURRENT_BRANCH
(matching `feature/<feature_slug>` → `<step>-<feature_slug>.md`).
If no spec is found, stop and ask the user which spec this branch implements.

## Step 3 — Safety checks before committing
Run:
```bash
git status
git diff --staged
git diff
git log main..HEAD --oneline
```
Stop and tell the user if any of these appear in the changes:
- `.env` or any secret (`DJANGO_SECRET_KEY`, `FERNET_KEY`, `META_APP_SECRET`,
  `GOOGLE_OAUTH_CLIENT_SECRET`, Razorpay keys, OAuth tokens) —
  see `docs/09-security/Security-Controls.md`
- A migration that drops tables/columns or deletes data without a
  backup step noted in the spec — Coding-Standards §6
- Implementation of anything marked **V2 — Planned. V1 Implementation: NO.**
  in `docs/01-product/Feature-Scope.md`

## Step 4 — Run the tests
```bash
python manage.py makemigrations --check --dry-run
pytest
```
If migrations are missing or any test fails, stop and report the
failures. Never ship with failing tests.

## Step 5 — Generate commit message
Generate a Conventional Commit message:
- feat: new feature
- fix: bug fix
- chore: config or tooling
- docs: documentation only
- test: tests only

Rules:
- Lowercase
- No period at the end
- Under 72 characters
- Describes what the merchant/system can now do, not what the code does

Good: "feat: hold review requests as quota_exceeded when plan limit is hit"
Bad: "feat: added reserve_quota function to billing/services.py"

## Step 6 — Commit
```bash
git add .
git commit -m "<generated-message>"
```
Report: "✓ Committed — <message>"

## Step 7 — Push to feature branch
```bash
git push -u origin CURRENT_BRANCH
```
Report: "✓ Pushed — CURRENT_BRANCH"

## Step 8 — Create PR via GitHub MCP
Use the GitHub MCP server to create a pull request
from CURRENT_BRANCH into main.

Title: plain English feature name, no conventional commit prefix
Example: "Add Shopify order webhook ingestion"

Description:
```markdown
## What this PR does
<one paragraph from the spec overview section>

## Related architecture docs
<every docs/ file listed in the spec's "Source docs" section>

## Locked decisions touched
<copy the spec's "Locked decisions touched" section, updated for any
locked decision from docs/FINAL-ARCHITECTURE-REVIEW.md the diff
actually changes. If any line is LOCKED DECISION CHANGE, include:
"LOCKED DECISION CHANGE — USER SIGN-OFF REQUIRED">


## Changes
<bullet list of every file changed with one line description each>

## Migrations
<list new migrations; state "None" if none.
Any destructive migration must note its backup step>

## Definition of done
<copy the definition of done checklist from the spec,
mark every item as checked [x]>

## How to test
1. docker compose up -d   (Postgres + Redis)
2. python manage.py migrate
3. Run the seed data management command (test Merchant with 2+ Locations,
   SHARED_POOL WhatsAppAccount, Plan/Subscription)
4. pytest
5. python manage.py runserver
   (plus `celery -A reviewflow worker -Q events,whatsapp,google_sync,default -l info`
   and `celery -A reviewflow beat -l info` if the feature uses background tasks)
6. <specific steps from the spec to verify this feature works —
   API calls, Django Admin checks, webhook posts>
```

Report: "✓ PR created — <PR URL>"

**STOP here.** Never merge automatically. Report the PR URL and a
short summary (title, files changed, Definition of done status), then
continue with Step 9 before anything else.

## Step 9 — Locked-decision sign-off (only if a locked decision changed)
If the PR's "Locked decisions touched" contains any
LOCKED DECISION CHANGE, ask:

"This PR changes a locked architectural decision: <decision(s)>.
It requires explicit architectural sign-off before merge
(Coding-Standards §8). Do you sign off on this change?"

Wait for an explicit sign-off. A generic "continue", "ship it", or
"go ahead" is NOT architectural sign-off. Without explicit sign-off,
STOP WITHOUT MERGING and report the PR URL.

If no locked decision changed, skip to Step 10.

## Step 10 — Explicit merge confirmation
Ask the user:

"PR created successfully: <PR URL>

Do you want me to squash-merge this PR?"

Wait for explicit user confirmation. This is required even after
locked-decision sign-off in Step 9. If the user says no, not yet,
wait, review first, or anything that is not an explicit approval,
STOP WITHOUT MERGING — report the PR URL and that the branch is left
in place.

## Step 11 — Merge PR via GitHub MCP
Only after explicit confirmation in Step 10: use the GitHub MCP
server to squash-merge the pull request just created.

If the merge fails, STOP and report the error. Do not run any
cleanup step.

Report: "✓ PR merged to main"

Cleanup (Steps 12–14) runs ONLY after a successful merge.

## Step 12 — Delete remote branch via GitHub MCP
Use the GitHub MCP server to delete CURRENT_BRANCH
from GitHub after the merge.

Report: "✓ Remote branch deleted"

## Step 13 — Switch to main and pull
```bash
git checkout main
git pull origin main
```
Report: "✓ Switched to main — up to date"

## Step 14 — Delete local feature branch
```bash
git branch -D CURRENT_BRANCH
```
Report: "✓ Local branch deleted"

## Final summary
If stopped without merging (no sign-off or no merge confirmation), print:
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
/ship-feature paused — not merged
✓ Tests passed
✓ Committed — <message>
✓ Pushed — <branch>
✓ PR created — <PR URL>
⏸ Waiting for <locked-decision sign-off | merge confirmation>
Re-run /ship-feature or ask me to merge when ready
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌

After a successful merge and cleanup, print:
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
/ship-feature complete
✓ Tests passed
✓ Committed — <message>
✓ Pushed — <branch>
✓ PR created and merged
✓ Remote branch deleted
✓ Switched to main
✓ Local branch deleted
Next: run /create-spec for the next feature
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌

## Rules
- Never commit directly to main
- Never commit secrets or `.env` files
- Never ship with failing tests or missing migrations
- Never merge automatically — stop after creating the PR
- Never merge without explicit user confirmation to squash-merge
- Never merge a PR with a LOCKED DECISION CHANGE without explicit
  architectural sign-off, and still ask for merge confirmation after it
- Always use squash merge
- Run cleanup only after a successful merge; then always delete both
  remote and local branch
- If GitHub MCP is not connected stop and say:
  "GitHub MCP is not connected. Run /mcp to check connection."
- If push fails due to no upstream, use git push -u origin CURRENT_BRANCH
- Never proceed to merge if PR creation fails
