# ADR 0009 — Operator-initiated replay of acknowledged records after a database restore

**Status** Proposed (design only; not implemented) · 2026-11-10

## Context

A point-in-time or dump restore (docs/operations/pitr.md) brings the central database back to a
moment T before the incident. Records the server acknowledged after T are gone from the
database. The collectors that captured them still hold them: after acknowledgement, a record
moves to the collector's **replay cache** and is kept for `replay_retention_days` (default 7)
with its original identity (`collector_id`, `spool_epoch`, `seq`), envelope and digest. No command
re-sends cached records today, so the interval from T to the stop stays a gap in history.

The ingest contract already makes a re-send safe (ADR 0002). Identity is global: a record whose
identity and digest are already in the ledger counts as a duplicate and is not inserted again. The
same identity with a different digest becomes an `ingest_conflict` and is never overwritten.

## Decision

1. **Detect the gap on the server.** The heartbeat already reports each collector's
   `spool_epoch` and `next_seq`. Diagnostics and `doctor` compare these with the ledger's highest
   `seq` per collector and epoch, and with holes inside the ledger's range. A difference that
   pending uploads cannot explain becomes a **critical** fault: "ledger gap: collector C epoch E
   seq A–B missing". `labmon-server admin ledger-gaps [--collector C]` prints the exact ranges.
   It only reads.
2. **Replay on the collector, by exact identity range, started by an operator.** Add
   `labmon-collector replay --config … --epoch E --from-seq A --to-seq B [--dry-run]`. It runs
   with the collector service stopped (it takes the spool lock) and moves the cached
   acknowledged records in that range back to *pending*, without changing them. The normal
   uploader then sends them in new batches. `--dry-run` prints how many of the range are still
   cached and which have been pruned.
3. **The server needs no new write path.** Replayed batches go through the normal ingest with
   all of its validation, including the assignment windows. Already-present records count as
   duplicates, missing ones are committed, and a digest mismatch is recorded as a conflict and
   never applied. A receipt marks the batch `replay=true` so audits can tell replay from live
   traffic.
4. **Report what could not be recovered.** After replay, `ledger-gaps` must show no gap for the
   range. Any part already pruned from the cache stays an explicit, permanent gap. It is recorded
   in the incident report and shown as missing data. It is never filled by interpolation.

## Alternatives considered

- **Server-triggered automatic replay** (the server tells collectors what to re-send). This adds
  a server-to-edge command channel. v1 keeps the edge pull-free and command-free, so this is
  deferred together with remote configuration (ADR 0008).
- **Re-send the whole replay cache on every collector start.** Simple, but it sends up to 7 days
  of duplicates after every restart, and it hides real gaps instead of reporting them.
- **Restore collector spools centrally.** Spools are edge state on many PCs and are not backed up
  centrally, so this would be slower and riskier than replay by identity range.

## Consequences

- Recovery point for the edge: data acknowledged after the restore target survives only while
  the replay cache still holds it. Size `replay_retention_days` to exceed the time needed to
  detect a bad restore and finish one. Raising it costs spool space (SPECIFICATIONS §10 sizing).
- Implementation work, each with tests on real SQLite and PostgreSQL:
  - outbox `requeue_acked(epoch, from, to)`, idempotent and bounded;
  - the `replay` CLI with `--dry-run`;
  - the ledger-gap query and fault;
  - a `replay` flag on receipts;
  - an end-to-end drill: restore to T, detect the gap, replay, then `ledger-gaps` is empty and
    the history has no gap.
- Until it is implemented, pitr.md states the gap honestly.
