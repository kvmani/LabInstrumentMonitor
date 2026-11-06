"""Near-constant room temperature with bounded noise. Automated smoke fixture only."""

from __future__ import annotations

import math
from dataclasses import dataclass

from labmon.profiles import AMBIENT
from labmon.profiles.base import ProfileSpec
from labmon.sim.base import (
    ChannelReading,
    Fault,
    FaultKind,
    SourceReading,
    SourceUnavailable,
    active_faults,
    noise,
)


@dataclass
class AmbientSource:
    seed: int
    faults: tuple[Fault, ...] = ()
    base_c: float = 23.0

    @property
    def profile(self) -> ProfileSpec:
        return AMBIENT

    def cadence_ms(self, t_ms: int) -> int:
        return 1000

    def read(self, t_ms: int) -> SourceReading:
        if any(f.kind is FaultKind.DROPOUT for f in active_faults(self.faults, t_ms)):
            raise SourceUnavailable("no response from logger")
        t_s = t_ms / 1000
        value = self.base_c + 0.3 * math.sin(2 * math.pi * t_s / 3600.0)
        value = round(value + noise(self.seed, t_ms, "temp", 0.05, clip=3.0), 3)
        return SourceReading(t_ms=t_ms, channels=(ChannelReading("temp", value),))
