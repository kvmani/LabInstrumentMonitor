# TASK_PROGRESS.md — working ledger

Evidence labels: **simulated** (synthetic sources on a dev machine/CI), **staged** (representative
VM), **field-verified** (real instrument). Everything below is **simulated**; nothing is staged or
field-verified, and no simulated pass is field acceptance.

## Current state (resume here)

- **Active work:** ROADMAP "First implementation tranche" items 1–5 — **done** (simulated furnace
  only). M0 gate evidence attached except owner inputs; M1 gate evidence attached, awaiting owner
  review. Next milestone: **M2** (see "Next actions").
- **Branches:** `main` and `claude/cool-hawking-huoxx4` carry identical history.
- **Dev environment used:** Ubuntu 24.04 container, CPython 3.12.3 (SQLite 3.45.1),
  PostgreSQL 16.15, Node 22, Chromium from Playwright 1.63.0.

## Checkpoint 1 — First implementation tranche

- **Objective:** tranche items 1–5 (M0 foundation + M1 synthetic vertical slice) with the
  simulated furnace only; ambient source kept as a smoke fixture.
- **Milestones touched:** M0, M1.
- **Validation plan (all executed, results below):** unit suite on virtual clocks; integration
  suite on real PostgreSQL 16; real-process recovery test; Playwright browser path; clean-checkout
  rebuild.

| Item | Status | Evidence |
| --- | --- | --- |
| T1 scaffold, deps, config, disposable PostgreSQL, first migration | Done | `uv.lock`, `web/package-lock.json`, `labmon-devdb`, `tests/integration/test_migrations.py` (upgrade → downgrade → upgrade, models = schema) |
| T2 furnace profile, deterministic source, 1 Hz virtual clock, ambient fixture, envelope/batch fixtures, collector, SQLite outbox, inspection CLI | Done | `src/labmon/{profiles,sim,collector}`, `tests/fixtures/*.json` (golden digests), `labmon-collector status/quarantine`, `tests/unit/*` |
| T3 batch ingestion, receipt, scoped history + status endpoints, duplicate / commit-before-ACK / rejection / equal readings | Done | `src/labmon/server/{ingest,status,series}.py`, `tests/integration/test_ingest.py`, `test_status_series.py` |
| T4 fleet page, live/history chart, simulator label, ranges, gaps, CSV, browser verification | Done | `web/`, `tests/e2e/test_browser.py` |
| T5 run instructions, results, limits, next milestone | Done | this file, `README.md`, `docs/operations/local-dev.md` |

## How to run

See `README.md` (quick start) and `docs/operations/local-dev.md` (full steps, failure
demonstrations, reset, offline dependency collection). Short form:

```bash
uv sync --frozen --extra dev && (cd web && npm ci && npm run build)
.venv/bin/labmon-devdb start
export LABMON_DATABASE_URL=postgresql+psycopg://labmon@127.0.0.1:55432/labmon LABMON_ENV=development
.venv/bin/labmon-server migrate && .venv/bin/labmon-server bootstrap-dev --password 'choose-one'
.venv/bin/labmon-server serve --port 8000 &
.venv/bin/labmon-collector run --config .devdata/collector/collector.toml
```

## Validation results (exact, latest run)

| Command | Result |
| --- | --- |
| `.venv/bin/ruff check src tests` / `ruff format --check` | clean |
| `.venv/bin/mypy` (strict, 32 files) | no issues |
| `cd web && npm run typecheck` / `npm run build` | clean; bundle 841 kB (281 kB gzip), no external URLs fetched |
| `.venv/bin/pytest` (all) | **104 passed in 64.7 s** (64 unit, 38 integration, 2 e2e) |
| Clean clone → `uv sync --frozen` → `npm ci` → build → `pytest tests/unit tests/integration` | 101 passed |
| Manual stack run + Chromium screenshots (1366×768, 1920×1080, 1024×768, stale, API down) | reviewed; no requests outside the app origin |

