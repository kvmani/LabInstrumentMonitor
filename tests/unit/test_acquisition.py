"""Scheduling on a virtual clock: cadence, no bursts after a stall, clock steps, source faults."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from labmon.collector.acquisition import Acquisition
from labmon.collector.config import InstrumentBinding
from labmon.collector.outbox import Outbox
from labmon.contracts.envelope import Envelope
from labmon.sim.clock import VirtualClock
from labmon.sim.sources import Fault, FaultKind

START = datetime(2026, 11, 6, 8, 0, 0, 250_000, tzinfo=UTC)
FIRST_TICK = int(datetime(2026, 11, 6, 8, 0, 1, tzinfo=UTC).timestamp())


def binding(faults: tuple[Fault, ...] = ()) -> InstrumentBinding:
    return InstrumentBinding(
        instrument_id="furnace-01",
        profile="sim-furnace",
        profile_revision_id="furnace-01-r1",
        seed=42,
        channels={"pv": "furnace-01.pv", "sp": "furnace-01.sp", "phase": "furnace-01.phase"},
        faults=faults,
    )


def records(ob: Outbox) -> list[Envelope]:
    rows = ob._conn.execute("SELECT envelope FROM record ORDER BY epoch, seq").fetchall()
    return [Envelope.model_validate_json(r[0]) for r in rows]


@pytest.fixture
def setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    clock = VirtualClock(START)
    ob = Outbox(tmp_path / "s.sqlite3", "col-1", now=clock.now)
    return clock, ob


def test_one_frame_per_slot_aligned_to_whole_seconds(setup) -> None:  # type: ignore[no-untyped-def]
    clock, ob = setup
    acq = Acquisition(ob, clock, (binding(),))
    acq.run(threading.Event(), max_slots=5)
    recs = records(ob)
    samples = [r for r in recs if r.kind == "sample"]
    assert len(samples) == 15
    frames = sorted({r.frame_id for r in samples})
    assert frames == [f"furnace-01:{FIRST_TICK + i}" for i in range(5)]
    assert samples[0].capture_time == datetime(2026, 11, 6, 8, 0, 1, tzinfo=UTC)
    assert [r.event_type for r in recs if r.kind == "event"] == [
        "acquisition_started",
        "spool_epoch_started",
        "acquisition_stopped",
    ]
    assert [r.seq for r in recs] == list(range(1, len(recs) + 1))


def test_stall_skips_missed_slots_without_burst(setup) -> None:  # type: ignore[no-untyped-def]
    clock, ob = setup
    acq = Acquisition(ob, clock, (binding(),))
    acq.start()
    clock.advance(acq.seconds_until_due())
    assert acq.run_due() == 1
    clock.advance(30.0)  # suspended laptop / long GC / stuck disk
    assert acq.run_due() == 1
    assert acq.run_due() == 0  # no burst of the 29 missed polls
    recs = records(ob)
    skipped = [r for r in recs if r.event_type == "schedule_skipped"]
    assert len(skipped) == 1 and skipped[0].detail["skipped_slots"] == 29
    ticks = sorted({int(r.frame_id.split(":")[1]) for r in recs if r.frame_id})
    assert ticks == [FIRST_TICK, FIRST_TICK + 30]


def test_wall_clock_step_is_recorded_and_flagged(setup) -> None:  # type: ignore[no-untyped-def]
    clock, ob = setup
    acq = Acquisition(ob, clock, (binding(),))
    acq.start()
    clock.advance(acq.seconds_until_due())
    acq.run_due()
    clock.advance(1.0)
    clock.jump_wall(3600.0)
    acq.run_due()
    recs = records(ob)
    jump = [r for r in recs if r.event_type == "clock_jump"]
    assert len(jump) == 1 and jump[0].detail["step_s"] == pytest.approx(3600.0)
    last_frame = [r for r in recs if r.kind == "sample"][-3:]
    assert {r.clock_confidence for r in last_frame} == {"suspect"}
    assert {r.quality for r in last_frame} == {"uncertain"}
    assert {r.quality_reason for r in last_frame} == {"clock_suspect"}
    # the source tick follows the schedule, not the stepped wall clock
    assert last_frame[0].frame_id == f"furnace-01:{FIRST_TICK + 1}"


def test_source_dropout_and_recovery_events(setup) -> None:  # type: ignore[no-untyped-def]
    clock, ob = setup
    faults = (Fault(FaultKind.DROPOUT, FIRST_TICK + 2, FIRST_TICK + 5),)
    acq = Acquisition(ob, clock, (binding(faults),))
    acq.run(threading.Event(), max_slots=8)
    recs = records(ob)
    types = [r.event_type for r in recs if r.kind == "event"]
    assert types.count("source_unavailable") == 1 and types.count("source_recovered") == 1
    frames = sorted({r.frame_id for r in recs if r.kind == "sample"})
    assert len(frames) == 5  # 8 slots - 3 unanswered


def test_spool_full_records_lost_interval_after_recovery(setup) -> None:  # type: ignore[no-untyped-def]
    clock, ob = setup
    ob.capacity, ob.diagnostic_reserve = 40, 10
    acq = Acquisition(ob, clock, (binding(),))
    acq.start()
    for _ in range(12):
        clock.advance(max(acq.seconds_until_due(), 0))
        acq.run_due()
    assert acq.stats.spool_full_rejections > 0
    b = ob.next_batch()
    assert b is not None
    ob.mark_acked(b.batch_id, 1)  # upload drains the spool
    clock.advance(max(acq.seconds_until_due(), 0))
    acq.run_due()
    lost = [r for r in records(ob) if r.event_type == "acquisition_lost_interval"]
    assert len(lost) == 1 and lost[0].detail["reason"] == "spool_full"
    assert lost[0].detail["start"] < lost[0].detail["end"]
    json.dumps(lost[0].detail)
