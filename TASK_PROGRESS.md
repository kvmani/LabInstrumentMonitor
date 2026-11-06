# TASK_PROGRESS.md — working ledger

Evidence labels: **simulated** (synthetic sources on a dev machine/CI), **staged** (representative VM),
**field-verified** (real instrument). Nothing in this ledger is staged or field-verified yet.

## Checkpoint 1 — First implementation tranche (ROADMAP.md §"First implementation tranche")

- **Objective:** deliver tranche items 1–5 (M0 foundation + M1 synthetic vertical slice) with the
  simulated furnace only. Ambient source stays as a smoke fixture.
- **Milestones touched:** M0 (foundation), M1 (synthetic vertical slice).
- **Validation plan:**
  1. `pytest` unit suite: contracts/digest, simulator determinism (virtual clock), outbox
     (sequence allocation, batch membership, quarantine, capacity), scheduler (no burst after sleep).
  2. `pytest` integration suite against a real, disposable PostgreSQL 16 cluster: first migration
     (upgrade/downgrade), ingest (duplicate submission, concurrent retransmission, identity conflict,
     whole-batch rejection, commit-before-ACK with lost ACK), status (equal readings on time stay
     Receiving, replay cannot refresh freshness, heartbeat does not refresh last-good), series (gaps).
  3. Process-level recovery test: real collector + real API processes; kill API, recover, reconcile
     expected IDs exactly once.
  4. Browser (Playwright, Chromium) happy path: login → fleet → furnace → history range → gaps → CSV;
     verify no external requests.

## Status

- [x] T1 — scaffold (`pyproject.toml` + `uv.lock`), env configuration, disposable PostgreSQL
      (`labmon-devdb`), first migration `0001` tested upgrade→downgrade→upgrade (simulated/dev)
- [x] T2 — furnace profile, deterministic source + virtual clock, ambient fixture, golden
      envelope/batch fixtures, collector, SQLite outbox, `labmon-collector status` (unit-tested)
- [ ] T3 — batch ingestion, receipts, scoped history endpoint, status endpoint + validations
      (code written; integration tests in progress)
- [ ] T4 — fleet page, live/history chart, simulator label, ranges, gaps, CSV, browser verification
- [ ] T5 — run instructions, validation results, limits, next milestone (this file + README)

## Log

### 2026-11-06 — commit 1 (foundation, collector, server code)
- Unit: `pytest tests/unit` → 63 passed. Integration: `pytest tests/integration/test_migrations.py`
  → 3 passed on PostgreSQL 16.15 (disposable cluster). ruff + mypy --strict clean. *Simulated.*
- Python 3.12.3 (`/usr/bin/python3.12`), linked SQLite 3.45.1 → outbox uses rollback journal
  (`journal_mode=delete`, `synchronous=FULL`), see ADR 0003.
- **Next:** ingest/status/series integration tests, ADRs, then UI (T4).
