"""Scheduled acquisition into the outbox.

* Scheduling uses the monotonic clock. After a stall (sleep, suspend, slow disk) the loop skips to
  the next future slot and records a ``schedule_skipped`` event instead of firing a burst of
  missed polls.
* The source tick of a slot is fixed by the schedule, not by wall time, so a wall-clock step cannot
  duplicate or reorder ticks. Wall-clock steps are detected and recorded as ``clock_jump`` events;
  the first sample after a step is marked with suspect clock confidence.
* A source that does not answer produces ``source_unavailable`` / ``source_recovered`` events. The
  collector stays online and keeps scheduling.
* When the spool is full, measurements are not accepted; after recovery an
  ``acquisition_lost_interval`` event records the interval that could not be captured.
"""

from __future__ import annotations

import functools
import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
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
from labmon.profiles import PROFILES
from labmon.sim.clock import Clock
from labmon.sim.sources import Source, SourceReading, SourceUnavailable, build_source

log = logging.getLogger(__name__)

CLOCK_JUMP_TOLERANCE_S = 2.0


def _build_envelope(fields: dict[str, Any], epoch: int, seq: int) -> Envelope:
    return Envelope(spool_epoch=epoch, seq=seq, **fields)


@dataclass
class _InstrumentState:
    binding: InstrumentBinding
    source: Source
    available: bool = True
    unavailable_since: datetime | None = None
    reason: str | None = None
    spool_full_since: datetime | None = None


@dataclass
class AcquisitionStats:
    slots: int = 0
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
        cadence_s: float = 1.0,
        simulated: bool = True,
    ) -> None:
        self.outbox = outbox
        self.clock = clock
        self.cadence_s = cadence_s
        self.simulated = simulated
        self.collector_id = outbox.collector_id
        self._states = [
            _InstrumentState(b, build_source(b.profile, b.seed, b.faults)) for b in instruments
        ]
        self.stats = AcquisitionStats()
        self._origin_mono: float | None = None
        self._origin_tick = 0
        self._slot = 0
        self._last_wall: datetime | None = None
        self._last_mono: float | None = None
        self._pending_clock_suspect = False
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
        profile = PROFILES[st.binding.profile]
        frame_id = f"{st.binding.instrument_id}:{reading.tick}"
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
            build = functools.partial(_build_envelope, fields)
            builders.append(build)
        self.outbox.append_many(builders)  # the whole frame or nothing
        self.stats.samples += len(builders)

    # --- scheduling ----------------------------------------------------------------------------

    def start(self) -> None:
        """Align the first slot to the next whole cadence boundary of the wall clock."""
        wall = self.clock.now()
        ts = wall.timestamp()
        next_tick = math.floor(ts / self.cadence_s) + 1
        delay = next_tick * self.cadence_s - ts
        self._origin_mono = self.clock.monotonic() + delay
        self._origin_tick = next_tick
        self._slot = 0
        self.sampling = True
        for st in self._states:
            self._event(
                st.binding,
                EventType.ACQUISITION_STARTED,
                {"cadence_s": self.cadence_s, "first_tick": next_tick},
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
        assert self._origin_mono is not None
        due = self._origin_mono + self._slot * self.cadence_s
        return due - self.clock.monotonic()

    def run_due(self) -> int:
        """Run the current slot if it is due. Returns the number of slots executed (0 or 1)."""
        assert self._origin_mono is not None
        mono = self.clock.monotonic()
        due = self._origin_mono + self._slot * self.cadence_s
        if mono < due:
            return 0
        behind = int((mono - due) // self.cadence_s)
        wall = self.clock.now()
        if behind >= 1:
            # Do not burst: jump to the latest slot whose time has come and record the skip.
            skipped_from = self._origin_tick + self._slot
            self._slot += behind
            self.stats.skipped_slots += behind
            for st in self._states:
                self._event(
                    st.binding,
                    EventType.SCHEDULE_SKIPPED,
                    {
                        "skipped_slots": behind,
                        "from_tick": skipped_from,
                        "to_tick": self._origin_tick + self._slot - 1,
                    },
                    wall,
                )
        tick = self._origin_tick + self._slot
        confidence = self._check_clock(wall, mono)
        self._acquire(tick, wall, confidence)
        self._slot += 1
        self.stats.slots += 1
        return 1

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

    def _acquire(self, tick: int, wall: datetime, confidence: ClockConfidence) -> None:
        for st in self._states:
            iid = st.binding.instrument_id
            try:
                reading = st.source.read(tick)
            except SourceUnavailable as exc:
                if st.available:
                    st.available, st.unavailable_since, st.reason = False, wall, str(exc)
                    self._event(
                        st.binding,
                        EventType.SOURCE_UNAVAILABLE,
                        {"reason": str(exc)[:120], "tick": tick},
                        wall,
                    )
                self.stats.source_states[iid] = "unavailable"
                continue
            if not st.available:
                self._event(
                    st.binding,
                    EventType.SOURCE_RECOVERED,
                    {"unavailable_since": format_utc(st.unavailable_since or wall), "tick": tick},
                    wall,
                )
                st.available, st.unavailable_since, st.reason = True, None, None
            try:
                self._samples(st, reading, wall, confidence)
            except SpoolFull:
                self.stats.spool_full_rejections += 1
                if st.spool_full_since is None:
                    st.spool_full_since = wall
                    log.error("spool full: acquisition impaired for %s", iid)
                self.stats.source_states[iid] = "spool_full"
                continue
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

    def run(self, stop: threading.Event, max_slots: int | None = None) -> None:
        self.start()
        executed = 0
        try:
            while not stop.is_set():
                if max_slots is not None and executed >= max_slots:
                    break
                wait = self.seconds_until_due()
                if wait > 0 and self.clock.sleep(wait, stop):
                    break
                executed += self.run_due()
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
