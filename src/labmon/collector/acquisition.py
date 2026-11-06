"""Scheduled acquisition into the outbox.

* Every instrument has its own schedule on *nominal* time (milliseconds since the Unix epoch).
  The source decides its interval after each sample (``cadence_ms``), e.g. 5 Hz while a tensile
  test runs and 1 Hz otherwise. Nominal time maps to the monotonic clock from one origin, so a
  wall-clock step can neither duplicate nor reorder samples.
* After a stall (sleep, suspend, slow disk) a schedule jumps to the latest nominal time that is
  due and records one ``schedule_skipped`` event instead of firing a burst of missed polls.
* Wall-clock steps are recorded as ``clock_jump`` events; samples captured across the step are
  marked with suspect clock confidence.
* A source that does not answer produces ``source_unavailable`` / ``source_recovered`` events. The
  collector stays online and keeps scheduling.
* Runs reported by the instrument program produce ``run_started`` / ``run_ended`` events carrying
  the run metadata (for example tensile specimen geometry).
* When the spool is full, measurements are not accepted; after recovery an
  ``acquisition_lost_interval`` event records the interval that could not be captured.
"""

from __future__ import annotations

import functools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from labmon.collector.config import InstrumentBinding
from labmon.collector.outbox import Outbox, SpoolFull
from labmon.contracts.envelope import (
    ClockConfidence,
    Envelope,
    EventType,
    Quality,
    QualityReason,
    RecordKind,
    format_utc,
)
from labmon.sim.base import RunInfo, Source, SourceReading, SourceUnavailable
from labmon.sim.clock import Clock
from labmon.sim.sources import build_source

log = logging.getLogger(__name__)

CLOCK_JUMP_TOLERANCE_S = 2.0


def _build_envelope(fields: dict[str, Any], epoch: int, seq: int) -> Envelope:
    return Envelope(spool_epoch=epoch, seq=seq, **fields)


def _utc_ms(t_ms: int) -> str:
    return format_utc(datetime.fromtimestamp(t_ms / 1000, UTC))


@dataclass
class _InstrumentState:
    binding: InstrumentBinding
    source: Source
    next_t_ms: int = 0
    available: bool = True
    unavailable_since: datetime | None = None
    reason: str | None = None
    spool_full_since: datetime | None = None
    run: RunInfo | None = None
    run_known: bool = False  # False until the first reading after start


@dataclass
class AcquisitionStats:
    frames: int = 0
    samples: int = 0
    events: int = 0
    skipped_slots: int = 0
    spool_full_rejections: int = 0
    source_states: dict[str, str] = field(default_factory=dict)


