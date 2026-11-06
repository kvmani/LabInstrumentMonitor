# ADR 0001 — Stack and process boundaries

**Status** Accepted for M0/M1 · 2026-11-06

## Decision

- **Central app:** one Python 3.12 package (`labmon`), FastAPI + Pydantic 2 + SQLAlchemy 2 (Core
  for hot paths) + psycopg 3 + Alembic, served by Uvicorn. Modular monolith: `contracts`,
  `profiles`, `sim`, `collector`, `server`.
- **Database:** PostgreSQL 16, one database. Unpartitioned `sample` table keyed by global
  identity with a `(channel_id, event_time, identity)` B-tree. No time-series extension until a
  benchmark justifies one (spec §2).
- **Collector:** separate process (`labmon-collector run`), never a web worker. One process owns
  the SQLite outbox through one serialised connection. Acquisition, upload and heartbeat run in
  separate threads so network stalls cannot delay the schedule.
- **Browser:** React 18 + TypeScript + Vite + Apache ECharts, bundled into `web/dist` and served by
  the API from the same origin with relative URLs. No CDN, fonts or telemetry.
- **Live update:** HTTP polling (2 s focused instrument, 5 s fleet) with abort-on-change,
  error backoff and slower polling for hidden tabs. SSE is deferred until measured.
- **Dev/test database:** `labmon-devdb` starts a disposable loopback-only cluster with the local
  PostgreSQL 16 binaries; tests clone a migrated template database per test.

## Consequences

- The collector keeps running when the API, the database or every browser is gone.
- The same package ships both sides; the collector imports no server modules and vice versa except
  the shared `contracts` and `profiles`.
- Packaging for Windows/Ubuntu services and offline wheelhouses is M3 work.
