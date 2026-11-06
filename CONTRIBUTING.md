# Contributing and engineering standards

These standards apply to every change, by people and by coding agents. They make the code base
safe to operate in an air-gapped lab, easy to review and cheap to maintain. [AGENTS.md](AGENTS.md)
is the short version; [SPECIFICATIONS.md](SPECIFICATIONS.md) is the product contract.

## Baseline

| Item | Decision |
| --- | --- |
| License | MIT ([LICENSE](LICENSE)). Do not copy code from repositories with incompatible licenses (for example AGPL); reimplement patterns. |
| Platform | Ubuntu 24.04 amd64, Python 3.12, PostgreSQL 16 (confirmed against the owner's systems). Windows 11 for future edge collectors. |
| Python stack | FastAPI, Pydantic 2, SQLAlchemy 2 (Core for hot paths), psycopg 3, Alembic, Uvicorn, httpx, argon2-cffi |
| Browser stack | React 19, TypeScript (strict), Vite, Apache ECharts — bundled locally, no CDN, no web fonts, no telemetry |
| Dependency locks | `uv.lock` (Python, hash-locked) and `web/package-lock.json`; versions pinned exactly |
| CI | `.github/workflows/ci.yml` runs **only on manual dispatch**. Nothing runs automatically on push or pull request. Run `scripts/check.sh` locally before every push. |

## Quality gate

```bash
scripts/check.sh          # ruff, mypy --strict, unit + integration (real PostgreSQL), UI typecheck + build
scripts/check.sh --e2e    # plus real-process recovery and Chromium browser suites
```

A change is not ready while any step fails. Never skip, mark xfail or weaken a test to get green;
fix the cause or record the open defect in `TASK_PROGRESS.md`.

## Definition of done

- [ ] Fits the active roadmap milestone; scope creep is a separate change.
- [ ] `scripts/check.sh` passes (and `--e2e` for acquisition, ingest, status or UI changes).
- [ ] New behaviour has tests at the lowest useful layer, including at least one failure or
      recovery path for any critical workflow (acquisition, spool, ingest, status, export).
- [ ] Scientific changes have known-answer tests with exact expectations (units, sign, ordering).
- [ ] API, schema and fixtures updated together; OpenAPI stays truthful; protocol fixtures in
      `tests/fixtures/` change only with a protocol version decision.
- [ ] Migrations are expand-only for ordinary releases, tested upgrade-with-data and downgrade to
      the documented rollback floor.
- [ ] UI changes reviewed at 1366×768, wide, tablet and phone widths, in light and dark themes,
      with live, stale, simulated and empty data, keyboard only.
- [ ] Docs updated: user-visible behaviour (README / operations), decisions (ADR), progress and
      exact test results (`TASK_PROGRESS.md`). Evidence labelled simulated / staged / field-verified.
- [ ] No secrets, site-confidential material or production data in code, fixtures or logs.

## Python

- Type everything; `mypy --strict` must pass without blanket ignores. Validate at boundaries with
  Pydantic models (`extra="forbid"`, bounded strings and lists).
- Timestamps are timezone-aware UTC internally (`datetime` with `tzinfo`); format with
  `contracts.envelope.format_utc`. Never assign server time to delayed data.
- Keep science in pure, deterministic functions (`sim/`, `server/analysis.py`) that are unit
  tested without I/O. Units are explicit in names or docs (`force_kn`, `area_mm2`).
- Database: SQLAlchemy Core for ingest and queries, explicit transactions (`engine.begin()`),
  bounded result sets, half-open time ranges `[start, end)`, ordering with an identity
  tie-breaker. Never interpolate user input into SQL.
- Errors: catch specific exceptions. A worker loop may catch `Exception` only to log and keep the
  process alive, and must say so in a comment. Do not swallow data-loss conditions.
- Logging: no tokens, passwords or full payloads. Use structured, actionable messages.
- Small modules with one responsibility; prefer one readable implementation per contract.

## TypeScript / UI

- `strict` + `noUncheckedIndexedAccess`; no `any`. Relative API URLs only.
- Charts never smooth raw data, break lines at gaps and invalid values, label units and the time
  zone, and keep acquisition order in XY views. Bucketed data is never labelled as raw samples.
- Every status is conveyed by text and icon, not colour alone. Interactive elements are native
  buttons, links and inputs with accessible names. Respect reduced motion.
- Polling is bounded, aborts on input change, backs off on errors and slows for hidden tabs.
- Colours come from CSS tokens in `styles.css` (light and dark); no inline hex values in components.
- Format with Prettier (`npm run format`, pinned in `web/package.json`, width 100); the gate runs
  `npm run format:check`.

## Data, acquisition and ingest invariants

- A record's identity `(collector_id, spool_epoch, seq)` is allocated with its durable write and
  never reused. Retries resend identical bytes. The server acknowledges only after commit.
- Original quality, source status, raw value, profile revision and time provenance are preserved.
- Heartbeats, cached values and replays never make data look fresh.
- Simulation is labelled everywhere: records, UI, CSV file names and rows, chart titles.
- v1 is read-only: no write-capable adapter path or control API.

## Migrations

- One Alembic revision per change, autogenerated then reviewed and hand-edited; add a docstring
  stating expand/contract nature and data impact.
- Ordinary releases are expand-only so release N-1 runs against schema N. Destructive steps need
  their own plan, approval and backup verification, and never run as part of a code rollback.

## Security

- Every ID-bearing endpoint checks lab scope and returns 404 for out-of-scope IDs.
- Cookie-authenticated writes require the CSRF header and same origin; sessions are opaque,
  revocable and expire. Collector credentials are bearer tokens stored only as hashes.
- Bound every input: body size, decompressed size, list lengths, string lengths, time ranges.
- Escape spreadsheet formulas in CSV text fields. No external requests from the browser.
- Report suspected vulnerabilities privately to the repository owner.

## Dependencies

Add a dependency only with evidence of: maintained upstream, compatible license, wheels/binaries
for Ubuntu 24.04 amd64 (and Windows for collector code), and an offline install path. Record it
in [docs/dependencies.md](docs/dependencies.md) and pin it in the lock file in the same change.

## Commits and branches

- Small, reviewable commits with an imperative subject (≤ 72 characters) and a body explaining
  why. Reference the milestone where useful. Update `TASK_PROGRESS.md` with exact results.
- Record decisions that change acquisition, storage, scientific semantics, security or
  deployment as an ADR in `docs/adr/` (`NNNN-title.md`: context, decision, consequences).
- Follow the owner's branch policy; never force-push shared history.
