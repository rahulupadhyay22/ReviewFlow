---
description: Writes and runs tests for a specific ReviewFlow feature. Pass the spec name as argument e.g. /test-feature 03-shopify-ingestion
argument-hint: "Spec name e.g. 03-shopify-ingestion"
allowed-tools: Read, Glob, Agent, Bash(git branch:*), Bash(git diff:*), Bash(git status:*)
---

Run the full testing pipeline for the feature specified in
$ARGUMENTS. The `docs/` folder is the source of truth for ReviewFlow;
tests follow `docs/10-development/Testing-Strategy.md`.

If no argument is provided, stop immediately and say:
"Please provide a spec name. Usage: /test-feature <spec-name>
e.g. /test-feature 03-shopify-ingestion"

## Pre-flight Check

1. Strip any trailing `.md` from $ARGUMENTS. If
   `.claude/specs/<spec-name>.md` does not exist, list the files in
   `.claude/specs/` and stop, saying:
   "Spec file not found at .claude/specs/<spec-name>.md. Please check
   the spec name and try again."

2. Run once each:
   ```bash
   git branch --show-current
   git diff main...HEAD --stat
   git diff HEAD --stat
   ```
   If there are no code changes (only `docs/`/`.claude/` or nothing),
   stop and say: "No implementation found. Implement the feature
   before running /test-feature."

---

## Step 1: Write Tests

Invoke the **reviewflow-test-writer** subagent **once** with:

- Spec file: `.claude/specs/<spec-name>.md`
- Branch name and the list of changed files from the pre-flight
  (implementation to read for real names, signatures, URLs, and
  exceptions only)
- Instruction: "Write pytest tests based on what the spec and docs/
  say the feature SHOULD do — not on what the implementation does.
  Cover every Definition of done item, plus the Testing-Strategy.md
  priority scenarios this feature touches (tenant isolation, webhook
  signature + idempotency, adapter contract fixtures, role
  permissions, campaign/dispatch concurrency, quota states, opt-out,
  frequency cap, audit logging — whichever apply). Place tests in
  `<app>/tests/test_<topic>.py` following the existing layout; reuse
  existing fixtures and keep new ones feature-local per your Fixture
  Rules. Mock an external provider only when the feature actually
  interacts with that provider — do not introduce unused WhatsApp,
  Google, Razorpay, Shopify, or other provider mocks into unrelated
  tests. Follow your Run Contract and return one report listing every
  file written."

  Example — Shopify ingestion feature: Shopify provider mock YES;
  WhatsApp/Google/Razorpay mocks only if the feature directly
  interacts with them.

Wait for reviewflow-test-writer to fully complete. From its report,
record the exact list of **test files written**.

**Do NOT proceed to Step 2 if** the writer reports any of:
- no test files written;
- "No spec found" or "Implementation not found";
- a collection error it could not resolve;
- a missing test dependency (e.g. pytest-django).

Instead, stop and show the writer's report and its blockers.

---

## Step 2: Run Tests

Invoke the **reviewflow-test-runner** subagent **once** with:

- Test files to execute: exactly the files listed in the writer's report
- Spec file: `.claude/specs/<spec-name>.md`
- Instruction: "First verify every listed test file exists using
  Glob or Read — do not trust the writer's report. Run ONLY these
  test files — do NOT run the full suite. Use `python -m pytest <files> -q -rfEs --tb=short
  -p no:cacheprovider`. Follow your Run Contract (max 2 runs).
  Classify each failure as Implementation bug, Test bug, Missing
  implementation, Environment/configuration, or Possibly flaky, and cite the
  docs/ rule it touches."

---

## Handoff Rules

- Do NOT start Step 2 until Step 1 is fully complete
- Each subagent is invoked exactly once — never retry a failed
  subagent, never loop write → run → rewrite
- Do NOT fix any code or tests regardless of the results
- Do NOT run any tests beyond the files the writer produced
- Do NOT start services or install packages; if the runner reports
  an Environment blocker, pass its suggested command to the user
- If either subagent fails or returns no output, report which one
  failed and do not present a partial result as complete

---

## Final Output

After both subagents complete, produce a combined summary:

### Testing Pipeline Report — <spec-name>

**Step 1 — Tests Written**
- `<file>` — `<test_name>` — <which spec requirement / Testing-Strategy
  scenario it validates>
- Definition of done items NOT covered: <list or "none">

**Step 2 — Test Results**
- Mirror the reviewflow-test-runner's structured report

**Verdict**
One of:
- ✅ Ready for code review — all tests pass. Next: `/code-review-feature <spec-name>`
- ❌ Needs fixes — list each failing test with its classification and root cause
- ⚠️ Blocked — environment or missing dependency; show the command to run
  (e.g. `docker compose up -d`)