Gate-relevant facts demonstrated (all simulated):

- **Browser independence:** closing the browser does not stop collection (sample count rises).
- **API outage:** API SIGKILLed 8 s; collector kept capturing; after restart every acknowledged
  identity is in PostgreSQL exactly once with the same digest; no PV gap > 2.5 s across the outage.
- **Lost ACK:** commit succeeds, response lost (500) → retry returns the recorded receipt;
  rows stay exactly once (`test_commit_before_ack_lost_ack_recovers_exactly_once`).
- **Concurrent retransmission:** 6 threads, same batch → one receipt, 60 rows.
- **Collector crash:** SIGKILL + restart resumes the same epoch and sequence; the stopped interval
  is an explicit gap, not invented data.
- **Freshness:** equal values on time stay Receiving; heartbeats and replayed history cannot make
  data fresh; suspect clocks are excluded; process state shows Unknown when its channel is stale.
- **Scope:** every ID-bearing endpoint (status, series, CSV) returns 404 across labs; 401 without
  a session; CSRF header and login throttle enforced.

## Contracts introduced (keep fixtures, OpenAPI and docs in step when changing)

- Protocol v1 envelope/batch/ACK: `src/labmon/contracts/envelope.py`, ADR 0002, fixtures in
  `tests/fixtures/`. OpenAPI served at `/api/openapi.json`.
- Endpoints: `POST /api/v1/ingest/batches`, `POST /api/v1/collectors/heartbeat`,
  `POST|GET|DELETE /api/v1/session`, `GET /api/v1/instruments`,
  `GET /api/v1/instruments/{id}/status`, `GET /api/v1/channels/{id}/series`,
  `GET /api/v1/instruments/{id}/export.csv`, `/health/live`, `/health/ready`.
- Schema: Alembic `0001` (rollback floor; its downgrade is for disposable DBs only).
- Outbox schema v1 (SQLite), journal choice per ADR 0003.

## Current limits (known, intentional for M1)

- Only the simulated furnace (+ ambient smoke fixture). UTM, vacuum dilatometer and opt-in DSC
  are M2 and must reuse these contracts.
- Bucketed history is computed from raw rows on demand (no rollup tables yet); fine for days of
  1 Hz data, not yet benchmarked for the 30-day / 8-series gate (M2).
- Gaps are derived at query time from sample spacing (threshold max(3 × cadence, 5 s)); there is
  no persisted missing-interval table yet. Collector diagnostic events are shown as markers.
- CSV is a bounded synchronous stream (≤ 500 000 rows) without manifest; durable export jobs,
  manifest and checksums are M2 (EXP-01).
- Accounts: users, Argon2id, sessions and lab grants exist; there is no user administration UI or
  role-specific write actions (v1 is read-only). Collector credentials are issued by the dev
  bootstrap only; provisioning/rotation is M3.
- Collector configuration is a local TOML written by `bootstrap-dev`;
  `GET /api/v1/collectors/config` and applied-revision acknowledgement are M3 (wizard).
- No tolerance band or excursion metrics on the furnace chart yet (ANA-01, M2).
- No historical playback / 30-day fixture loader yet (M2); the virtual clock exists in
  `labmon.sim.clock` and drives the unit tests.
- HTTP only in development; the collector refuses plain HTTP unless `allow_insecure_http = true`.
- `.github/workflows/ci.yml` is added but **has not been observed running** on GitHub.

## Risks and open inputs

| Item | Owner | Notes |
| --- | --- | --- |
| Repository license | Owner | Not chosen; `pyproject.toml` deliberately has no license field |
| Ubuntu/Python/PostgreSQL baseline | Owner / site | Taken from SPECIFICATIONS §2 (24.04 / 3.12 / 16); not re-verified against the reference repos in this session |
| SQLite WAL-reset fix threshold (3.51.3) | Dev | Re-verify against upstream notes and each platform build before M3 (ADR 0003) |
| Spool epoch derived from wall clock at spool creation | Dev | A badly wrong clock at creation could reuse an epoch; server refuses with `identity_conflict` rather than overwriting (ADR 0002) |
| Windows 11 collector | Dev | Not exercised yet (POSIX token-permission check only); M3 packaging |
| Starlette TestClient deprecation of `httpx` | Dev | Warning filtered in tests; revisit when upgrading Starlette |

