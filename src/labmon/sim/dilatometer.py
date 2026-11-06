"""Fictional vacuum dilatometer: pump-down, thermal ramp, hold, cooldown, vent.

Specimen: simulated low-alloy steel, L0 = 25 mm, mean expansion coefficient 14e-6 /K
(0.35 µm/K). One 3630 s cycle from the shared anchor:

==============  ======  ===================================================================
Phase           Length  Behaviour
==============  ======  ===================================================================
Idle / vented    120 s  1.0e5 Pa, 25 °C, new specimen so ΔL = 0
Pump-down        600 s  P = Pbase + 1e5·exp(-t/45 s) (monotonic, 1e5 Pa → ~1e-3 Pa)
Heating         1050 s  25 → 900 °C at 50 K/min; ferrite→austenite contraction of 45 µm
                        centred at 740 °C; gauge error ("ERR") at +120 s for 15 s
Hold             600 s  900 °C; outgassing burst +0.3 Pa peaking at +215 s
Cooling         1200 s  exponential (τ = 300 s); reverse transformation near 560 °C restores
                        the 45 µm and leaves a permanent −15 µm (transformation plasticity)
Vent              60 s  pressure returns to atmosphere (τ = 8 s)
==============  ======  ===================================================================

The length change is signed: it is negative at the end of every cycle (≈ −9.5 µm at 41 °C).
Pressure is strictly positive (log display allowed); gauge errors are bad-quality readings, not
zeros. Each cycle from pump-down to the end of venting is reported as one run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from labmon.contracts.envelope import QualityReason
from labmon.profiles import DILATOMETER
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
    logistic,
    noise,
)

ATM_PA = 1.0e5


@dataclass(frozen=True)
class DilatometerProgram:
    idle_s: float = 120.0
    pump_s: float = 600.0
    heat_s: float = 1050.0
    hold_s: float = 600.0
    cool_s: float = 1200.0
    vent_s: float = 60.0
    start_c: float = 25.0
    hold_c: float = 900.0
    cool_tau_s: float = 300.0
    pump_tau_s: float = 45.0
    base_pa: float = 2.0e-4
    expansion_um_per_k: float = 0.35
    transformation_um: float = 45.0
    heat_transform_c: float = 740.0
    cool_transform_c: float = 560.0
    permanent_um: float = -15.0
    gauge_fault_after_heat_start_s: float = 120.0
    gauge_fault_s: float = 15.0
    burst_after_hold_start_s: float = 215.0
    burst_pa: float = 0.3

    @property
    def cycle_s(self) -> float:
        return self.idle_s + self.pump_s + self.heat_s + self.hold_s + self.cool_s + self.vent_s

    def locate(self, t_ms: int) -> tuple[int, float]:
        n, pos = divmod(t_ms - ANCHOR_MS, round(self.cycle_s * 1000))
        return n, pos / 1000

    def _cool_t(self, t: float) -> float:
        return self.start_c + (self.hold_c - self.start_c) * math.exp(-t / self.cool_tau_s)

    def _base(self, temp_c: float) -> float:
        return self.base_pa * (1.0 + (temp_c - self.start_c) / 200.0)

    def _pumped(self, t: float, temp_c: float) -> float:
        return self._base(temp_c) + ATM_PA * math.exp(-t / self.pump_tau_s)

    def state(self, t_ms: int) -> tuple[str, float, float, float]:
        """Return (phase, pressure Pa, temperature °C, ΔL µm) without noise."""
        _, t = self.locate(t_ms)
        s0 = self.start_c
        if t < self.idle_s:
            return "idle", ATM_PA, s0, 0.0
        t -= self.idle_s
        since_pump = t
        if t < self.pump_s:
            return "pump_down", self._pumped(since_pump, s0), s0, 0.0
        t -= self.pump_s
        if t < self.heat_s:
            temp = s0 + (self.hold_c - s0) * t / self.heat_s
            f = logistic((temp - self.heat_transform_c) / 10.0)
            dl = self.expansion_um_per_k * (temp - s0) - self.transformation_um * f
            return "heating", self._pumped(since_pump, temp), temp, dl
        t -= self.heat_s
        if t < self.hold_s:
            temp = self.hold_c
            burst = self.burst_pa * math.exp(-(((t - self.burst_after_hold_start_s) / 8.0) ** 2))
            dl = self.expansion_um_per_k * (temp - s0) - self.transformation_um
            return "hold", self._pumped(since_pump, temp) + burst, temp, dl
        t -= self.hold_s
        if t < self.cool_s + self.vent_s:
            temp = self._cool_t(t)
            g = logistic((self.cool_transform_c - temp) / 12.0)
            dl = (
                self.expansion_um_per_k * (temp - s0)
                - self.transformation_um * (1 - g)
                + self.permanent_um * g
            )
            pressure = self._pumped(since_pump, temp)
            if t >= self.cool_s:
                tv = t - self.cool_s
                pressure = ATM_PA - (ATM_PA - pressure) * math.exp(-tv / 8.0)
                return "vent", pressure, temp, dl
            return "cooling", pressure, temp, dl
        return "idle", ATM_PA, s0, 0.0  # pragma: no cover - rounding guard

    def gauge_fault(self, t_ms: int) -> bool:
        _, t = self.locate(t_ms)
        start = self.idle_s + self.pump_s + self.gauge_fault_after_heat_start_s
        return start <= t < start + self.gauge_fault_s


@dataclass
class DilatometerSource:
    seed: int
    faults: tuple[Fault, ...] = ()
    program: DilatometerProgram = DilatometerProgram()

    @property
    def profile(self) -> ProfileSpec:
        return DILATOMETER

    def cadence_ms(self, t_ms: int) -> int:
        return 1000

    def run_at(self, t_ms: int) -> RunInfo | None:
        p = self.program
        n, t = p.locate(t_ms)
        if t < p.idle_s:
            return None
        cycle_start = ANCHOR_MS + round(n * p.cycle_s * 1000)
        return RunInfo(
            key=f"c{n:06d}",
            name=f"Dilatometry D-{n:05d}",
            kind="dilatometry",
            program_start_ms=cycle_start + round(p.idle_s * 1000),
            program_end_ms=cycle_start + round(p.cycle_s * 1000),
            metadata={
                "specimen_id": f"D-{n:05d}",
                "material": "simulated low-alloy steel",
                "initial_length_mm": 25.0,
                "recipe": f"vacuum, {p.start_c:.0f}→{p.hold_c:.0f} °C at 50 K/min, "
                f"hold {p.hold_s / 60:.0f} min",
            },
        )

    def read(self, t_ms: int) -> SourceReading:
        active = active_faults(self.faults, t_ms)
        if any(f.kind is FaultKind.DROPOUT for f in active):
            raise SourceUnavailable("no response from dilatometer controller")
        _, pressure, temp, dl = self.program.state(t_ms)
        pressure *= math.exp(noise(self.seed, t_ms, "p", 0.02))
        pressure = float(f"{pressure:.4g}")
        temp = round(temp + noise(self.seed, t_ms, "t", 0.15), 2)
        dl = round(dl + noise(self.seed, t_ms, "dl", 0.05), 3)
        p_reading = ChannelReading("pressure", pressure, raw=f"{pressure:.4E}")
        if self.program.gauge_fault(t_ms) or any(f.kind is FaultKind.GAUGE_FAULT for f in active):
            p_reading = ChannelReading.bad(
                "pressure", QualityReason.VENDOR_STATUS_FAULT, "ERR", "gauge_error"
            )
        return SourceReading(
            t_ms=t_ms,
            channels=(
                p_reading,
                ChannelReading("temperature", temp, raw=f"{temp:.2f}"),
                ChannelReading("dl", dl, raw=f"{dl:.3f}"),
            ),
            run=self.run_at(t_ms),
        )
