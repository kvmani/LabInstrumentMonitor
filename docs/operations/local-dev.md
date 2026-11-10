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
.venv/bin/labmon-server bootstrap-dev --password 'choose-a-dev-password' --with-dsc
.venv/bin/labmon-server serve --port 8000
.venv/bin/labmon-collector run --config .devdata/collector/collector.toml
.venv/bin/labmon-server worker               # rollups + durable exports (separate process)
```

The worker is a separate process so aggregation and exports never block API workers. Export
ZIPs are written to `LABMON_EXPORT_DIR` (default `var/exports`, outside the release) and expire
after 7 days. Without it the
API stays correct (unprocessed minutes are aggregated from raw rows and reported as
`rebuilding_minutes`), but long views get slower. `--once` drains the queue and exits.

## 30-day fixture (M2 evidence)

```bash
.venv/bin/labmon-server load-fixture furnace-30d     # ~4 min: 7.7 M samples, original times
.venv/bin/labmon-server worker --once                # ~2 min: 130 k minute rollups
LABMON_RUN_SLOW=1 .venv/bin/pytest -s tests/perf     # or: scripts/check.sh --slow
```

The fixture instrument `furnace-hist` (lab-demo) holds 30 days from 2026-09-01 with a one-sample
+420 °C peak on day 17 and a 2.5 h dropout on day 9; its assignment covers only that window so it
never looks live. Open it in the UI and choose a custom range (2026-09-01 to 2026-10-01). The
slow suite asserts the peak value and time and the exact dropout gap, and writes latency to
`test-results/perf-furnace-30d.json`.

`bootstrap-dev` creates lab `lab-demo`, the default fleet — `furnace-01` (`sim-furnace`),
`utm-01` (`sim-utm`) and `dil-01` (`sim-dilatometer`) — collector `col-bay1-01` with a fresh
credential in `.devdata/collector/collector.token` (mode 0600), and user `scientist`. Add
`--with-dsc` for the opt-in DSC (`dsc-01`) and `--with-ambient` for the ambient smoke fixture.
Development assignments start 31 days back so rehearsal playback is accepted. It refuses to run
when `LABMON_ENV=production`.

## Rehearsal history (playback)

```bash
# stop the live collector first: one process owns the spool (enforced by an OS lock)
.venv/bin/labmon-collector backfill --config .devdata/collector/collector.toml --hours 3
.venv/bin/labmon-collector run --config .devdata/collector/collector.toml   # uploads the backlog
```

Playback runs the real acquisition code on a virtual clock from `now − hours` to now, with the
original (past) timestamps. Its `acquisition_started` events carry `mode = historical_playback`,
so rehearsal data is distinguishable from live acquisition in the Diagnostics event list and in
CSV/event exports. Three hours produce roughly 27 completed tensile tests, three dilatometer runs,
a furnace heat treatment and four DSC scans. It is not a recovery mechanism.

## What to look at

| Instrument | Overview | Runs & XY analysis |
| --- | --- | --- |
| Furnace | PV with stepped setpoint and ±5 °C tolerance band, phase strip, overshoot at soak start | one run per heat treatment (time series of the run) |
| UTM | force and extension at 5 Hz during tests, test-state strip | stress–strain curve, UTS ≈ 480 MPa at 18 % strain, fracture after UTS |
| Dilatometer | log-scale pressure (pump-down, outgassing burst, scripted gauge error), temperature, signed ΔL | run time series |
| DSC | sample and programmed temperature, heat flow (exo up) | heat flow vs sample temperature: endotherm ≈ 158.5 °C (−286 mJ), exotherm ≈ 153.5 °C |

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

## Soak runs (simulated release evidence)

`labmon-soak` runs the API, worker and collector as separate processes against a **dedicated**
database (it migrates it and bootstraps the demo fleet with the DSC), optionally kills the API on
a schedule, and samples memory, open descriptors, outbox backlog, ingest latency
(`receive_time - capture_time`), staleness per instrument, database size and dirty rollup
minutes. At the end it stops the collector, uploads the rest with `labmon-collector flush` and
checks that every spooled record reached the ledger once.

```bash
createdb -h 127.0.0.1 -p 55432 -U labmon labmon_soak        # or any scratch database
.venv/bin/labmon-soak --database-url postgresql+psycopg://labmon@127.0.0.1:55432/labmon_soak \
    --hours 72 --interval 60 --restart-api-every 240 --out test-results/soak-72h
```

Output: `samples.jsonl` (one line per interval) and `summary.json` (verdict, faults with
recovery times, ledger). Checks: RSS growth ≤ 64 MB and descriptor growth ≤ 32 per process after
warm-up, interval p95 latency ≤ 10 s outside fault recovery, each API kill recovered within 90 s,
outbox drained, no sequence holes or ingest conflicts. Exit 0 means all passed. The e2e suite
runs a 60 s version with two API kills. Evidence from it is **simulated**; record the host, CPU,
RAM and versions with any result you keep.

## Tests

```bash
scripts/check.sh                             # the whole local gate (CI is manual-dispatch only)
scripts/check.sh --e2e                       # plus real-process and browser suites
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
