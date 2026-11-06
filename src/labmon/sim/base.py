"""Simulator building blocks shared by every synthetic source.

Contract (the same boundary a future hardware adapter implements):

* ``read(t_ms)`` returns one acquisition frame for the nominal sample time ``t_ms``
  (milliseconds since the Unix epoch). Values are a pure function of ``(seed, t_ms)``, so a
  restart or a replay reproduces them exactly and known-answer tests have exact expectations.
* ``cadence_ms(t_ms)`` is the sampling interval the source wants *after* ``t_ms``
  (for example 5 Hz while a tensile test runs and 1 Hz while idle).
* ``SourceUnavailable`` means the source did not answer; the collector stays online.
* Faults are scripted by time interval, never random.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from labmon.contracts.envelope import Quality, QualityReason
from labmon.profiles.base import ProfileSpec

ANCHOR = datetime(2026, 1, 1, tzinfo=UTC)
ANCHOR_MS = int(ANCHOR.timestamp() * 1000)


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

    @staticmethod
    def bad(key: str, reason: QualityReason, raw: str, status: str) -> ChannelReading:
        return ChannelReading(key, None, Quality.BAD, reason, raw=raw, source_status=status)


@dataclass(frozen=True)
class RunInfo:
    """A run the (simulated) instrument program reports, e.g. one tensile test."""

    key: str  # unique per repetition, e.g. "c000123"
    name: str
    kind: str
    program_start_ms: int
    program_end_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceReading:
    t_ms: int
    channels: tuple[ChannelReading, ...]
    run: RunInfo | None = None
    source_time: datetime | None = None


class FaultKind(StrEnum):
    DROPOUT = "dropout"  # source does not answer; collector stays online
    OPEN_PROBE = "open_probe"  # primary temperature reads open-circuit (bad quality)
    SPIKE = "spike"  # one-sample excursion (+magnitude) on the primary measurement
    OVERRANGE = "overrange"  # primary measurement beyond the profile's valid range
    EXTENSOMETER_SLIP = "extensometer_slip"  # tensile extension invalid (missing pair)
    GAUGE_FAULT = "gauge_fault"  # vacuum gauge reports an error


@dataclass(frozen=True)
class Fault:
    """Active for nominal times in ``[start_s, end_s)`` (Unix seconds)."""

    kind: FaultKind
    start_s: float
    end_s: float
    magnitude: float = 0.0

    def active(self, t_ms: int) -> bool:
        return self.start_s * 1000 <= t_ms < self.end_s * 1000


class Source(Protocol):
    seed: int
    faults: tuple[Fault, ...]

    @property
    def profile(self) -> ProfileSpec: ...

    def read(self, t_ms: int) -> SourceReading: ...

    def cadence_ms(self, t_ms: int) -> int: ...


def rng(seed: int, t_ms: int, stream: str) -> random.Random:
    # String seeds are hashed with SHA-512 by CPython: stable across processes and platforms.
    return random.Random(f"{seed}:{stream}:{t_ms}")


def noise(seed: int, t_ms: int, stream: str, sigma: float, clip: float = 4.0) -> float:
    """Gaussian noise bounded to ``clip`` standard deviations."""
    v = rng(seed, t_ms, stream).gauss(0.0, sigma)
    return max(-clip * sigma, min(clip * sigma, v))


def active_faults(faults: tuple[Fault, ...], t_ms: int) -> list[Fault]:
    return [f for f in faults if f.active(t_ms)]


def cycle_position(t_ms: int, cycle_ms: int) -> tuple[int, int]:
    """Return (cycle index, position in ms) relative to the shared anchor."""
    return divmod(t_ms - ANCHOR_MS, cycle_ms)


def logistic(x: float) -> float:
    if x < -40:
        return 0.0
    if x > 40:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def fmt(value: float, digits: int) -> str:
    return f"{value:.{digits}f}"
