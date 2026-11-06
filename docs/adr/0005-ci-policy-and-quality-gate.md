# ADR 0005 — Manual-only CI and a local quality gate

**Status** Accepted · 2026-11-06

## Context

The owner does not want automation running on GitHub without an explicit request. The project
still needs a reproducible, single-command quality gate.

## Decision

- `.github/workflows/ci.yml` uses `workflow_dispatch` only. No `push`, `pull_request`,
  `schedule` or other automatic triggers are added to any workflow.
- `scripts/check.sh` is the quality gate: ruff lint and format check, `mypy --strict`, unit and
  real-PostgreSQL integration tests, UI typecheck and production build; `--e2e` adds the
  real-process recovery suite and the Chromium browser suite. Contributors and agents run it
  before every push and record the exact result in `TASK_PROGRESS.md`.
- The manual workflow runs the same steps so a dispatched run and a local run are comparable.

## Consequences

- Nothing on GitHub verifies a push automatically; the ledger records which commit was checked
  and how. Release evidence (M3) must include a recorded full gate run for the release commit.
