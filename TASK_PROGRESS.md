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
- [x] T3 — batch ingestion, receipts, scoped history endpoint, status endpoint + validations
      (38 integration tests on real PostgreSQL 16; simulated)
- [ ] T4 — fleet page, live/history chart, simulator label, ranges, gaps, CSV, browser verification
- [ ] T5 — run instructions, validation results, limits, next milestone (this file + README)

## Log

### 2026-11-06 — commit 1 (foundation, collector, server code)
- Unit: `pytest tests/unit` → 63 passed. Integration: `pytest tests/integration/test_migrations.py`
  → 3 passed on PostgreSQL 16.15 (disposable cluster). ruff + mypy --strict clean. *Simulated.*
- Python 3.12.3 (`/usr/bin/python3.12`), linked SQLite 3.45.1 → outbox uses rollback journal
  (`journal_mode=delete`, `synchronous=FULL`), see ADR 0003.
- **Next:** ingest/status/series integration tests, ADRs, then UI (T4).

### 2026-11-06 — commit 2 (T3 ingest/status/history validated)
- `pytest tests/unit tests/integration` → **101 passed** (PostgreSQL 16.15 disposable cluster).
  Covers: duplicate submission, identical retransmission replay, batch-id conflict (409), identity
  conflict surfaced in `ingest_conflict`, explicit whole-batch rejection + edge re-batch, lost ACK
  after commit (exactly once), 6 concurrent retransmissions → one receipt, overlapping concurrent
  batches → no double insert, auth/revocation/foreign collector, gzip expansion cap, future clock
  flag, equal readings on time stay Receiving, replay cannot refresh freshness, heartbeat does not
  refresh freshness, offline/delayed/source-unavailable reasons, raw/bucket series with gaps and a
  one-sample peak, CSV provenance + formula escaping + audit, cross-lab 404s, CSRF/throttle.
- Fixed during testing: FastAPI closure dependencies need eager annotations in `app.py`; Core
  entity selects return flat rows (status/CSV).
- Added ADRs 0001–0003, `docs/threat-model.md`, `docs/dependencies.md`.
- **Next:** T4 UI (React/Vite/ECharts), process-level recovery test, Playwright happy path.

### 2026-11-06 — commit 3 (T4 UI first cut)
- `web/`: React 19.2.9 + TypeScript 5.9.3 + Vite 7.3.7 + ECharts 6.1.0, pinned in
  `package-lock.json`. Fleet page (search, status filters/counts, simulator badge, freshness,
  reason text), instrument workspace (sticky status header, channel tiles with last-good, live
  follow vs labelled pause, presets 15 min–30 d + custom, keyboard zoom/pan, auto/fixed Y with
  minimum span, gap overlays, bad-quality markers, event markers, bucket min–max band + mean,
  summary text, readings table, CSV link, PNG export), IST/UTC toggle, connection banner.
- Manual run (simulated): `labmon-devdb start`, `migrate`, `bootstrap-dev`, `serve`, collector →
  status Receiving, 1 Hz ACKs, Chromium at 1366×768 made **no requests outside 127.0.0.1:8000**.
- **Next:** process-level recovery e2e (kill API / lost ACK), Playwright browser e2e, screenshots
  at supported sizes, docs/operations/local-dev.md.
