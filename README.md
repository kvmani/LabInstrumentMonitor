# Lab Instrument Monitor

Read-only, air-gapped instrument monitoring and scientific history. A collector next to the
instrument acquires readings into a durable local outbox and uploads them to a central API backed
by PostgreSQL; scientists use a local web UI. The first release is **simulator-only**.

- Product contract: [SPECIFICATIONS.md](SPECIFICATIONS.md) · plan: [ROADMAP.md](ROADMAP.md) ·
  workflow: [AGENTS.md](AGENTS.md) · progress ledger: [TASK_PROGRESS.md](TASK_PROGRESS.md)
- Decisions: [docs/adr](docs/adr) · local operation: [docs/operations/local-dev.md](docs/operations/local-dev.md)
- License: **not yet confirmed by the repository owner** (open M0 input).

## Layout

| Path | Contents |
| --- | --- |
| `src/labmon/contracts` | Protocol v1 envelopes, batches, ACKs, canonical digests |
| `src/labmon/profiles`, `src/labmon/sim` | Typed profiles, deterministic simulated furnace and ambient sources, virtual clock |
| `src/labmon/collector` | Acquisition scheduler, SQLite outbox, uploader, heartbeat, `labmon-collector` CLI |
| `src/labmon/server` | FastAPI app, SQLAlchemy models, Alembic migrations, ingest, status, history, CSV |
| `web/` | React + TypeScript + Vite + ECharts UI (bundled locally, no CDN) |
| `tests/` | `unit`, `integration` (real PostgreSQL), `e2e` (real processes + Chromium) |

## Quick start (development, simulated furnace)

Requires Python 3.12, PostgreSQL 16 binaries, Node 22 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev                      # locked dependencies into .venv
(cd web && npm ci && npm run build)      # UI bundle in web/dist
.venv/bin/labmon-devdb start             # disposable cluster on 127.0.0.1:55432
export LABMON_DATABASE_URL=postgresql+psycopg://labmon@127.0.0.1:55432/labmon LABMON_ENV=development
.venv/bin/labmon-server migrate
.venv/bin/labmon-server bootstrap-dev --password 'change-me-dev-only'
.venv/bin/labmon-server serve --port 8000          # terminal 1
.venv/bin/labmon-collector run --config .devdata/collector/collector.toml   # terminal 2
```

Open <http://127.0.0.1:8000> and sign in as `scientist`. Inspect the outbox with
`.venv/bin/labmon-collector status --config .devdata/collector/collector.toml`.
Reset the designated dev state with `.venv/bin/labmon-devdb reset && rm -rf .devdata`.

## Tests

```bash
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy
.venv/bin/pytest tests/unit tests/integration       # starts a disposable PostgreSQL 16 cluster
.venv/bin/pytest tests/e2e                          # real API + collector processes, Chromium
(cd web && npm run typecheck)
```
