---
name: avenger-sdd
description: Use when the user says “Avenger team”, “OpusPlan mode”, “delegate mode”, “Open Specs”, or asks for SDD/TDD process improvement. Main agent writes specs, batches orthogonal exploration, delegates bounded TDD execution/review, keeps live-state rollout sequential, and reports DIBB.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [delegation, sdd, tdd, planning, process, avenger, dibb]
    related_skills: [writing-plans, subagent-driven-development, test-driven-development, plan]
---

# Avenger SDD

## Overview

Avenger SDD is an OpusPlan-style operating mode for complex agentic engineering work. The main agent stays as planner, spec owner, gatekeeper, and live-state operator. Subagents act as disposable specialists for bounded exploration, TDD implementation, and review.

Use this when the work is bigger than one straightforward patch, when live service rollout is involved, or when the operator asks for process improvement around delegation. The goal is to avoid one long, fragile loop that mixes planning, exploration, mutation, rollout, and reporting.

Core principle:

```
Main agent owns specs and gates.
Subagents own bounded probes and TDD tasks.
Batch only orthogonal work.
Keep live-state changes sequential.
Report DIBB.
```

## When to Use

Use this skill when the user says or implies:

- “Avenger team”
- “OpusPlan mode”
- “delegate mode”
- “Open Specs” / “open specs”
- “SDD” / “subagent-driven development”
- “TDD execution with reviewers”
- “batch the exploration tasks”
- “process improvement” for agent workflows
- A task requires inspect → patch → test → reload → smoke → report

Do not use for:

- Simple one-shot answers
- Single-file edits with obvious tests
- Pure research where no execution plan is needed
- Long durable jobs that must survive interruption without wrapping them in background/cron execution

## Required Companion Skills

Load these when relevant:

- `writing-plans` — for executable implementation/spec plans
- `subagent-driven-development` — for fresh implementer/reviewer delegates
- `test-driven-development` — for RED/GREEN/REFACTOR enforcement
- `plan` — when the user wants a saved plan/spec before implementation
- `systematic-debugging` — when the root cause is not obvious within ~5 minutes

## Phase 0 — Preflight

Before doing non-trivial work, establish the operating envelope.

### 1. State the task card

Write a compact task card that includes:

```markdown
Task ID: T-<slug>
Goal: <one sentence>
Current state: <known facts>
Constraints: <safety / live-service / user preference constraints>
Non-goals: <what not to touch>
Acceptance: <what proves done>
Gates: <preflight, test, rollout, smoke, report>
```

### 2. Check loop/context budget when possible

For multi-step work, prefer:

- `agent.max_turns >= 90`
- `delegation.max_iterations >= 90`
- High-tier planner model for the main agent when available

If budget is low, raise it before starting or explicitly narrow scope.

### 3. Decide execution durability

Use `delegate_task` for short, bounded reasoning/exploration.

Use durable execution instead when work must survive parent interruption:

- `terminal(background=true, notify_on_complete=true)` for finite long commands/tests
- `cronjob` for scheduled or autonomous workflows

Important: `delegate_task` is synchronous. If the parent is interrupted, child work is cancelled and discarded.

## Phase 1 — Write Open Specs

The main agent writes the spec before implementation. The spec must be complete enough that a fresh subagent can execute without reading the full chat.

Minimum spec sections:

```markdown
# <Feature / Fix> Open Specs

Goal:
Architecture:
Constraints:
Non-goals:
Acceptance Criteria:

Spec 1: <Planning / API / behavior>
- Requirements
- Tests
- Acceptance

Spec 2: <Execution / rollout / integration>
- Requirements
- Tests
- Acceptance

Risks:
Open Questions:
Execution Plan:
```

For code tasks, add exact paths and commands:

```markdown
Files likely touched:
- Modify: `path/to/file.py`
- Test: `tests/path/test_file.py`

Focused test command:
`.venv/bin/python -m pytest tests/path/test_file.py::test_name -q`

Expected RED:
`AssertionError` / missing method / specific failure
```

## Phase 2 — Batch Orthogonal Exploration

Batch only work that does not mutate shared state and does not depend on another batched result.

Good batch candidates:

- Root-cause inspection
- Test coverage audit
- Reload/runbook discovery
- Minimal repro design
- Risk/security review
- Alternative architecture comparison

Do not batch:

- Live service reloads/restarts
- Port ownership changes
- Database migrations
- Production/live sends
- Multiple implementers editing the same files
- Runtime smoke tests that depend on a sequential rollout

### Delegate prompt template

```text
Avenger role: <Ops / Regression / Architecture / Security / Reviewer>
Goal: <bounded goal>
Context: <task card + relevant paths + constraints>
Rules:
- Do not mutate live state.
- Do not print secrets.
- Return DIBB only.
- Stop after <timebox> or when evidence is sufficient.
```

Use `delegate_task(tasks=[...])` for independent probes.

