# TASK_PROGRESS.md — working ledger

Evidence labels: **simulated** (synthetic sources on a dev machine/CI), **staged** (representative
VM), **field-verified** (real instrument). Everything below is **simulated**; nothing is staged or
field-verified, and no simulated pass is field acceptance.

## Current state (resume here)

- **Done:** first tranche (M0 + M1) and the second tranche (ROADMAP "Second implementation
  tranche"): MIT license, confirmed Ubuntu 24.04 / Python 3.12 / PostgreSQL 16 baseline,
  manual-only CI with `scripts/check.sh`, UTM + vacuum dilatometer + opt-in DSC simulators,
  runs and XY analyses, redesigned dashboards, rehearsal playback, standards docs.
- **Active milestone:** M3 preparation (M2 buildable items done). Next actions are listed at the end of this section.
- **Branches:** `main` and `claude/cool-hawking-huoxx4` carry identical history.
- **Environment used:** Ubuntu 24.04 container, CPython 3.12.3 (SQLite 3.45.1), PostgreSQL
  16.15, Node 22, Chromium from Playwright 1.63.0. All evidence is **simulated**.

### Checkpoint 3 — M2 tranche 3 (in progress)

- **Objective:** M2 gate items in order: (1) minute rollups with revisioned dirty-bucket
  invalidation in the ingest transaction, a separate rollup worker, and a seeded 30-day furnace
  fixture (one-sample peak + known dropout) proving the 30-day plot keeps both, with measured
  query latency; (2) user runs and annotations with audit (RUN-01); (3) role matrix;
  (4) durable exports with manifest; (5) spool lock, DSC onset, tensile 0.2 % yield.
- **Validation plan:** rollup output equals on-the-fly raw aggregation for the same range;
  late sample invalidates and is recomputed; worker never clears a newer invalidation;
  30-day known answers (count, peak value and time, dropout interval) asserted from the API;
  p50/p95 latency recorded; `scripts/check.sh --e2e` green after each step.

**Step 1 result — rollups and the 30-day gate (simulated, done):**

- Migration `0003` (expand-only): `sample_rollup_1m` cache + `rollup_dirty` revisioned queue;
  ingest invalidates touched minutes in the sample transaction; `labmon-server rollup-worker`.
- 30-day fixture `furnace-30d` (7 749 000 samples over 3 channels) loads in ~230 s, rollups in
  ~96 s. The 30-day view keeps the one-sample peak (1270.33 °C at 2026-09-18T12:34:56Z) and
  reports exactly one gap for the 2.5 h dropout.
- Latency (warm, dev container, sequential): 3-series instrument view p50 0.40 s / p95 0.46 s;
  **8-series p50 1.01 s / p95 1.12 s** (target ≤ 3 s). Cold single series 0.14 s. First version
  read 43 k minute rows into Python (p95 2.28 s for 3 series); aggregation and gap detection
  now run in SQL with raw fallback only for dirty and edge minutes.
- Defect found by the equivalence test and fixed: min/max ties across a recomputed edge minute
  and an aggregated partial went to the later timestamp.

**Step 2 result — RUN-01 runs and annotations (simulated, done):** migration `0004`; user run
records (backdated, never future), corrections with required reason and before/after audit;
instrument-reported runs keep name and window; annotations (points or windows) with revisions
and soft retraction; write access for scientist/custodian/administrator only (viewer/auditor
read-only, other labs 404). UI: chart markers, annotations panel, new/correct/end run.
10 integration tests + browser flow.

**Step 3 result — EXP-01 durable exports (simulated, done):** migration `0005`; leased jobs in
the separate `labmon-server worker`; ZIP with `data.csv` + `manifest.json` (snapshot cutoff,
profile revisions, counts, sha256); atomic publication; access re-checked at download; 7-day
expiry. 5 integration tests (checksums agree, killed worker recovered, no partial files,
access/expiry) + browser download.

Gate after step 3: `scripts/check.sh --e2e` → 150 passed + 2 e2e passed.

**Step 4 — spool ownership lock (done):** OS-level exclusive lock per spool (POSIX flock /
Windows msvcrt); `backfill` refuses while the collector runs; `status`/`quarantine` open the
spool read-only.

**Step 5 — analysis depth (done):** UTM two-stage strain control (elastic 2.5e-4 /s);
tensile modulus fit (≥15 points, within 5 % of 200 GPa) and Rp0.2 (within 3 MPa of 353.6);
DSC extrapolated onsets (156.6 / 154.0 °C); furnace soak tolerance per run (within/out/
unobserved, excursions with documented merge rules); history summary mean and coverage.

**UI review (simulated, dev stack, 3 h playback + 30-day fixture):** furnace run 81.3 % within
±5 °C with a −28 °C lag excursion and a +7.77 °C overshoot; 30-day fixture view shows the
1270.3 °C spike, the 2.5 h dropout gap and 99.7 % coverage. Fixed: setpoint and tolerance band
swamped bucketed views (tolerance now only at sample resolution; process value drawn on top).

Gate: `scripts/check.sh --e2e` → 154 passed + 2 e2e; `--slow` 30-day evidence passed.

**Step 6 — OPS-01 diagnostics (done):** fault rules with severities and one next step each
(17 unit tests), collector disk headroom in the heartbeat, `/api/v1/system/health` (rollup
queue, export jobs, database size, export disk). Backup freshness is reported as *not yet
monitored* (M3).

**Step 7 — HIST-01 saved views (done):** migration `0006`; personal views private, lab
templates for custodian/administrator (audited); validated configs; UI views menu.
Prettier 3.8.1 pinned and enforced by `scripts/check.sh`.

**Step 8 — administration (done):** audited `labmon-server admin` for labs, users, grants,
collectors, token rotation/revocation and simulated enrolment; `docs/operations/
administration.md`. Integration test provisions a lab from scratch.

Gate after step 8: `scripts/check.sh` → 181 passed; `--e2e` 2 passed (last run at step 7).

**M2 status:** all buildable M2 items are implemented with simulated evidence. Open: the
scientist usability review (needs people), and hour-level rollups only if larger fleets need
them (8-series 30-day p95 is 1.12 s today).

**Step 9 — M3 preparation (simulated rehearsal, done):** `doctor`, `backup`, `restore-check`,
systemd units (API, worker, backup timer, Linux collector), nginx TLS site, environment template,
`deploy/install.sh`, `scripts/build-release.sh` (wheelhouse, SBOM, checksums; 15 MB tarball in
~1.7 min). Rehearsal in a temporary root with proxies pointed at a dead port: offline install,
migrate to 0006, doctor OK, API ready and UI served from the installed release, backup +
verified restore (counts match), install of a second release, rollback by symlink, tampered
bundle refused. Defect found and fixed: renaming a built venv broke its entry points. Docs:
`docs/operations/deployment.md`, ADR 0006. Not yet done: staging VM, real systemd/reboot,
TLS from a client workstation, Windows collector service, PITR, 72 h soak.

### Checkpoint 2 — owner requests of 2026-11-06 (simulated)

| Request | Result | Evidence |
| --- | --- | --- |
| Baseline confirmed | Recorded in AGENTS, CONTRIBUTING, SPECIFICATIONS v0.3, ROADMAP (M0 done) | docs |
| CI manual only | `ci.yml` has only `workflow_dispatch`; ADR 0005; local gate `scripts/check.sh` | `.github/workflows/ci.yml` |
| MIT license | `LICENSE`, `pyproject.toml` (`license = "MIT"`), `web/package.json` | files |
| UTM, dilatometer, DSC simulators | One source contract, per-instrument multi-rate scheduling (UTM 5 Hz while testing), run events, known answers in each module docstring (ADR 0004) | `tests/unit/test_simulator.py`, `test_acquisition.py` |
| Runs and XY analysis | Migration 0002 (expand-only), run projection in the ingest transaction, `/runs`, `/runs/{id}`, `/events` | `tests/integration/test_runs.py` |
| Elegant front end and dashboards | Fleet dashboard (KPIs, cards, sparklines, table), workspace tabs (Overview / Runs & XY analysis / Diagnostics), light/dark/auto | `tests/e2e/test_browser.py`, screenshots |
| Best-practice foundational docs | CONTRIBUTING.md (standards, definition of done), AGENTS.md, SPECIFICATIONS v0.3, ROADMAP, ADR 0004/0005, README, operations doc | docs |

Known answers verified end to end through the real collector path (acquisition → SQLite outbox
→ uploader → API → PostgreSQL), see `tests/integration/test_runs.py`:

- Tensile run: UTS 480 ± 0.6 MPa at strain 0.18 ± 0.01, max force 13.572 kN, fracture after the
  UTS at strain 0.28, > 700 frame-paired points at 5 Hz in acquisition order, UTS withheld for
  incomplete runs.
- DSC run (exo up): endotherm below −25 mW at 157.5–159.5 °C with area −286 mJ ± 3 %
  (−28.6 J/g); exotherm above +29 mW at 152–154.5 °C with area +286 mJ ± 5 %.
- Collector restart mid-run is idempotent; a missed run end is closed when the next run starts
  (`end_not_reported`) and never shown as active past its planned end.

Rehearsal run (manual, simulated): `backfill --hours 3` spooled 60 632 frames
(181 896 samples) in 1 min 44 s; the live collector then uploaded the 181 982-record backlog in
70 s with nothing quarantined. Screenshots reviewed: dashboard light/dark, furnace 24 h,
dilatometer log pressure, UTM stress–strain run, DSC heat flow vs temperature, diagnostics,
phone/tablet/wide.

Defects found and fixed during this checkpoint:

1. Outbox append counted every pending row (`COUNT(*)`), so appends slowed as a backlog grew;
   now an O(1) counter in the outbox meta table, recounted on open (regression test).
2. A run left open by a stopped collector stayed "Running" forever (fixed as above).
3. "Delivery delayed" was claimed whenever a heartbeat saw records in flight; now it requires
   the oldest pending record to be older than the 5 s freshness window at heartbeat time.

**Step 10 — local help (done):** `#/help` page shipped in the web bundle (no external links):
status words and their meaning taken from the same labels the UI shows, charts and gaps, runs
and analyses, exports, roles. Browser test opens it from the top bar.

### Validation (latest, step 10)

| Command | Result |
| --- | --- |
| `scripts/check.sh --e2e` | ruff + mypy --strict clean, **182 passed** (unit + integration, 70 s), Prettier/typecheck/build clean, e2e **2 passed** (43 s) |

### Current limits

- Nothing is staged or field-verified; the release rehearsal ran in a temporary root without
  real systemd, reboot or client-workstation TLS.
- DSC peak areas use a straight baseline and are labelled estimates.
- The CI workflow has never been dispatched on GitHub; only local gate runs are recorded.
- No scientist usability review yet.

### Next actions (M3)

1. Staging VM rehearsal of `deploy/install.sh` with real systemd, reboot and TLS from a
   client workstation (needs an authorized environment).
2. Windows collector service approach and runbook; PITR runbook for PostgreSQL.
3. Soak tooling (72 h simulated fleet) with spool, latency and memory evidence.
4. Scientist usability review of the dashboards, fix findings and repeat.

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

## Tranche 1 record: validation results

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

## Tranche 1 record: limits at the end of M1 (superseded by the current state above)

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
| Repository license | Owner | **Resolved:** MIT (2026-11-06) |
| Ubuntu/Python/PostgreSQL baseline | Owner / site | **Resolved:** confirmed by the owner (24.04 / 3.12 / 16) |
| SQLite WAL-reset fix threshold (3.51.3) | Dev | Re-verify against upstream notes and each platform build before M3 (ADR 0003) |
| Spool epoch derived from wall clock at spool creation | Dev | A badly wrong clock at creation could reuse an epoch; server refuses with `identity_conflict` rather than overwriting (ADR 0002) |
| Windows 11 collector | Dev | Not exercised yet (POSIX token-permission check only); M3 packaging |
| Starlette TestClient deprecation of `httpx` | Dev | Warning filtered in tests; revisit when upgrading Starlette |

## Tranche 1 record: planned next actions (superseded by the current state above)

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

### 2026-11-06 — commits 6–10 (second tranche)
- `323682e` MIT license and manual-only CI. `dca348f` simulator fleet and multi-rate scheduler
  (81 unit tests). `9f9bca9` runs and XY analysis (125 unit + integration tests).
  `ddf0b11` dashboards, playback, outbox counter, run closing, delivery-delayed rule, docs
  (128 + 2 e2e). Final commit: phone layout polish and this ledger.
