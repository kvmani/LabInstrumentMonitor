"""SQLite outbox: identity allocation, persisted batch membership, ACK/reject handling, capacity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from labmon.collector.outbox import Outbox, OutboxError, SpoolFull, choose_journal_mode
from labmon.contracts.envelope import Envelope, EventType, RecordKind

T = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)


def sample(epoch: int, seq: int, value: float = 1.0, collector: str = "col-1") -> Envelope:
    return Envelope(
        kind=RecordKind.SAMPLE,
        collector_id=collector,
        spool_epoch=epoch,
        seq=seq,
        instrument_id="furnace-01",
        profile_revision_id="furnace-01-r1",
        channel_id="furnace-01.pv",
        value=value,
        quality="good",
        capture_time=T + timedelta(seconds=seq),
        simulated=True,
    )


def event(epoch: int, seq: int) -> Envelope:
    return Envelope(
        kind=RecordKind.EVENT,
        collector_id="col-1",
        spool_epoch=epoch,
        seq=seq,
        instrument_id="furnace-01",
        profile_revision_id="furnace-01-r1",
        event_type=EventType.SOURCE_UNAVAILABLE,
        capture_time=T,
        simulated=True,
    )


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    return tmp_path / "spool.sqlite3"


def test_sequence_allocation_is_persistent_and_gapless(spool: Path) -> None:
    ob = Outbox(spool, "col-1", now=lambda: T)
    epoch = ob.epoch
    assert epoch > 1  # derived from creation time: a recreated spool gets a higher epoch
    for _ in range(5):
        ob.append(lambda e, s: sample(e, s))
    ob.close()
    ob = Outbox(spool, "col-1", now=lambda: T + timedelta(days=1))
    assert ob.epoch == epoch and not ob.new_epoch_created
    env = ob.append(lambda e, s: sample(e, s))
    assert env.seq == 6
    assert ob.identities() == [(epoch, i) for i in range(1, 7)]


def test_recreated_spool_gets_new_higher_epoch(spool: Path) -> None:
    first = Outbox(spool, "col-1", now=lambda: T)
    e1 = first.epoch
    first.close()
    spool.unlink()
    second = Outbox(spool, "col-1", now=lambda: T + timedelta(minutes=5))
    assert second.epoch > e1 and second.new_epoch_created


def test_spool_is_bound_to_its_collector(spool: Path) -> None:
    Outbox(spool, "col-1").close()
    with pytest.raises(OutboxError):
        Outbox(spool, "col-2")


def test_identity_mismatch_is_refused(spool: Path) -> None:
    ob = Outbox(spool, "col-1")
    with pytest.raises(OutboxError):
        ob.append(lambda e, s: sample(e, s + 1))
    assert ob.counts().pending == 0


def test_batch_membership_is_fixed_before_send_and_survives_restart(spool: Path) -> None:
    ob = Outbox(spool, "col-1")
    for _ in range(7):
        ob.append(lambda e, s: sample(e, s))
    b1 = ob.next_batch(max_records=5)
    assert b1 is not None and len(b1.envelopes) == 5
    again = ob.next_batch(max_records=5)
    assert again is not None and again.batch_id == b1.batch_id  # same batch until ACK
    payload = b1.payload(T, 1)
    ob.close()
    ob = Outbox(spool, "col-1")
    resumed = ob.next_batch(max_records=5)
    assert resumed is not None and resumed.batch_id == b1.batch_id
    assert resumed.payload(T, 1) == payload  # byte-identical retransmission
    c = ob.counts()
    assert (c.pending, c.batched, c.open_batches) == (2, 5, 1)


def test_ack_dequeues_into_replay_cache(spool: Path) -> None:
    now = [T]
    ob = Outbox(spool, "col-1", now=lambda: now[0])
    for _ in range(3):
        ob.append(lambda e, s: sample(e, s))
    b = ob.next_batch()
    assert b is not None
    assert ob.mark_acked(b.batch_id, receipt_id=11) == 3
    assert ob.mark_acked(b.batch_id, receipt_id=11) == 0  # idempotent
    c = ob.counts()
    assert (c.pending, c.batched, c.acked_cached) == (0, 0, 3)
    assert ob.next_batch() is None
    now[0] = T + timedelta(days=8)
    assert ob.prune_acked(timedelta(days=7)) == 3


def test_rejection_quarantines_named_records_and_rebatches_the_rest(spool: Path) -> None:
    ob = Outbox(spool, "col-1")
    for _ in range(4):
        ob.append(lambda e, s: sample(e, s))
    b = ob.next_batch()
    assert b is not None
    epoch = ob.epoch
    q, rq = ob.mark_rejected(b.batch_id, [(epoch, 2, "type_mismatch", "bad type")], "rejected")
    assert (q, rq) == (1, 3)
    assert ob.quarantined() == [
        {"epoch": epoch, "seq": 2, "code": "type_mismatch", "message": "bad type"}
    ]
    b2 = ob.next_batch()
    assert b2 is not None and b2.batch_id != b.batch_id
    assert [Envelope.model_validate_json(x).seq for x in b2.envelopes] == [1, 3, 4]


def test_unattributed_rejection_quarantines_whole_batch(spool: Path) -> None:
    ob = Outbox(spool, "col-1")
    ob.append(lambda e, s: sample(e, s))
    b = ob.next_batch()
    assert b is not None
    assert ob.mark_rejected(b.batch_id, [], "schema") == (1, 0)
    assert ob.next_batch() is None


def test_capacity_protects_pending_data_and_keeps_a_diagnostic_reserve(spool: Path) -> None:
    ob = Outbox(spool, "col-1", capacity=12, diagnostic_reserve=2)
    for _ in range(10):
        ob.append(lambda e, s: sample(e, s))
    with pytest.raises(SpoolFull):
        ob.append(lambda e, s: sample(e, s))
    ob.append(lambda e, s: event(e, s))  # diagnostics may use the reserve
    ob.append(lambda e, s: event(e, s))
    with pytest.raises(SpoolFull):
        ob.append(lambda e, s: event(e, s))
    assert ob.counts().unacknowledged == 12
    # a refused append does not consume a sequence number
    assert ob.counts().next_seq == 13


def test_frame_append_is_atomic(spool: Path) -> None:
    ob = Outbox(spool, "col-1", capacity=5, diagnostic_reserve=1)
    ob.append_many([lambda e, s: sample(e, s)] * 3)
    with pytest.raises(SpoolFull):
        ob.append_many([lambda e, s: sample(e, s)] * 3)  # would exceed: none persisted
    assert ob.counts().unacknowledged == 3


def test_byte_budget_splits_batches(spool: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import labmon.collector.outbox as mod

    monkeypatch.setattr(mod, "BATCH_BYTE_BUDGET", 2000)
    ob = Outbox(spool, "col-1")
    for _ in range(10):
        ob.append(lambda e, s: sample(e, s))
    b = ob.next_batch()
    assert b is not None and 1 <= len(b.envelopes) < 10
    assert sum(len(x) + 1 for x in b.envelopes) <= 2000


def test_journal_mode_choice() -> None:
    assert choose_journal_mode("auto", (3, 45, 1)) == "delete"
    assert choose_journal_mode("auto", (3, 51, 2)) == "delete"
    assert choose_journal_mode("auto", (3, 51, 3)) == "wal"
    assert choose_journal_mode("wal", (3, 45, 1)) == "wal"
    with pytest.raises(ValueError):
        choose_journal_mode("memory")


def test_pragmas_are_applied(spool: Path) -> None:
    ob = Outbox(spool, "col-1", journal_mode="delete")
    assert ob._conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert ob._conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_unacknowledged_counter_tracks_every_transition(spool: Path) -> None:
    ob = Outbox(spool, "col-1", capacity=1000, diagnostic_reserve=10)

    def counter() -> int:
        return int(ob._meta("unacked") or 0)

    for _ in range(6):
        ob.append(lambda e, s: sample(e, s))
    assert counter() == 6
    b = ob.next_batch(max_records=3)
    assert b is not None and counter() == 6  # batching does not change it
    ob.mark_acked(b.batch_id, 1)
    assert counter() == 3
    b2 = ob.next_batch()
    assert b2 is not None
    ob.mark_rejected(b2.batch_id, [(ob.epoch, 5, "x", "y")], "r")
    assert counter() == 2  # quarantined leaves, requeued stays
    ob._set_meta("unacked", "999")  # corrupted counter is recomputed on open
    ob.close()
    assert int(Outbox(spool, "col-1")._meta("unacked") or 0) == 2


def test_append_cost_does_not_grow_with_backlog(spool: Path) -> None:
    import time

    ob = Outbox(spool, "col-1", capacity=200_000, diagnostic_reserve=10, journal_mode="wal")
    ob._conn.execute("PRAGMA synchronous=OFF")  # measure the query path, not the disk
    batch = [lambda e, s: sample(e, s)] * 100

    def timed(n: int) -> float:
        t0 = time.perf_counter()
        for _ in range(n):
            ob.append_many(batch)
        return time.perf_counter() - t0

    early = timed(20)
    for _ in range(400):  # 40 000 pending records
        ob.append_many(batch)
    late = timed(20)
    assert late < early * 3 + 0.05
