# ADR 0002 — Ingest identity, idempotency and acknowledgement

**Status** Accepted for M1 · 2026-11-06

## Identity

- A record (sample or diagnostic event) is identified by `(collector_id, spool_epoch, seq)`.
  `seq` is allocated in the same SQLite transaction that persists the immutable envelope.
- `spool_epoch` is fixed at spool creation as *minutes since 2020-01-01 UTC* (or a configured
  floor), so a recreated spool always uses a new, higher epoch without contacting the server. A
  `spool_epoch_started` event records the recreation. Risk: a badly wrong wall clock at spool
  creation could reuse an epoch; the server then reports `identity_conflict` instead of
  overwriting (see below), and the operator recovers with a new epoch floor.
- The central `ingest_identity` ledger holds one row per accepted identity independent of the
  sample table layout, so later partitioning cannot weaken replay identity.

## Digests

- Record digest: SHA-256 of the canonical JSON of the envelope (sorted keys, no whitespace, UTC
  timestamps as `YYYY-MM-DDTHH:MM:SS.ffffffZ`, non-finite numbers forbidden).
- Batch digest: SHA-256 over `{v, batch_id, collector_id, [record digests]}`. Transport metadata
  (`sent_at`, `attempt`) is excluded.

## Protocol v1 (`POST /api/v1/ingest/batches`)

| Situation | Response | Edge action |
| --- | --- | --- |
| Committed | 200 `committed`, receipt id, digest | dequeue into replay cache |
| Same batch id, same digest | 200/422 with the recorded outcome, `replayed_receipt=true` | as recorded |
| Same batch id, different digest | 409 `batch_conflict` | stop, keep data, raise fault |
| Identity already committed, same digest | counted as `duplicates` | none |
| Identity already committed, different digest | 422 `identity_conflict`, row in `ingest_conflict` | quarantine record |
| Any permanent per-record fault | 422 whole batch rejected (explicit), identities + codes | quarantine named, re-batch the rest under a new id |
| Malformed envelope with readable identity | 422 `malformed_record` | quarantine |
| Unreadable batch / unsupported version | 400 | stop, raise fault |
| Bad or revoked credential / foreign collector | 401 / 403 | stop upload, keep acquiring |
| Database unavailable, contention | 503 + `Retry-After` | retry same batch with backoff |
| Body > 1 MiB raw or decoded | 413 | stop, raise fault |

Samples, events, identity ledger rows and the receipt commit in **one** PostgreSQL transaction;
the ACK is built only after `COMMIT` returns. Concurrent retransmissions are serialised by the
unique constraints (`INSERT … ON CONFLICT DO NOTHING` waits for the competing transaction); the
loser replays the winner's receipt. Deadlock (`40P01`) and serialisation (`40001`) failures retry
up to five times with a deterministic short delay.

## Time

`event_time` = trusted source sample time if the adapter declares `sample_time`, otherwise the
collector capture time. A capture time more than 5 s ahead of server receipt marks the sample
`clock_confidence=suspect` (`clock_flag=future_capture_time`) but keeps its original quality.
Suspect samples are excluded from freshness. Records more than 24 h in the future or before 2000
are rejected (`time_out_of_bounds`).

## Assignment fence

A record is accepted only inside a `collector_assignment` window
(`valid_from <= capture_time < valid_to`) and, when set, at or below the acknowledged
`(cutoff_epoch, cutoff_seq)`. Assignment history is never deleted.