## Next actions (M2 · reliability and chart workbench)

1. Long-history path: 30-day furnace fixture with a one-sample peak and a known dropout loaded via
   a documented test path (virtual clock, original event times); minute/hour rollups with dirty
   bucket invalidation in the ingest transaction; persisted missing intervals; verify the 30-day
   plot keeps the peak and dropout and measure p95 against §10.
2. Add the UTM, vacuum dilatometer and opt-in DSC profiles + sources on the same contracts, with
   known-answer runs (UTS/fracture order, pump-down + signed ΔL + log-pressure, DSC peak + sign)
   and run-specific XY views using frame-paired raw values.
3. Runs and annotations (RUN-01), saved views, furnace tolerance band and excursion metrics.
4. Durable export worker with manifest/checksums, chart PNG with legend/simulation label (partly
   present via ECharts save-as-image), diagnostics page (OPS-01).
5. Role matrix (Viewer/Scientist/Custodian/Administrator/Auditor) with audit of admin actions.
6. Confirm CI on GitHub, then scientist usability review.

## Log

### 2026-11-06 — commit 1 (foundation, collector, server code)
- Unit: `pytest tests/unit` → 63 passed. Integration: `pytest tests/integration/test_migrations.py`
  → 3 passed on PostgreSQL 16.15 (disposable cluster). ruff + mypy --strict clean. *Simulated.*
- Python 3.12.3 (`/usr/bin/python3.12`), linked SQLite 3.45.1 → outbox uses rollback journal
  (`journal_mode=delete`, `synchronous=FULL`), see ADR 0003.

### 2026-11-06 — commit 2 (T3 ingest/status/history validated)
- `pytest tests/unit tests/integration` → **101 passed** (PostgreSQL 16.15 disposable cluster).
  Covers duplicate submission, identical retransmission replay, batch-id conflict (409), identity
  conflict surfaced in `ingest_conflict`, explicit whole-batch rejection + edge re-batch, lost ACK
  after commit (exactly once), concurrent retransmissions, overlapping concurrent batches,
  auth/revocation/foreign collector, gzip expansion cap, future clock flag, freshness rules,
  raw/bucket series with gaps and a one-sample peak, CSV provenance + formula escaping + audit,
  cross-lab 404s, CSRF/throttle.
- Fixed during testing: FastAPI closure dependencies need eager annotations in `app.py`; Core
  entity selects return flat rows (status/CSV).
- Added ADRs 0001–0003, `docs/threat-model.md`, `docs/dependencies.md`.

### 2026-11-06 — commit 3 (T4 UI first cut)
- `web/`: React 19.2.9 + TypeScript 5.9.3 + Vite 7.3.7 + ECharts 6.1.0. Fleet page and
  instrument workspace (see Validation results). Manual run: Receiving at 1 Hz, no external
  requests.

### 2026-11-06 — commit 4 (M1 gate evidence: process recovery + browser)
- `pytest` (all) → 103 passed in 69 s. Recovery and browser evidence as summarised above.

### 2026-11-06 — commit 5 (hardening, docs, CI)
- Collector worker loops log and back off on unexpected errors; the process exits non-zero if
  acquisition dies so a service manager restarts it (new unit test).
- `docs/operations/local-dev.md` (run, reset, failure demos, offline wheelhouse path verified with
  `uv export` + `uv build`), `.github/workflows/ci.yml` (unverified on GitHub).
- Clean-clone rebuild: frozen install, `npm ci`, build, 101 unit+integration tests passed.
- ROADMAP status column: M0 "In progress", M1 "Simulated evidence attached; awaiting owner review".
