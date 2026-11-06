"""Simulated heat-flux DSC measuring an indium-like reference (opt-in fourth profile).

Sign convention: **exo up** — positive heat flow is exothermic; endothermic peaks point down.

One 2520 s cycle from the shared anchor (10 mg sample, 10 K/min):

=============  ======  ===================================================================
Segment        Length  Behaviour
=============  ======  ===================================================================
Isothermal      120 s  30 °C, heat flow 0 mW
Heating        1020 s  30 → 200 °C; baseline −0.60 mW − 0.004 mW/K·(T−30), melting
                       endotherm: onset at 156.6 °C sample temperature, peak −26.0 mW about
                       12 s later (≈ 158.6 °C), area −286 mJ (28.6 J/g)
Isothermal      120 s  200 °C
Cooling        1020 s  200 → 30 °C; baseline mirrored; crystallisation exotherm after 2.6 K
                       supercooling (154.0 °C), peak +30.1 mW, area +286 mJ
Isothermal      240 s  30 °C
=============  ======  ===================================================================

Sample temperature lags the programmed temperature by rate × 6 s (1 K at 10 K/min). Baseline
transitions are smoothed with a 20 s time constant; a slow drift of 0.02 mW per cycle-length is
added. Each cycle is reported as one run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from labmon.profiles import DSC
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
class DscProgram:
    iso1_s: float = 120.0
    heat_s: float = 1020.0
    iso2_s: float = 120.0
    cool_s: float = 1020.0
    iso3_s: float = 240.0
    low_c: float = 30.0
    high_c: float = 200.0
    lag_s: float = 6.0
    base_mw: float = 0.60
    cp_slope_mw_per_k: float = 0.004
    baseline_tau_s: float = 20.0
    melt_onset_c: float = 156.6
    melt_rise_s: float = 12.0
    melt_decay_s: float = 5.0
    freeze_c: float = 154.0
    freeze_rise_s: float = 3.0
    freeze_decay_s: float = 8.0
    enthalpy_mj: float = 286.0
    drift_mw: float = 0.02

    @property
    def cycle_s(self) -> float:
        return self.iso1_s + self.heat_s + self.iso2_s + self.cool_s + self.iso3_s

    @property
    def rate_k_per_s(self) -> float:
        return (self.high_c - self.low_c) / self.heat_s

    @property
    def melt_peak_mw(self) -> float:
        return self.enthalpy_mj / (self.melt_rise_s / 2 + self.melt_decay_s)

    @property
    def freeze_peak_mw(self) -> float:
        return self.enthalpy_mj / (self.freeze_rise_s / 2 + self.freeze_decay_s)

    def locate(self, t_ms: int) -> tuple[int, float]:
        n, pos = divmod(t_ms - ANCHOR_MS, round(self.cycle_s * 1000))
        return n, pos / 1000

    @staticmethod
    def _peak(dt: float, rise: float, decay: float, height: float) -> float:
        if dt < 0:
            return 0.0
        if dt < rise:
            return height * dt / rise
        return height * math.exp(-(dt - rise) / decay)

    def state(self, t_ms: int) -> tuple[str, float, float, float]:
        """Return (segment, programmed °C, sample °C, heat flow mW) without noise."""
        _, t = self.locate(t_ms)
        r = self.rate_k_per_s
        lag_k = r * self.lag_s
        drift = self.drift_mw * t / self.cycle_s
        b = self.base_mw
        if t < self.iso1_s:
            # baseline relaxes from the previous cooling offset
            hf = b * math.exp(-(t + self.iso3_s) / self.baseline_tau_s)
            return "isothermal", self.low_c, self.low_c, hf + drift
        t -= self.iso1_s
        if t < self.heat_s:
            prog = self.low_c + r * t
            sample = prog - lag_k * (1 - math.exp(-t / self.lag_s))
            base = -(b + self.cp_slope_mw_per_k * (sample - self.low_c))
            base *= 1 - math.exp(-t / self.baseline_tau_s)
            t_onset = (self.melt_onset_c - self.low_c + lag_k) / r
            hf = base - self._peak(
                t - t_onset, self.melt_rise_s, self.melt_decay_s, self.melt_peak_mw
            )
            return "heating", prog, sample, hf + drift
        t -= self.heat_s
        end_base = -(b + self.cp_slope_mw_per_k * (self.high_c - lag_k - self.low_c))
        if t < self.iso2_s:
            sample = self.high_c - lag_k * math.exp(-t / self.lag_s)
            return (
                "isothermal",
                self.high_c,
                sample,
                end_base * math.exp(-t / self.baseline_tau_s) + drift,
            )
        t -= self.iso2_s
        if t < self.cool_s:
            prog = self.high_c - r * t
            sample = prog + lag_k * (1 - math.exp(-t / self.lag_s))
            base = b + self.cp_slope_mw_per_k * (sample - self.low_c)
            base *= 1 - math.exp(-t / self.baseline_tau_s)
            t_freeze = (self.high_c - self.freeze_c + lag_k) / r
            hf = base + self._peak(
                t - t_freeze, self.freeze_rise_s, self.freeze_decay_s, self.freeze_peak_mw
            )
            return "cooling", prog, sample, hf + drift
        t -= self.cool_s
        sample = self.low_c + lag_k * math.exp(-t / self.lag_s)
        hf = (b + self.cp_slope_mw_per_k * lag_k) * math.exp(-t / self.baseline_tau_s)
        return "isothermal", self.low_c, sample, hf + drift


@dataclass
class DscSource:
    seed: int
    faults: tuple[Fault, ...] = ()
    program: DscProgram = DscProgram()

    @property
    def profile(self) -> ProfileSpec:
        return DSC

    def cadence_ms(self, t_ms: int) -> int:
        return 1000

    def run_at(self, t_ms: int) -> RunInfo:
        p = self.program
        n, _ = p.locate(t_ms)
        start = ANCHOR_MS + round(n * p.cycle_s * 1000)
        return RunInfo(
            key=f"c{n:06d}",
            name=f"DSC heat-cool H-{n:05d}",
            kind="dsc_scan",
            program_start_ms=start,
            program_end_ms=start + round(p.cycle_s * 1000),
            metadata={
                "sample_id": f"H-{n:05d}",
                "material": "indium-like reference (simulated)",
                "sample_mass_mg": 10.0,
                "rate_k_per_min": p.rate_k_per_s * 60,
                "sign_convention": "exo_up",
                "expected": {
                    "melt_onset_c": p.melt_onset_c,
                    "enthalpy_j_per_g": p.enthalpy_mj / 10.0,
                },
            },
        )

    def read(self, t_ms: int) -> SourceReading:
        if any(f.kind is FaultKind.DROPOUT for f in active_faults(self.faults, t_ms)):
            raise SourceUnavailable("no response from DSC cell")
        _, prog, sample, hf = self.program.state(t_ms)
        sample = round(sample + noise(self.seed, t_ms, "ts", 0.01), 3)
        hf = round(hf + noise(self.seed, t_ms, "hf", 0.01), 4)
        return SourceReading(
            t_ms=t_ms,
            channels=(
                ChannelReading("sample_t", sample, raw=f"{sample:.3f}"),
                ChannelReading("program_t", round(prog, 3), raw=f"{prog:.3f}"),
                ChannelReading("heat_flow", hf, raw=f"{hf:.4f}"),
            ),
            run=self.run_at(t_ms),
        )
