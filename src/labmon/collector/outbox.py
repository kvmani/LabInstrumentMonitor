"""Durable SQLite outbox owned by a single collector process.

Guarantees (see docs/adr/0002-ingest-identity.md and docs/adr/0003-outbox-journal.md):

* A sequence number is allocated and the immutable envelope persisted in one transaction, before
  any transmission. The stored canonical JSON is what is sent, byte for byte, on every retry.
* Batch ID and exact batch membership are persisted before the first send. A lost ACK leads to a
  retry of the same batch, never a re-batching.
* Records leave the pending set only on a committed central acknowledgement, and remain in a
  bounded replay cache afterwards. Rejected records are quarantined with their reason; valid
  siblings return to pending and are re-batched under a new batch ID. Nothing is silently deleted.
* The spool is capped. Measurements stop being accepted at ``capacity - diagnostic_reserve``;
  diagnostic events may use the reserve so the lost interval can be recorded.

One connection, serialised by a lock, performs all spool I/O.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from labmon.contracts.envelope import (
    MAX_BATCH_DECODED_BYTES,
    MAX_BATCH_RECORDS,
    Envelope,
    RecordKind,
    batch_digest,
    format_utc,
)

SCHEMA_VERSION = 1
# Upstream fixed the WAL-reset corruption bug in 3.51.3 (assumption recorded in ADR 0003; verify
# against the release notes of the exact linked build). Older builds use a rollback journal.
WAL_SAFE_SQLITE = (3, 51, 3)
EPOCH_BASE = datetime(2020, 1, 1, tzinfo=UTC)
# leave room for the batch wrapper inside the decoded-size cap
BATCH_BYTE_BUDGET = MAX_BATCH_DECODED_BYTES - 16 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS epoch (
  epoch INTEGER PRIMARY KEY, started_at TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS record (
  epoch INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  envelope TEXT NOT NULL,
  digest TEXT NOT NULL,
  size INTEGER NOT NULL,
  capture_time TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending', 'batched', 'acked', 'quarantined')),
  batch_id TEXT,
  acked_at TEXT,
  quarantine_code TEXT,
  quarantine_message TEXT,
  PRIMARY KEY (epoch, seq)
);
CREATE INDEX IF NOT EXISTS record_state ON record (state, epoch, seq);
CREATE INDEX IF NOT EXISTS record_batch ON record (batch_id);
CREATE TABLE IF NOT EXISTS batch (
  batch_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open', 'acked', 'rejected', 'conflict')),
  record_count INTEGER NOT NULL,
  digest TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TEXT,
  last_error TEXT,
  acked_at TEXT,
  receipt_id INTEGER
);
"""


class SpoolFull(Exception):
    """The pending spool reached its measurement capacity."""


class OutboxError(Exception):
    pass


@dataclass(frozen=True)
class PendingBatch:
    batch_id: str
    collector_id: str
    digest: str
    envelopes: tuple[str, ...]  # stored canonical JSON, sent verbatim
    attempts: int

    def payload(self, sent_at: datetime, attempt: int) -> bytes:
        body = (
            '{"v":1,"batch_id":"'
            + self.batch_id
            + '","collector_id":"'
            + self.collector_id
            + '","sent_at":"'
            + format_utc(sent_at)
            + '","attempt":'
            + str(attempt)
            + ',"records":['
            + ",".join(self.envelopes)
            + "]}"
        )
        return body.encode("utf-8")


@dataclass(frozen=True)
class OutboxCounts:
    collector_id: str
    epoch: int
    next_seq: int
    pending: int
    batched: int
    acked_cached: int
    quarantined: int
    open_batches: int
    oldest_pending_capture_time: str | None
    capacity: int
    journal_mode: str
    sqlite_version: str
    upload_fault: str | None

    @property
    def unacknowledged(self) -> int:
        return self.pending + self.batched

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "unacknowledged": self.unacknowledged}


