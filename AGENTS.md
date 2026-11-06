# AGENTS.md

## Mission

Build a maintainable, air-gapped lab instrument monitor. The application captures, preserves and explains scientific readings. Version 1 observes instruments and records evidence. Deliver a simulator-only first release with the furnace, universal tensile testing machine and fictional vacuum dilatometer as the default fleet, plus an opt-in DSC. The browser, the ML Server portal and the acquisition process have independent lifetimes.

## Confirmed decisions

- **License:** MIT. **Platform baseline:** Ubuntu 24.04, Python 3.12, PostgreSQL 16 (confirmed by the owner).
- **CI:** GitHub Actions workflows run only on manual `workflow_dispatch`. Never add push, pull-request or scheduled triggers. Run `scripts/check.sh` locally instead.
- **Standards:** [CONTRIBUTING.md](CONTRIBUTING.md) holds the engineering standards and the definition of done. Treat it as binding.

## Start here

1. Read [SPECIFICATIONS.md](SPECIFICATIONS.md) for the requirements and boundaries, [ROADMAP.md](ROADMAP.md) for the active milestone, `TASK_PROGRESS.md` for the current state, and [CONTRIBUTING.md](CONTRIBUTING.md) for how work is done.
2. Inspect relevant source, tests, ADRs (`docs/adr/`) and operations documentation (`docs/operations/`). Use `kvmani/LabAxes`, `kvmani/ml_server`, `kvmani/ml_server_deploy` and `kvmani/project_management_software` as references for patterns and integration. This repository owns its application and data. Do not copy code from incompatible licenses.
3. Before a multi-step task, add a brief checkpoint to `TASK_PROGRESS.md` with objective, milestone and validation plan. Update it with changed contracts, exact test outcomes, unresolved risks and next action. Mark evidence separately as simulated, staged or field-verified. Commit and push after each substantial step so work can resume without loss.

## Engineering rules

- Use the stack and release boundaries in the specification. Keep acquisition outside web workers. Prefer one readable implementation per contract. Add dependencies only with target-platform, security, licensing and offline packaging evidence, recorded in `docs/dependencies.md`.
- Preserve measurement identity, original quality, acquisition profile and time provenance. Commit before acknowledging. Retries must be idempotent. Display gaps and suspect data explicitly. A heartbeat, cached value or replay must never masquerade as fresh telemetry.
- Use the simulator for development, CI and the first pilot. Every simulated source is deterministic for a seed, documents its known answers and has known-answer tests. Defer hardware adapters until the owner selects an instrument and provides its approved interface and procedure. Keep v1 read-only. Keep confidential site materials out of test fixtures and public repositories.
- Protect credentials, enforce scoped access on the server and keep mutable state outside releases. Use approved endpoints, service identities and bounded I/O. Keep migrations expand-only and compatible with the documented rollback floor, and retain scientific records. Never restore over newer data as part of ordinary code rollback.
- Fit each change to the active milestone. Update affected API schemas, fixture examples, user help and operator procedures together. Capture changes to acquisition, storage, scientific semantics or deployment in an ADR. Document open assumptions and failure modes.

## Verification and handoff

- Run `scripts/check.sh` (formatting, lint, strict types, unit and real-PostgreSQL integration tests, UI typecheck and build) and `scripts/check.sh --e2e` for acquisition, ingest, status or UI work. Use real PostgreSQL for transaction and migration tests. Use deterministic source fixtures and test at least one failure/recovery path per modified critical workflow. Verify physical quantities, pairing and run order in the tensile and DSC XY views.
- For UI work, review the running app at supported viewport sizes (1366×768, wide, tablet, phone) in light and dark themes with live, stale, simulated and empty data. Verify keyboard operation, scientific units, time ranges and display of gaps. `labmon-collector backfill` provides rehearsal history; keep evidence in a reproducible test or review record.
- For a release, verify the pinned deployed baseline, offline installation, migration and rollback, data survival, and service health from a client workstation. Record scope, exact versions, commands, test results and remaining gaps. Never label a simulated pass as field acceptance.
- Follow the owner's repository branch and release policy. Keep commits reviewable. Portal and suite integration are separate changes against their own baselines. Deploy to a real environment only as authorized.