## Phase 3 — Decision Gate

After exploration, the main agent synthesizes the DIBBs and chooses one path.

Decision gate output:

```markdown
BLUF: <chosen direction>
Evidence: <why>
Rejected paths: <what not to do>
Next RED test: <exact behavior to lock>
Implementation lane: <one bounded task>
Rollout gate: <how live behavior will be verified>
```

Do not let every delegate recommendation become scope. Pick the smallest reversible next step.

## Phase 4 — TDD Implementation Lane

Implementer delegates must follow TDD unless the user explicitly waives it.

### Implementer task card

```text
Implement Task N using strict TDD.

Objective:
Files:
Test command:
Expected RED:
Steps:
1. Write failing test first.
2. Run focused test and verify expected failure.
3. Write minimal code to pass.
4. Run focused test and report output.
5. Run adjacent regression tests if cheap.
6. Return DIBB.

Do not refactor unrelated code.
Do not touch live services.
```

### Review gates

After implementation, dispatch reviewers when practical:

1. Spec reviewer — checks requirement compliance and no scope creep.
2. Quality reviewer — checks maintainability, edge cases, and regression risk.

Spec review must pass before quality review.

If a reviewer requests changes, send the fix to an implementer and re-review. Do not proceed on “close enough” for critical/important issues.

## Phase 5 — Sequential Live Rollout Gate

The main agent owns live-state changes. Keep these sequential and auditable.

For service changes:

1. Identify service manager and current PID.
2. Run focused tests first.
3. Reload/restart with the least disruptive safe mechanism.
4. Verify new PID/process/port/health.
5. Run one controlled smoke test.
6. Capture verifiable handle: message ID, HTTP status, log line, PID, or test output.
7. If smoke reveals a new failure, freeze rollout and convert it into a failing test.

Never hide a new runtime failure behind a broad refactor.

## DIBB Reporting Contract

Every delegate/background worker should report in this exact structure.

### D — Done

- What was inspected, changed, or tested
- Exact commands run
- Pass/fail status

### I — Insights

- Root cause candidates
- Evidence
- Relevant files/functions

### B — Blockers

- Missing context
- Timeout/interruption
- Permission or service dependency
- Sequential dependency

### B — Bets

- Recommended next move
- Confidence
- Smallest validating test

## User-Facing Report Style

For iMessage/mobile, report only gate-level results.

Good:

```text
BLUF: rollout smoke passed.

Done: reloaded Lite, focused tests pass 4/4, smoke sent successfully.

Insight: original port bind bug is fixed; event-loop fallback is now covered.

Bet: next hardening is a launchd reload runbook + broader BlueBubbles suite cleanup.
```

Avoid narrating retries, plumbing, or irrelevant tool output unless it blocks progress.

## Common Pitfalls

1. **Delegating vague work.** “Investigate this” without paths, constraints, and output contract wastes subagent context. Give a task card.

2. **Batching sequential work.** Reloads, migrations, live sends, and port ownership changes must be sequential.

3. **Trusting delegate_task for durable work.** A parent interruption cancels child delegates. Use background/cron for durable jobs.

4. **Skipping RED.** If a runtime smoke exposes a new bug, add a regression test before patching further.

5. **Letting the main agent become an implementer too early.** Main should plan, gate, synthesize, and operate live state. Implementation can be delegated when bounded.

6. **Reporting raw process instead of DIBB.** The user wants concise operational status, not tool plumbing.

7. **Over-expanding scope after exploration.** Exploration generates options; the main agent must choose one smallest reversible next step.

## Verification Checklist

Before saying the Avenger workflow completed:

- [ ] Spec/task card exists and is understandable without full chat history
- [ ] Orthogonal work was batched; sequential work was not
- [ ] Delegates had bounded goals, constraints, and DIBB output contract
- [ ] TDD was used for behavior changes or explicitly waived
- [ ] Focused tests passed before live rollout
- [ ] Live rollout, if any, was performed sequentially by the main agent
- [ ] Smoke result has a verifiable handle
- [ ] Any new runtime bug became a regression test
- [ ] Final report includes BLUF + DIBB

## One-Shot Recipe: Complex Live-Service Fix

1. Ack with task ID.
2. Load `avenger-sdd`, `writing-plans`, `test-driven-development`, and domain skill.
3. Write task card and specs.
4. Batch exploration delegates:
   - Ops/reload probe
   - Regression coverage probe
   - Architecture probe
   - Risk probe
5. Main agent chooses one fix path.
6. Implement with TDD.
7. Run focused tests.
8. Reload live service sequentially.
9. Smoke test once.
10. Report BLUF + DIBB.

## One-Shot Recipe: Process Improvement Only

1. Write specs for the desired workflow.
2. Batch independent evaluator delegates if useful.
3. Synthesize DIBB.
4. Save reusable plan or skill.
5. Do not touch production/runtime state.
