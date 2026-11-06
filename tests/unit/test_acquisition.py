"""Scheduling on a virtual clock: per-instrument cadence, no bursts, clock steps, faults, runs."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from labmon.collector.acquisition import Acquisition
from labmon.collector.config import InstrumentBinding
from labmon.collector.outbox import Outbox
from labmon.contracts.envelope import Envelope
from labmon.profiles import PROFILES
from labmon.sim.base import ANCHOR_MS, Fault, FaultKind
from labmon.sim.clock import VirtualClock

START = datetime(2026, 11, 6, 8, 0, 0, 250_000, tzinfo=UTC)
FIRST_MS = int(datetime(2026, 11, 6, 8, 0, 1, tzinfo=UTC).timestamp() * 1000)


def binding(
    profile: str = "sim-furnace", iid: str = "furnace-01", faults: tuple[Fault, ...] = ()
) -> InstrumentBinding:
    keys = [c.key for c in PROFILES[profile].channels]
    return InstrumentBinding(
        instrument_id=iid,
        profile=profile,
        profile_revision_id=f"{iid}-r1",
        seed=42,
        channels={k: f"{iid}.{k}" for k in keys},
        faults=faults,
    )


def records(ob: Outbox) -> list[Envelope]:
    rows = ob._conn.execute("SELECT envelope FROM record ORDER BY epoch, seq").fetchall()
    return [Envelope.model_validate_json(r[0]) for r in rows]


def frames_of(recs: list[Envelope], iid: str) -> list[int]:
    return sorted(
        {int(r.frame_id.split(":")[1]) for r in recs if r.frame_id and r.instrument_id == iid}
    )


def make(tmp_path: Path, start: datetime = START):  # type: ignore[no-untyped-def]
    clock = VirtualClock(start)
    return clock, Outbox(tmp_path / "s.sqlite3", "col-1", now=clock.now)


def step(acq: Acquisition, clock: VirtualClock) -> int:
    clock.advance(max(acq.seconds_until_due(), 0))
    return acq.run_due()


def test_one_frame_per_second_aligned_to_whole_seconds(tmp_path: Path) -> None:
    clock, ob = make(tmp_path)
    acq = Acquisition(ob, clock, (binding(),))
    acq.run(threading.Event(), max_frames=5)
    recs = records(ob)
    assert len([r for r in recs if r.kind == "sample"]) == 15
    assert frames_of(recs, "furnace-01") == [FIRST_MS + 1000 * i for i in range(5)]
    first = next(r for r in recs if r.kind == "sample")
    assert first.capture_time == datetime(2026, 11, 6, 8, 0, 1, tzinfo=UTC)
    types = [r.event_type for r in recs if r.kind == "event"]
    assert types[:2] == ["acquisition_started", "spool_epoch_started"]
    assert types[-1] == "acquisition_stopped"
    assert [r.seq for r in recs] == list(range(1, len(recs) + 1))


def test_stall_skips_missed_slots_without_burst(tmp_path: Path) -> None:
    clock, ob = make(tmp_path)
    acq = Acquisition(ob, clock, (binding(),))
    acq.start()
    assert step(acq, clock) == 1
    clock.advance(30.0)  # suspended laptop / long GC / stuck disk
    assert acq.run_due() == 1
    assert acq.run_due() == 0  # no burst of the missed polls
    recs = records(ob)
    skipped = [r for r in recs if r.event_type == "schedule_skipped"]
    assert len(skipped) == 1 and skipped[0].detail["skipped_slots"] == 29  # type: ignore[index]
    assert frames_of(recs, "furnace-01") == [FIRST_MS, FIRST_MS + 30_000]


def test_wall_clock_step_is_recorded_and_flagged(tmp_path: Path) -> None:
    clock, ob = make(tmp_path)
    acq = Acquisition(ob, clock, (binding(),))
    acq.start()
    step(acq, clock)
    clock.jump_wall(3600.0)
    step(acq, clock)
    recs = records(ob)
    jump = [r for r in recs if r.event_type == "clock_jump"]
    assert len(jump) == 1 and jump[0].detail["step_s"] == pytest.approx(3600.0)  # type: ignore[index]
    last_frame = [r for r in recs if r.kind == "sample"][-3:]
    assert {r.clock_confidence for r in last_frame} == {"suspect"}
    assert {r.quality_reason for r in last_frame} == {"clock_suspect"}
    # the nominal sample time follows the schedule, not the stepped wall clock
    assert last_frame[0].frame_id == f"furnace-01:{FIRST_MS + 1000}"


def test_source_dropout_and_recovery_events(tmp_path: Path) -> None:
    clock, ob = make(tmp_path)
    t0 = FIRST_MS // 1000
    faults = (Fault(FaultKind.DROPOUT, t0 + 2, t0 + 5),)
    acq = Acquisition(ob, clock, (binding(faults=faults),))
    acq.start()
    for _ in range(8):
        step(acq, clock)
    recs = records(ob)
    types = [r.event_type for r in recs if r.kind == "event"]
    assert types.count("source_unavailable") == 1 and types.count("source_recovered") == 1
    assert len(frames_of(recs, "furnace-01")) == 5  # 8 slots - 3 unanswered


def test_spool_full_records_lost_interval_after_recovery(tmp_path: Path) -> None:
    clock, ob = make(tmp_path)
    ob.capacity, ob.diagnostic_reserve = 40, 10
    acq = Acquisition(ob, clock, (binding(),))
    acq.start()
    for _ in range(12):
        step(acq, clock)
    assert acq.stats.spool_full_rejections > 0
    b = ob.next_batch()
    assert b is not None
    ob.mark_acked(b.batch_id, 1)
    step(acq, clock)
    lost = [r for r in records(ob) if r.event_type == "acquisition_lost_interval"]
    assert len(lost) == 1 and lost[0].detail["reason"] == "spool_full"  # type: ignore[index]


def test_independent_cadences_and_utm_5hz_during_a_test(tmp_path: Path) -> None:
    # start 2 s before the first UTM preload (cycle 0, idle 240 s)
    start = datetime.fromtimestamp((ANCHOR_MS + 238_000) / 1000, UTC)
    clock, ob = make(tmp_path, start)
    acq = Acquisition(
        ob, clock, (binding("sim-utm", "utm-01"), binding("sim-dilatometer", "dil-01"))
    )
    acq.start()
    while clock.now() < start + timedelta(seconds=12):
        step(acq, clock)
    recs = records(ob)
    utm = frames_of(recs, "utm-01")
    dil = frames_of(recs, "dil-01")
    preload = ANCHOR_MS + 240_000
    assert [t for t in utm if t < preload] == [ANCHOR_MS + 239_000]  # 1 Hz while idle
    after = [t for t in utm if t >= preload]
    assert after[:3] == [preload, preload + 200, preload + 400]  # 5 Hz once the test starts
    assert all(b - a == 1000 for a, b in zip(dil, dil[1:], strict=False))
    started = [r for r in recs if r.event_type == "run_started"]
    assert [r.instrument_id for r in started] == ["dil-01", "utm-01"] or [
        r.instrument_id for r in started
    ] == ["utm-01", "dil-01"]
    utm_run = next(r for r in started if r.instrument_id == "utm-01")
    assert utm_run.detail["run_key"] == "c000000"  # type: ignore[index]
    assert utm_run.detail["metadata"]["geometry"]["gauge_length_mm"] == 25.0  # type: ignore[index]


def test_run_end_and_next_start_are_recorded_once(tmp_path: Path) -> None:
    start = datetime.fromtimestamp((ANCHOR_MS + 400_000) / 1000, UTC)  # inside UTM cycle 0
    clock, ob = make(tmp_path, start)
    acq = Acquisition(ob, clock, (binding("sim-utm", "utm-01"),))
    acq.start()
    while clock.now() < start + timedelta(seconds=250):  # past the next preload at 645 s
        step(acq, clock)
    events = [
        (r.event_type, r.detail.get("run_key"))
        for r in records(ob)  # type: ignore[union-attr]
        if r.event_type in ("run_started", "run_ended")
    ]
    assert events == [
        ("run_started", "c000000"),
        ("run_ended", "c000000"),
        ("run_started", "c000001"),
    ]
