---
description: Runs parallel security and quality code review for a specific ReviewFlow feature. Pass the spec name as argument e.g. /code-review-feature 03-shopify-ingestion
argument-hint: "Spec name e.g. 03-shopify-ingestion"
allowed-tools: Read, Glob, Agent, Bash(git diff:*), Bash(git branch:*), Bash(git status:*)
---

Run the full code review pipeline for the feature specified in
$ARGUMENTS. The `docs/` folder is the source of truth for ReviewFlow.

If no argument is provided, stop immediately and say:
"Please provide a spec name. Usage: /code-review-feature
<spec-name> e.g. /code-review-feature 03-shopify-ingestion"

## Pre-flight Check

1. **Spec exists.** Strip any trailing `.md` from $ARGUMENTS and
   check `.claude/specs/<spec-name>.md` exists. If not, list the
   files in `.claude/specs/`, say the spec was not found, and stop.

2. **Require a feature branch.** Run `git branch --show-current`.
   If the current branch is `main`, STOP immediately and say:
   "Code review requires a feature branch. Switch to the feature
   branch created by /create-spec (e.g. `git checkout feature/<slug>`)
   and re-run /code-review-feature <spec-name>."
   Do not fall back to reviewing working-tree changes on `main`.
   If a local `main` branch does not exist, stop and say the review
   needs `main` as its base.

3. **Collect the changed set.** Run once each:
   ```bash
   git diff main...HEAD --stat
   git diff HEAD --stat
   ```
   The changed set is the union of committed branch changes and
   uncommitted (staged + unstaged) changes.

4. If the changed set is empty, stop immediately and say:
   "No changes detected. Implement the feature before running
   code review."

5. If the only changes are under `docs/` or `.claude/`, say so and
   stop — there is no code to review.

---

## Step 1: Parallel Review

Invoke both subagents **in a single message** so they run in
parallel. Each subagent is invoked **exactly once**.

**reviewflow-security-reviewer** receives:
- Spec file: `.claude/specs/<spec-name>.md`
- Branch name and the list of changed files from the pre-flight
- Instruction: "Review only the changed code for security against
  docs/ (tenant isolation + RLS, auth mechanisms, permissions,
  webhook fail-closed verification, secrets/token encryption, PII
  logging, IDOR, rate limits, audit logging, messaging compliance).
  Verify delegated controls (permission classes, managers,
  middleware, RLS policies) before reporting anything as missing.
  Do not comment on quality or style. Follow your Run Contract and
  return one report."

**reviewflow-quality-reviewer** receives:
- Spec file: `.claude/specs/<spec-name>.md`
- Branch name and the list of changed files from the pre-flight
- Instruction: "Review only the changed code for quality and
  architecture conformance against docs/ (service-layer rule,
  tenant-scoped managers, adapter pattern, idempotency, concurrency &
  transactional correctness, locked decisions, V1 scope, data
  model/migrations, Django/DRF/Celery craft, required tests). Do not
  comment on security. Follow your
  Run Contract and return one report."

Do not wait for one to finish before starting the other.

---

## Step 2: Unified Report

Once both subagents have completed, combine their findings into a
single report. De-duplicate overlapping findings — if both agents
flagged the same `file:line`, merge them into one finding with both
perspectives noted and keep the higher severity.

Structure the combined report as:

```
Code Review Report — <spec-name>
Branch: <branch>   Files reviewed: <n>   Not reviewed: <list or "none">

## Security Findings
<reviewflow-security-reviewer output>

## Quality Findings
<reviewflow-quality-reviewer output>

## Combined Action Plan
Ordered checklist, highest priority first:
1. [ ] 🔴 Security — Critical
2. [ ] 🟠 Security — High
3. [ ] 🔴 Quality — Must fix
4. [ ] 🟡 Security — Medium
5. [ ] 🟡 Quality — Should fix
6. [ ] 🔵 Security — Low / Unverified (say what to check)
7. [ ] 🔵 Quality — Polish
Each item: `file:line` — issue — fix (one line), with the doc/section cited.

## Overall Verdict
<one of the three below>
```

Verdict rules:
- **CHANGES REQUESTED** — any Security Critical/High, or any
  Quality Must fix. Must fix before `/ship-feature`.
- **APPROVED WITH SUGGESTIONS** — only Medium/Low/Unverified
  security items and/or Should fix/Polish quality items. Can ship;
  address suggestions in a later step.
- **APPROVED** — no findings in either report. Ready for
  `/ship-feature`.

---

## Step 3: Ask for Approval

After presenting the unified report, ask:

"Do you want me to implement the action plan now?"

Wait for explicit user confirmation before making any changes. Do
not touch any files until the user approves. If the user approves,
implement only the approved items, then tell them to re-run
`/code-review-feature <spec-name>` — do not re-run the review
automatically.

---

## Rules
- Do NOT edit any files before user approval
- Do NOT start one reviewer before the other — both run in parallel,
  each exactly once
- Do NOT retry a failed subagent; do NOT loop review → fix → review
  on your own
- Do NOT skip the pre-flight checks
- Do NOT review on `main` — a feature branch is required
- Do NOT proceed if `.claude/specs/<spec-name>.md` does not exist —
  report it and stop
- If either subagent fails or returns no output, report which one
  failed and do not present a partial review as complete
- Security findings are never downgraded when merging with quality
  findings