class Acquisition:
    def __init__(
        self,
        outbox: Outbox,
        clock: Clock,
        instruments: tuple[InstrumentBinding, ...],
        simulated: bool = True,
    ) -> None:
        self.outbox = outbox
        self.clock = clock
        self.simulated = simulated
        self.collector_id = outbox.collector_id
        self._states = [
            _InstrumentState(b, build_source(b.profile, b.seed, b.faults)) for b in instruments
        ]
        self.stats = AcquisitionStats()
        self._origin_mono = 0.0
        self._origin_ms = 0
        self._last_wall: datetime | None = None
        self._last_mono: float | None = None
        self.sampling = False

    # --- record builders -----------------------------------------------------------------------

    def _event(
        self, binding: InstrumentBinding, etype: EventType, detail: dict[str, object], at: datetime
    ) -> None:
        def build(epoch: int, seq: int) -> Envelope:
            return Envelope(
                kind=RecordKind.EVENT,
                collector_id=self.collector_id,
                spool_epoch=epoch,
                seq=seq,
                instrument_id=binding.instrument_id,
                profile_revision_id=binding.profile_revision_id,
                capture_time=at,
                simulated=self.simulated,
                event_type=etype,
                detail=detail,
            )

        try:
            self.outbox.append(build)
            self.stats.events += 1
        except SpoolFull:
            log.error("spool full: could not record %s for %s", etype, binding.instrument_id)

    def _samples(
        self,
        st: _InstrumentState,
        reading: SourceReading,
        at: datetime,
        confidence: ClockConfidence,
    ) -> None:
        profile = st.source.profile
        frame_id = f"{st.binding.instrument_id}:{reading.t_ms}"
        builders: list[Callable[[int, int], Envelope]] = []
        for ch in reading.channels:
            spec = profile.channel(ch.key)
            value, quality, reason = ch.value, ch.quality, ch.quality_reason
            if (
                quality is Quality.GOOD
                and isinstance(value, float)
                and spec.valid_min is not None
                and spec.valid_max is not None
                and not spec.valid_min <= value <= spec.valid_max
            ):
                quality, reason = Quality.BAD, QualityReason.OVERRANGE
            if confidence is ClockConfidence.SUSPECT and quality is Quality.GOOD:
                quality, reason = Quality.UNCERTAIN, QualityReason.CLOCK_SUSPECT
            fields: dict[str, Any] = {
                "kind": RecordKind.SAMPLE,
                "collector_id": self.collector_id,
                "instrument_id": st.binding.instrument_id,
                "profile_revision_id": st.binding.profile_revision_id,
                "channel_id": st.binding.channels[ch.key],
                "frame_id": frame_id,
                "value": value,
                "raw": ch.raw,
                "source_status": ch.source_status,
                "quality": quality,
                "quality_reason": reason,
                "capture_time": at,
                "clock_confidence": confidence,
                "simulated": self.simulated,
                "source_time": reading.source_time,
                "source_time_meaning": profile.source_time_meaning,
            }
            builders.append(functools.partial(_build_envelope, fields))
        self.outbox.append_many(builders)  # the whole frame or nothing
        self.stats.samples += len(builders)

    def _run_transition(self, st: _InstrumentState, run: RunInfo | None, at: datetime) -> None:
        """Emit run boundaries. After a (re)start the current run is announced again; the
        server treats repeated starts of the same run as idempotent."""
        prev = st.run
        same = run is not None and prev is not None and prev.key == run.key
        if st.run_known and (same or (prev is None and run is None)):
            return
        if st.run_known and prev is not None and not same:
            self._event(
                st.binding,
                EventType.RUN_ENDED,
                {
                    "run_key": prev.key,
                    "end_reason": "completed",
                    "program_end": _utc_ms(prev.program_end_ms),
                },
                at,
            )
        if run is not None:
            self._event(
                st.binding,
                EventType.RUN_STARTED,
                {
                    "run_key": run.key,
                    "name": run.name,
                    "kind": run.kind,
                    "program_start": _utc_ms(run.program_start_ms),
                    "program_end": _utc_ms(run.program_end_ms),
                    "metadata": run.metadata,
                },
                at,
            )
        st.run, st.run_known = run, True

    # --- scheduling ----------------------------------------------------------------------------

    def _mono_of(self, t_ms: int) -> float:
        return self._origin_mono + (t_ms - self._origin_ms) / 1000.0

    def _nominal_now(self, mono: float) -> int:
        return self._origin_ms + int((mono - self._origin_mono) * 1000)

    def start(self) -> None:
        wall = self.clock.now()
        self._origin_ms = int(wall.timestamp() * 1000)
        self._origin_mono = self.clock.monotonic()
        self.sampling = True
        for st in self._states:
            cadence = max(1, st.source.cadence_ms(self._origin_ms))
            st.next_t_ms = (self._origin_ms // cadence + 1) * cadence  # next grid point
            self._event(
                st.binding,
                EventType.ACQUISITION_STARTED,
                {"cadence_ms": cadence, "first_sample": _utc_ms(st.next_t_ms)},
                wall,
            )
            if self.outbox.new_epoch_created:
                self._event(
                    st.binding, EventType.SPOOL_EPOCH_STARTED, {"epoch": self.outbox.epoch}, wall
                )
        self.outbox.new_epoch_created = False

    def stop(self) -> None:
        if self.sampling:
            at = self.clock.now()
            for st in self._states:
                self._event(st.binding, EventType.ACQUISITION_STOPPED, {}, at)
        self.sampling = False

    def seconds_until_due(self) -> float:
        due = min(self._mono_of(st.next_t_ms) for st in self._states)
        return due - self.clock.monotonic()

    def run_due(self) -> int:
        """Acquire every instrument whose next sample is due. Returns frames acquired."""
        mono = self.clock.monotonic()
        due = [st for st in self._states if self._mono_of(st.next_t_ms) <= mono]
        if not due:
            return 0
        wall = self.clock.now()
        confidence = self._check_clock(wall, mono)
        now_ms = self._nominal_now(mono)
        for st in due:
            cadence = max(1, st.source.cadence_ms(st.next_t_ms))
            if now_ms - st.next_t_ms >= cadence:
                # Do not burst: jump to the latest grid point whose time has come.
                latest = st.next_t_ms + ((now_ms - st.next_t_ms) // cadence) * cadence
                skipped = (latest - st.next_t_ms) // cadence
                self.stats.skipped_slots += skipped
                self._event(
                    st.binding,
                    EventType.SCHEDULE_SKIPPED,
                    {
                        "skipped_slots": skipped,
                        "from": _utc_ms(st.next_t_ms),
                        "to": _utc_ms(latest - cadence),
                    },
                    wall,
                )
                st.next_t_ms = latest
            t_ms = st.next_t_ms
            self._acquire(st, t_ms, wall, confidence)
            st.next_t_ms = t_ms + max(1, st.source.cadence_ms(t_ms))
        return len(due)

    def _check_clock(self, wall: datetime, mono: float) -> ClockConfidence:
        confidence = ClockConfidence.TRUSTED
        if self._last_wall is not None and self._last_mono is not None:
            drift = (wall - self._last_wall).total_seconds() - (mono - self._last_mono)
            if abs(drift) > CLOCK_JUMP_TOLERANCE_S:
                for st in self._states:
                    self._event(
                        st.binding,
                        EventType.CLOCK_JUMP,
                        {"step_s": round(drift, 3), "previous_wall": format_utc(self._last_wall)},
                        wall,
                    )
                confidence = ClockConfidence.SUSPECT
        self._last_wall, self._last_mono = wall, mono
        return confidence

    def _acquire(
        self, st: _InstrumentState, t_ms: int, wall: datetime, confidence: ClockConfidence
    ) -> None:
        iid = st.binding.instrument_id
        try:
            reading = st.source.read(t_ms)
        except SourceUnavailable as exc:
            if st.available:
                st.available, st.unavailable_since, st.reason = False, wall, str(exc)
                self._event(
                    st.binding,
                    EventType.SOURCE_UNAVAILABLE,
                    {"reason": str(exc)[:120], "at": _utc_ms(t_ms)},
                    wall,
                )
            self.stats.source_states[iid] = "unavailable"
            return
        if not st.available:
            self._event(
                st.binding,
                EventType.SOURCE_RECOVERED,
                {
                    "unavailable_since": format_utc(st.unavailable_since or wall),
                    "at": _utc_ms(t_ms),
                },
                wall,
            )
            st.available, st.unavailable_since, st.reason = True, None, None
        self._run_transition(st, reading.run, wall)
        try:
            self._samples(st, reading, wall, confidence)
        except SpoolFull:
            self.stats.spool_full_rejections += 1
            if st.spool_full_since is None:
                st.spool_full_since = wall
                log.error("spool full: acquisition impaired for %s", iid)
            self.stats.source_states[iid] = "spool_full"
            return
        self.stats.frames += 1
        if st.spool_full_since is not None:
            self._event(
                st.binding,
                EventType.ACQUISITION_LOST_INTERVAL,
                {
                    "start": format_utc(st.spool_full_since),
                    "end": format_utc(wall),
                    "reason": "spool_full",
                },
                wall,
            )
            st.spool_full_since = None
        self.stats.source_states[iid] = "ok"

    def run(self, stop: threading.Event, max_frames: int | None = None) -> None:
        self.start()
        try:
            while not stop.is_set():
                if max_frames is not None and self.stats.frames >= max_frames:
                    break
                wait = self.seconds_until_due()
                if wait > 0 and self.clock.sleep(wait, stop):
                    break
                self.run_due()
        finally:
            self.stop()

    def source_states(self) -> list[dict[str, str]]:
        return [
            {
                "instrument_id": st.binding.instrument_id,
                "state": self.stats.source_states.get(st.binding.instrument_id, "waiting"),
                **({"reason": st.reason} if st.reason else {}),
            }
            for st in self._states
        ]
