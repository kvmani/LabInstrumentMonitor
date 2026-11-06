# ADR 0003 — SQLite outbox journal mode

**Status** Accepted for M1 · 2026-11-06

The specification prefers SQLite WAL with `synchronous=FULL`, but only on a build that includes the
upstream WAL-reset corruption fix (or a documented vendor backport); otherwise a fully synchronised
rollback journal.

## Decision

- `journal_mode = "auto"` (default) selects **WAL** when the linked SQLite is ≥ 3.51.3 and the
  **rollback journal (`DELETE`)** otherwise. `synchronous=FULL` in both cases.
- The threshold 3.51.3 is our reading of the upstream release notes and must be re-verified
  against <https://sqlite.org/wal.html> and the exact build shipped for each target platform
  before the M3 release (open item).
- `labmon-collector status` prints the linked SQLite version and the active journal mode so
  commissioning records capture it.

## Evidence

Development host (simulated): CPython 3.12.3 links SQLite **3.45.1** → rollback journal selected.
The crash/throughput gates in ROADMAP M1/M3 must be measured with the mode actually selected on
the Windows 11 and Ubuntu 24.04 edge builds.
