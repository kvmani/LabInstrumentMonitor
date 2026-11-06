"""Simulated heat-treatment furnace: ramp, soak with a brief overshoot, cooldown, idle.

Default program (one 6600 s cycle from the shared anchor): idle 300 s at ambient, ramp to 850 °C
in 1800 s, soak 1800 s (overshoot peaks at about 857.1 °C 232 s into the soak), free cooling
2700 s. The process value lags the setpoint through a first-order response and is continuous
across phase boundaries and cycles. Each ramp-soak-cool sequence is reported as one run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from labmon.contracts.envelope import QualityReason
from labmon.profiles import FURNACE
from labmon.profiles.base import ProfileSpec
from labmon.sim.base import (
    ANCHOR_MS,
    ChannelReading,
    Fault,
    FaultKind,
    RunInfo,
    SourceReading,
    SourceUnavailable,
    active_faults,
    noise,
)


@dataclass(frozen=True)
class FurnaceProgram:
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

    def position(self, t_s: float) -> float:
        return (t_s - ANCHOR_MS / 1000) % self.cycle_s

    def cycle(self, t_s: float) -> int:
        return int((t_s - ANCHOR_MS / 1000) // self.cycle_s)

    def _rate(self) -> float:
        return (self.soak_c - self.ambient_c) / self.ramp_s

    def _lag_at_ramp_end(self) -> float:
        return self._rate() * self.lag_tau_s * (1.0 - math.exp(-self.ramp_s / self.lag_tau_s))

    def _soak_pv(self, t: float) -> float:
        x = t / self.overshoot_peak_s
        bump = self.overshoot_c * x * math.exp(1.0 - x)
        return self.soak_c - self._lag_at_ramp_end() * math.exp(-t / self.lag_tau_s) + bump

    def _residual(self, t_since_soak_end: float) -> float:
        """Excess over ambient while cooling freely after the soak (continuous across cycles)."""
        return (self._soak_pv(self.soak_s) - self.ambient_c) * math.exp(
            -t_since_soak_end / self.cool_tau_s
        )

    def state(self, t_s: float) -> tuple[int, float, float]:
        """Return (phase, setpoint, noiseless process value) at nominal time ``t_s``."""
        t = self.position(t_s)
        if t < self.idle_s:
            return 0, self.ambient_c, self.ambient_c + self._residual(self.cool_s + t)
        t -= self.idle_s
        if t < self.ramp_s:
            sp = self.ambient_c + self._rate() * t
            pv = sp - self._rate() * self.lag_tau_s * (1.0 - math.exp(-t / self.lag_tau_s))
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
    faults: tuple[Fault, ...] = ()
    program: FurnaceProgram = field(default_factory=FurnaceProgram)

    @property
    def profile(self) -> ProfileSpec:
        return FURNACE

    def cadence_ms(self, t_ms: int) -> int:
        return 1000

    def run_at(self, t_ms: int) -> RunInfo | None:
        p = self.program
        t_s = t_ms / 1000
        if p.position(t_s) < p.idle_s:
            return None
        n = p.cycle(t_s)
        start = ANCHOR_MS + (n * p.cycle_s + p.idle_s) * 1000
        return RunInfo(
            key=f"c{n:06d}",
            name=f"Heat treatment #{n}",
            kind="heat_treatment",
            program_start_ms=start,
            program_end_ms=ANCHOR_MS + (n + 1) * p.cycle_s * 1000,
            metadata={
                "recipe": f"ramp to {p.soak_c:.0f} °C, soak {p.soak_s // 60} min",
                "soak_c": p.soak_c,
                "tolerance_c": FURNACE.tolerance_c,
            },
        )

    def read(self, t_ms: int) -> SourceReading:
        active = active_faults(self.faults, t_ms)
        if any(f.kind is FaultKind.DROPOUT for f in active):
            raise SourceUnavailable("no response from controller")
        phase, sp, pv = self.program.state(t_ms / 1000)
        pv = round(pv + noise(self.seed, t_ms, "pv", self.program.noise_c), 2)
        pv_reading = ChannelReading("pv", pv, raw=f"{pv:.2f}")
        for f in active:
            if f.kind is FaultKind.SPIKE:
                spiked = round(pv + f.magnitude, 2)
                pv_reading = ChannelReading("pv", spiked, raw=f"{spiked:.2f}")
            elif f.kind is FaultKind.OPEN_PROBE:
                pv_reading = ChannelReading.bad(
                    "pv", QualityReason.SENSOR_OPEN, "OPEN", "open_thermocouple"
                )
            elif f.kind is FaultKind.OVERRANGE:
                hi = FURNACE.channel("pv").valid_max or 0.0
                pv_reading = ChannelReading.bad(
                    "pv", QualityReason.OVERRANGE, f">{hi:.0f}", "overrange"
                )
        return SourceReading(
            t_ms=t_ms,
            channels=(
                pv_reading,
                ChannelReading("sp", round(sp, 2), raw=f"{sp:.2f}"),
                ChannelReading("phase", phase, raw=str(phase)),
            ),
            run=self.run_at(t_ms),
        )
