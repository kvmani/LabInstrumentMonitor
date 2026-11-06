# Local development and simulator operation

Development and CI only. This is not a deployment procedure (M3 delivers service packaging,
HTTPS and backup runbooks). Evidence produced this way is **simulated**.

## Prerequisites

| Tool | Version used for M1 evidence |
| --- | --- |
| Python | 3.12.3 (linked SQLite 3.45.1) |
| PostgreSQL server binaries | 16.15 (`/usr/lib/postgresql/16/bin`, or set `LABMON_PG_BINDIR`) |
| uv | any recent; installs exactly what `uv.lock` pins |
| Node.js / npm | 22 / 10 (UI build only, never at runtime) |
| Chromium | bundled with Playwright 1.63 (browser tests only) |

## One command per step

```bash
uv sync --frozen --extra dev
(cd web && npm ci && npm run build)
.venv/bin/labmon-devdb start          # 127.0.0.1:55432, trust auth on loopback, data in .devdata/pg
export LABMON_DATABASE_URL=postgresql+psycopg://labmon@127.0.0.1:55432/labmon
export LABMON_ENV=development         # enables bootstrap-dev and /api/docs; cookies not Secure
.venv/bin/labmon-server migrate
.venv/bin/labmon-server bootstrap-dev --password 'choose-a-dev-password'
.venv/bin/labmon-server serve --port 8000
.venv/bin/labmon-collector run --config .devdata/collector/collector.toml
```

`bootstrap-dev` creates lab `lab-demo`, instrument `furnace-01` (profile `sim-furnace`, seed 42,
revision `furnace-01-r1`), collector `col-bay1-01` with a fresh credential in
`.devdata/collector/collector.token` (mode 0600), and user `scientist`. Add `--with-ambient` for
the ambient smoke fixture. It refuses to run when `LABMON_ENV=production`.

When running as root (containers), the cluster runs as the `postgres` account; the cluster
directory and its parents must be traversable by that account, otherwise choose
`--dir /tmp/labmon-pg`.

## Inspect and reset

```bash
.venv/bin/labmon-collector status --config .devdata/collector/collector.toml [--json]
.venv/bin/labmon-collector quarantine --config .devdata/collector/collector.toml
curl -s http://127.0.0.1:8000/health/ready
```

Reset the designated development state (deletes only `.devdata`):

```bash
pkill -f "labmon-collector run"; pkill -f "labmon-server serve"
.venv/bin/labmon-devdb reset && rm -rf .devdata
```

## Demonstrating failure behaviour

| Action | Expected observation |
| --- | --- |
| Stop the API for a minute | Collector keeps sampling; `status` shows growing *pending*; browser banner "Cannot reach the monitor service"; after restart the backlog drains and history has no hole |
| Stop the collector | Within ~5 s the furnace shows **Stale · collector online · no new readings since …**; after 30 s **Collector offline · last good reading at …**; process state **Unknown** with last known value |
| Restart the collector | Same spool epoch, sequence continues; the stopped interval is shown as an explicit gap |
| Revoke the credential (`UPDATE collector_credential SET revoked_at = now()`) | Collector reports an upload fault and keeps spooling; nothing is dropped |

## Tests

```bash
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy
.venv/bin/pytest tests/unit                  # no database, virtual clocks, no sleeps
.venv/bin/pytest tests/integration           # real PostgreSQL 16, one cloned DB per test
.venv/bin/pytest tests/e2e                   # real processes + Chromium; needs web/dist
LABMON_SCREENSHOTS=1 .venv/bin/pytest tests/e2e/test_browser.py   # review screenshots
(cd web && npm run typecheck)
```

Set `LABMON_TEST_PG_ADMIN_URL=postgresql+psycopg://postgres@host:5432/postgres` to use an existing
server instead of the auto-started disposable cluster.

## Offline dependency collection (air-gapped hosts)

On a connected staging machine with the same OS/architecture as the target:

```bash
uv export --frozen --no-dev --no-emit-project -o requirements.txt
python3.12 -m pip download -r requirements.txt --only-binary=:all: -d wheelhouse/
uv build --wheel -o wheelhouse/                       # the labmon wheel itself
(cd web && npm ci && npm run build)                    # ship web/dist prebuilt
sha256sum wheelhouse/* > wheelhouse/SHA256SUMS
```

Transfer `wheelhouse/`, `web/dist/` and `SHA256SUMS` through the approved channel, verify the
checksums, then on the target:

```bash
python3.12 -m venv /opt/labmon/venv
/opt/labmon/venv/bin/pip install --no-index --find-links wheelhouse -r requirements.txt wheelhouse/labmon-*.whl
```

For a Windows 11 collector, run the download step with `--platform win_amd64 --python-version 3.12`.
Licenses are listed in [../dependencies.md](../dependencies.md). The full release procedure,
SBOM and signed artifacts are M3 deliverables.
