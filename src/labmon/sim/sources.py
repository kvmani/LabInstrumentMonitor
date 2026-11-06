"""Deterministic synthetic sources behind the adapter boundary.

Every reading is a pure function of ``(seed, tick)`` where ``tick`` is the nominal sample index
(seconds since the Unix epoch at 1 Hz). Restarting a collector or replaying a window therefore
produces the same values, which gives known-answer tests exact expectations.

Faults are scripted by tick interval, never random, so failure tests are reproducible.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from labmon.contracts.envelope import Quality, QualityReason
from labmon.profiles import AMBIENT, FURNACE
from labmon.profiles.base import ProfileSpec


class SourceUnavailable(Exception):
    """The source did not answer (timeout, unplugged link, busy session)."""


@dataclass(frozen=True)
class ChannelReading:
    key: str
    value: float | int | bool | None
    quality: Quality = Quality.GOOD
    quality_reason: QualityReason | None = None
    raw: str | None = None
    source_status: str | None = None


@dataclass(frozen=True)
class SourceReading:
    tick: int
    channels: tuple[ChannelReading, ...]
    source_time: datetime | None = None


class FaultKind(StrEnum):
    DROPOUT = "dropout"  # source does not answer; collector stays online
    OPEN_PROBE = "open_probe"  # process temperature reads open-circuit (bad quality)
    SPIKE = "spike"  # one-sample excursion (+magnitude °C) on process temperature
    OVERRANGE = "overrange"  # process temperature beyond the profile's valid range


@dataclass(frozen=True)
class Fault:
    kind: FaultKind
    start_tick: int
    end_tick: int  # exclusive
    magnitude: float = 0.0

    def active(self, tick: int) -> bool:
        return self.start_tick <= tick < self.end_tick


def _rng(seed: int, tick: int, stream: str) -> random.Random:
    # String seeds are hashed with SHA-512 by CPython: stable across processes and platforms.
    return random.Random(f"{seed}:{stream}:{tick}")


@dataclass(frozen=True)
class FurnaceProgram:
    """Ramp, soak (with a brief overshoot) and cooldown, repeating from ``anchor``."""

    anchor: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    ambient_c: float = 25.0
    soak_c: float = 850.0
    idle_s: int = 300
    ramp_s: int = 1800
    soak_s: int = 1800
    cool_s: int = 2700
    lag_tau_s: float = 60.0
    cool_tau_s: float = 600.0
    overshoot_c: float = 8.0
    overshoot_peak_s: float = 180.0
    noise_c: float = 0.3

    @property
    def cycle_s(self) -> int:
        return self.idle_s + self.ramp_s + self.soak_s + self.cool_s

    def position(self, tick: int) -> int:
        return (tick - int(self.anchor.timestamp())) % self.cycle_s

    def _lag_at_ramp_end(self) -> float:
        rate = (self.soak_c - self.ambient_c) / self.ramp_s
        return rate * self.lag_tau_s * (1.0 - math.exp(-self.ramp_s / self.lag_tau_s))

    def _soak_pv(self, t: float) -> float:
        x = t / self.overshoot_peak_s
        bump = self.overshoot_c * x * math.exp(1.0 - x)
        return self.soak_c - self._lag_at_ramp_end() * math.exp(-t / self.lag_tau_s) + bump

    def _residual(self, t_since_soak_end: float) -> float:
        """Excess over ambient while cooling freely after the soak (continuous across cycles)."""
        return (self._soak_pv(self.soak_s) - self.ambient_c) * math.exp(
            -t_since_soak_end / self.cool_tau_s
        )

    def state(self, tick: int) -> tuple[int, float, float]:
        """Return (phase, setpoint, noiseless process value) at a nominal tick."""
        t = self.position(tick)
        rate = (self.soak_c - self.ambient_c) / self.ramp_s
        if t < self.idle_s:
            return 0, self.ambient_c, self.ambient_c + self._residual(self.cool_s + t)
        t -= self.idle_s
        if t < self.ramp_s:
            sp = self.ambient_c + rate * t
            pv = sp - rate * self.lag_tau_s * (1.0 - math.exp(-t / self.lag_tau_s))
            pv += self._residual(self.cool_s + self.idle_s) * math.exp(-t / self.lag_tau_s)
            return 1, sp, pv
        t -= self.ramp_s
        if t < self.soak_s:
            return 2, self.soak_c, self._soak_pv(t)
        t -= self.soak_s
        return 3, self.ambient_c, self.ambient_c + self._residual(t)


@dataclass
class FurnaceSource:
    seed: int
    program: FurnaceProgram = field(default_factory=FurnaceProgram)
    faults: tuple[Fault, ...] = ()
    profile: ProfileSpec = FURNACE

    def read(self, tick: int) -> SourceReading:
        active = [f for f in self.faults if f.active(tick)]
        if any(f.kind is FaultKind.DROPOUT for f in active):
            raise SourceUnavailable("no response from controller")
        phase, sp, pv = self.program.state(tick)
        pv = round(pv + _rng(self.seed, tick, "pv").gauss(0.0, self.program.noise_c), 2)
        pv_reading = ChannelReading("pv", pv, raw=f"{pv:.2f}")
        for f in active:
            if f.kind is FaultKind.SPIKE:
                spiked = round(pv + f.magnitude, 2)
                pv_reading = ChannelReading("pv", spiked, raw=f"{spiked:.2f}")
            elif f.kind is FaultKind.OPEN_PROBE:
                pv_reading = ChannelReading(
                    "pv",
                    None,
                    Quality.BAD,
                    QualityReason.SENSOR_OPEN,
                    raw="OPEN",
                    source_status="open_thermocouple",
                )
            elif f.kind is FaultKind.OVERRANGE:
                hi = self.profile.channel("pv").valid_max or 0.0
                pv_reading = ChannelReading(
                    "pv",
                    None,
                    Quality.BAD,
                    QualityReason.OVERRANGE,
                    raw=f">{hi:.0f}",
                    source_status="overrange",
                )
        return SourceReading(
            tick=tick,
            channels=(
                pv_reading,
                ChannelReading("sp", round(sp, 2), raw=f"{sp:.2f}"),
                ChannelReading("phase", phase, raw=str(phase)),
            ),
        )


@dataclass
class AmbientSource:
    """Near-constant room temperature with bounded noise. Automated smoke fixture."""

    seed: int
    base_c: float = 23.0
    faults: tuple[Fault, ...] = ()
    profile: ProfileSpec = AMBIENT

    def read(self, tick: int) -> SourceReading:
        if any(f.kind is FaultKind.DROPOUT and f.active(tick) for f in self.faults):
            raise SourceUnavailable("no response from logger")
        noise = max(-0.15, min(0.15, _rng(self.seed, tick, "temp").gauss(0.0, 0.05)))
        value = round(self.base_c + 0.3 * math.sin(2 * math.pi * tick / 3600.0) + noise, 3)
        return SourceReading(tick=tick, channels=(ChannelReading("temp", value),))


Source = FurnaceSource | AmbientSource


def build_source(profile_key: str, seed: int, faults: tuple[Fault, ...] = ()) -> Source:
    if profile_key == FURNACE.key:
        return FurnaceSource(seed=seed, faults=faults)
    if profile_key == AMBIENT.key:
        return AmbientSource(seed=seed, faults=faults)
    raise ValueError(f"no simulator for profile {profile_key!r}")
