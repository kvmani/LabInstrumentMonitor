# Lab Instrument Monitor

Read-only, air-gapped instrument monitoring and scientific history. A collector next to each
instrument acquires readings into a durable local outbox and uploads them to a central API backed
by PostgreSQL; scientists use a local web UI. The first release is **simulator-only**: a
heat-treatment furnace, a universal tensile testing machine and a fictional vacuum dilatometer by
default, plus an opt-in differential scanning calorimeter.

- Product contract: [SPECIFICATIONS.md](SPECIFICATIONS.md) · plan: [ROADMAP.md](ROADMAP.md) ·
  agent workflow: [AGENTS.md](AGENTS.md) · engineering standards: [CONTRIBUTING.md](CONTRIBUTING.md) ·
  progress ledger: [TASK_PROGRESS.md](TASK_PROGRESS.md)
- Decisions: [docs/adr](docs/adr) · local operation and rehearsal:
  [docs/operations/local-dev.md](docs/operations/local-dev.md) · threat model:
  [docs/threat-model.md](docs/threat-model.md)
- Platform: Ubuntu 24.04, Python 3.12, PostgreSQL 16 · License: [MIT](LICENSE)

## What you get

| Area | Highlights |
| --- | --- |
| Fleet dashboard | KPI filters (receiving, need attention, collector not online, active runs), instrument cards with live value, secondary readings, 15-minute sparkline and active run; dense table view |
| Instrument workspace | Status header with independent acquisition / collector / process state; **Overview** with profile-driven small-multiple charts (shared time cursor, explicit gaps, bad-quality and event markers, log/fixed axes, furnace tolerance band, bucketed extrema bands), CSV and PNG export; **Runs & XY analysis** (engineering stress–strain with UTS, DSC heat flow vs sample temperature with exo-up sign convention, peaks and areas); **Diagnostics** (collector, clock offset, backlog, events) |
| Collector | Deterministic simulators on one source contract, per-instrument multi-rate scheduling (UTM 5 Hz while testing), SQLite outbox with idempotent retries, heartbeat, rehearsal playback |
| Central API | Commit-before-ACK ingest with receipts and an identity ledger, lab-scoped sessions (Argon2id), status, history, runs, CSV |

## Layout

| Path | Contents |
| --- | --- |
| `src/labmon/contracts` | Protocol v1 envelopes, batches, ACKs, canonical digests |
| `src/labmon/profiles`, `src/labmon/sim` | Typed profiles with display panels; one module per simulated instrument; virtual clock |
| `src/labmon/collector` | Acquisition scheduler, SQLite outbox, uploader, heartbeat, `labmon-collector` CLI |
| `src/labmon/server` | FastAPI app, SQLAlchemy models, Alembic migrations, ingest, status, history, runs, analysis, CSV |
| `web/` | React + TypeScript + Vite + ECharts UI (bundled locally, no CDN) |
| `tests/` | `unit`, `integration` (real PostgreSQL), `e2e` (real processes + Chromium) |
| `scripts/check.sh` | The local quality gate (CI is manual-dispatch only) |

## Quick start (development, simulated fleet)

Requires Python 3.12, PostgreSQL 16 server binaries, Node 22 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen --extra dev
(cd web && npm ci && npm run build)
.venv/bin/labmon-devdb start
export LABMON_DATABASE_URL=postgresql+psycopg://labmon@127.0.0.1:55432/labmon LABMON_ENV=development
.venv/bin/labmon-server migrate
.venv/bin/labmon-server bootstrap-dev --password 'change-me-dev-only' --with-dsc
.venv/bin/labmon-collector backfill --config .devdata/collector/collector.toml --hours 2   # optional rehearsal history
.venv/bin/labmon-server serve --port 8000                                                   # terminal 1
.venv/bin/labmon-collector run --config .devdata/collector/collector.toml                   # terminal 2
```

Open <http://127.0.0.1:8000> and sign in as `scientist`. Omit `--with-dsc` for the default
three-instrument fleet. Reset the designated dev state with
`.venv/bin/labmon-devdb reset && rm -rf .devdata`.

## Quality gate

```bash
scripts/check.sh          # lint, strict types, unit + integration tests, UI typecheck + build
scripts/check.sh --e2e    # plus real-process recovery and browser suites
```