def choose_journal_mode(requested: str = "auto", version: tuple[int, ...] | None = None) -> str:
    version = version or sqlite3.sqlite_version_info
    if requested == "auto":
        return "wal" if tuple(version) >= WAL_SAFE_SQLITE else "delete"
    if requested not in ("wal", "delete"):
        raise ValueError("journal_mode must be auto, wal or delete")
    return requested


def _utc_text(dt: datetime) -> str:
    return format_utc(dt)


class Outbox:
    def __init__(
        self,
        path: Path,
        collector_id: str,
        *,
        capacity: int = 200_000,
        diagnostic_reserve: int = 1_000,
        journal_mode: str = "auto",
        min_epoch: int = 1,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if capacity <= diagnostic_reserve:
            raise ValueError("capacity must exceed the diagnostic reserve")
        self.path = Path(path)
        self.capacity = capacity
        self.diagnostic_reserve = diagnostic_reserve
        self._now = now
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False, timeout=30.0
        )
        self.journal_mode = choose_journal_mode(journal_mode)
        mode = self._conn.execute(f"PRAGMA journal_mode={self.journal_mode}").fetchone()[0]
        if mode.lower() != self.journal_mode:
            raise OutboxError(f"could not set journal_mode={self.journal_mode} (got {mode})")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._tx():
            for stmt in filter(None, (s.strip() for s in _SCHEMA.split(";"))):
                self._conn.execute(stmt)
            existing = self._meta("collector_id")
            if existing is None:
                epoch = max(min_epoch, self._derive_epoch())
                self._set_meta("schema_version", str(SCHEMA_VERSION))
                self._set_meta("collector_id", collector_id)
                self._set_meta("epoch", str(epoch))
                self._set_meta("next_seq", "1")
                self._conn.execute(
                    "INSERT INTO epoch (epoch, started_at, reason) VALUES (?, ?, ?)",
                    (epoch, _utc_text(self._now()), "new spool"),
                )
                self.new_epoch_created = True
            elif existing != collector_id:
                raise OutboxError(f"spool belongs to collector {existing!r}, not {collector_id!r}")
            else:
                self.new_epoch_created = False
        self.collector_id = collector_id

    def _derive_epoch(self) -> int:
        # Minutes since 2020-01-01: a recreated spool gets a higher epoch than its predecessor
        # without contacting the server, as long as the wall clock is roughly right.
        return int((self._now() - EPOCH_BASE).total_seconds() // 60) + 1

    # --- low-level helpers ---------------------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def epoch(self) -> int:
        with self._lock:
            return int(self._meta("epoch") or 0)

    # --- append --------------------------------------------------------------------------------

    def append(self, build: Callable[[int, int], Envelope]) -> Envelope:
        """Allocate ``(epoch, seq)``, build the envelope and persist it atomically."""
        return self.append_many([build])[0]

    def append_many(self, builders: list[Callable[[int, int], Envelope]]) -> list[Envelope]:
        """Persist several envelopes (one acquisition frame) in a single transaction."""
        with self._tx() as conn:
            epoch = int(self._meta("epoch") or 0)
            seq = int(self._meta("next_seq") or 1)
            envs = [build(epoch, seq + i) for i, build in enumerate(builders)]
            for i, env in enumerate(envs):
                if env.identity != (self.collector_id, epoch, seq + i):
                    raise OutboxError("envelope identity does not match the allocated identity")
            unacked = conn.execute(
                "SELECT COUNT(*) FROM record WHERE state IN ('pending', 'batched')"
            ).fetchone()[0]
            only_events = all(e.kind is RecordKind.EVENT for e in envs)
            limit = self.capacity if only_events else self.capacity - self.diagnostic_reserve
            if unacked + len(envs) > limit:
                raise SpoolFull(f"{unacked} unacknowledged records (limit {limit})")
            rows = []
            for env in envs:
                text = env.canonical()
                rows.append(
                    (
                        env.spool_epoch,
                        env.seq,
                        env.kind.value,
                        text,
                        env.digest(),
                        len(text.encode()),
                        _utc_text(env.capture_time),
                    )
                )
            conn.executemany(
                "INSERT INTO record (epoch, seq, kind, envelope, digest, size, capture_time, state)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                rows,
            )
            self._set_meta("next_seq", str(seq + len(envs)))
            return envs

    # --- batching ------------------------------------------------------------------------------

    def next_batch(self, max_records: int = MAX_BATCH_RECORDS) -> PendingBatch | None:
        """Return the oldest open batch, or persist a new one from pending records."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT batch_id, digest, attempts FROM batch WHERE state = 'open' "
                "ORDER BY created_at, rowid LIMIT 1"
            ).fetchone()
            if row is not None:
                envs = tuple(
                    r[0]
                    for r in conn.execute(
                        "SELECT envelope FROM record WHERE batch_id = ? ORDER BY epoch, seq",
                        (row[0],),
                    )
                )
                return PendingBatch(row[0], self.collector_id, row[1], envs, int(row[2]))
            rows = conn.execute(
                "SELECT epoch, seq, envelope, digest, size FROM record WHERE state = 'pending' "
                "ORDER BY epoch, seq LIMIT ?",
                (max_records,),
            ).fetchall()
            chosen: list[tuple[int, int, str, str]] = []
            total = 0
            for epoch, seq, text, digest, size in rows:
                if chosen and total + size + 1 > BATCH_BYTE_BUDGET:
                    break
                chosen.append((epoch, seq, text, digest))
                total += size + 1
            if not chosen:
                return None
            batch_id = uuid.uuid4().hex
            digest = batch_digest(batch_id, self.collector_id, [c[3] for c in chosen])
            conn.execute(
                "INSERT INTO batch (batch_id, created_at, state, record_count, digest) "
                "VALUES (?, ?, 'open', ?, ?)",
                (batch_id, _utc_text(self._now()), len(chosen), digest),
            )
            conn.executemany(
                "UPDATE record SET state = 'batched', batch_id = ? WHERE epoch = ? AND seq = ?",
                [(batch_id, c[0], c[1]) for c in chosen],
            )
            return PendingBatch(batch_id, self.collector_id, digest, tuple(c[2] for c in chosen), 0)

    def record_attempt(self, batch_id: str, error: str | None) -> int:
        with self._tx() as conn:
            conn.execute(
                "UPDATE batch SET attempts = attempts + 1, last_attempt_at = ?, last_error = ? "
                "WHERE batch_id = ?",
                (_utc_text(self._now()), error, batch_id),
            )
            row = conn.execute("SELECT attempts FROM batch WHERE batch_id = ?", (batch_id,))
            return int(row.fetchone()[0])

    def mark_acked(self, batch_id: str, receipt_id: int) -> int:
        """Dequeue a batch after a committed central acknowledgement."""
        now = _utc_text(self._now())
        with self._tx() as conn:
            state = conn.execute(
                "SELECT state FROM batch WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if state is None:
                raise OutboxError(f"unknown batch {batch_id}")
            if state[0] == "acked":
                return 0
            conn.execute(
                "UPDATE batch SET state = 'acked', acked_at = ?, receipt_id = ? WHERE batch_id = ?",
                (now, receipt_id, batch_id),
            )
            cur = conn.execute(
                "UPDATE record SET state = 'acked', acked_at = ? "
                "WHERE batch_id = ? AND state = 'batched'",
                (now, batch_id),
            )
            return cur.rowcount

    def mark_rejected(
        self, batch_id: str, rejected: list[tuple[int, int, str, str]], batch_reason: str
    ) -> tuple[int, int]:
        """Quarantine rejected identities and return valid siblings to pending.

        ``rejected`` holds ``(epoch, seq, code, message)``. An empty list means the whole batch was
        rejected without per-record attribution, so every member is quarantined.
        Returns ``(quarantined, requeued)``.
        """
        with self._tx() as conn:
            members = {
                (int(e), int(s))
                for e, s in conn.execute(
                    "SELECT epoch, seq FROM record WHERE batch_id = ? AND state = 'batched'",
                    (batch_id,),
                )
            }
            targets = {(e, s): (c, m) for e, s, c, m in rejected if (e, s) in members}
            if not targets:
                targets = {k: ("batch_rejected", batch_reason) for k in members}
            for (e, s), (code, msg) in targets.items():
                conn.execute(
                    "UPDATE record SET state = 'quarantined', quarantine_code = ?, "
                    "quarantine_message = ? WHERE epoch = ? AND seq = ?",
                    (code, msg[:500], e, s),
                )
            requeue = members - set(targets)
            conn.executemany(
                "UPDATE record SET state = 'pending', batch_id = NULL WHERE epoch = ? AND seq = ?",
                list(requeue),
            )
            conn.execute(
                "UPDATE batch SET state = 'rejected', last_error = ? WHERE batch_id = ?",
                (batch_reason[:500], batch_id),
            )
            return len(targets), len(requeue)

    def mark_conflict(self, batch_id: str, reason: str) -> None:
        """Central API holds a different payload under this batch ID: stop, keep evidence."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE batch SET state = 'conflict', last_error = ? WHERE batch_id = ?",
                (reason[:500], batch_id),
            )
            self._set_meta("upload_fault", f"batch_conflict:{batch_id}")

    def set_upload_fault(self, fault: str | None) -> None:
        with self._tx() as conn:
            if fault is None:
                conn.execute("DELETE FROM meta WHERE key = 'upload_fault'")
            else:
                self._set_meta("upload_fault", fault[:200])

    def prune_acked(self, retention: timedelta) -> int:
        """Drop acknowledged records older than the replay-cache horizon."""
        cutoff = _utc_text(self._now() - retention)
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM record WHERE state = 'acked' AND acked_at < ?", (cutoff,)
            )
            conn.execute(
                "DELETE FROM batch WHERE state IN ('acked', 'rejected') AND batch_id NOT IN "
                "(SELECT DISTINCT batch_id FROM record WHERE batch_id IS NOT NULL) "
                "AND created_at < ?",
                (cutoff,),
            )
            return cur.rowcount

    # --- inspection ----------------------------------------------------------------------------

    def counts(self) -> OutboxCounts:
        with self._lock:
            by_state = dict(
                self._conn.execute("SELECT state, COUNT(*) FROM record GROUP BY state").fetchall()
            )
            oldest = self._conn.execute(
                "SELECT MIN(capture_time) FROM record WHERE state IN ('pending', 'batched')"
            ).fetchone()[0]
            open_batches = self._conn.execute(
                "SELECT COUNT(*) FROM batch WHERE state = 'open'"
            ).fetchone()[0]
            return OutboxCounts(
                collector_id=self.collector_id,
                epoch=int(self._meta("epoch") or 0),
                next_seq=int(self._meta("next_seq") or 1),
                pending=int(by_state.get("pending", 0)),
                batched=int(by_state.get("batched", 0)),
                acked_cached=int(by_state.get("acked", 0)),
                quarantined=int(by_state.get("quarantined", 0)),
                open_batches=int(open_batches),
                oldest_pending_capture_time=oldest,
                capacity=self.capacity,
                journal_mode=self.journal_mode,
                sqlite_version=sqlite3.sqlite_version,
                upload_fault=self._meta("upload_fault"),
            )

    def quarantined(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT epoch, seq, quarantine_code, quarantine_message FROM record "
                "WHERE state = 'quarantined' ORDER BY epoch, seq"
            ).fetchall()
        return [{"epoch": r[0], "seq": r[1], "code": r[2], "message": r[3]} for r in rows]

    def identities(self, state: str | None = None) -> list[tuple[int, int]]:
        with self._lock:
            if state is None:
                rows = self._conn.execute("SELECT epoch, seq FROM record ORDER BY epoch, seq")
            else:
                rows = self._conn.execute(
                    "SELECT epoch, seq FROM record WHERE state = ? ORDER BY epoch, seq", (state,)
                )
            return [(int(e), int(s)) for e, s in rows]
