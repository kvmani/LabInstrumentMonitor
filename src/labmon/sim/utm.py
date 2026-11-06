"""Simulated universal tensile testing machine (UTM) running repeated tensile tests.

Specimen (simulated structural steel, round bar): diameter 6.00 mm (A0 = 28.274 mm²),
extensometer gauge length L0 = 25.00 mm. Each 405 s cycle from the shared anchor:

==========  ======  =========================================================================
State       Length  Behaviour
==========  ======  =========================================================================
Idle (0)    240 s   force and extension near zero (specimen exchange), sampled at 1 Hz
Preload (1)   5 s   elastic loading to 0.2 kN, 5 Hz
Testing (2) 140 s   constant strain rate 2.0e-3 /s: elastic (E = 200 GPa) → yield at 350 MPa
                    → work hardening to UTS 480 MPa at 18 % strain → necking → fracture at
                    28 % strain (390 MPa), 5 Hz
Fractured(3) ~20 s  force ≈ 0, extension holds the plastic elongation, 5 Hz
==========  ======  =========================================================================

Known answers (noise-free): maximum engineering stress 480.0 MPa = 13.572 kN at strain 0.18
(extension 4.500 mm, test time 89.9825 s); fracture after the maximum at strain 0.28 (t = 140 s).
Engineering stress = force / A0 and strain = extension / L0, computed by the server from the
geometry reported with each run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from labmon.contracts.envelope import QualityReason
from labmon.profiles import UTM
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
class TensileSpecimen:
    diameter_mm: float = 6.0
    gauge_length_mm: float = 25.0
    modulus_mpa: float = 200_000.0
    yield_mpa: float = 350.0
    uts_mpa: float = 480.0
    uniform_strain: float = 0.18
    fracture_strain: float = 0.28
    fracture_mpa: float = 390.0
    hardening_exponent: float = 2.5

    @property
    def area_mm2(self) -> float:
        return math.pi * self.diameter_mm**2 / 4.0

    @property
    def yield_strain(self) -> float:
        return self.yield_mpa / self.modulus_mpa

    def stress(self, strain: float) -> float:
        """Engineering stress (MPa) for engineering strain, up to fracture."""
        if strain <= self.yield_strain:
            return self.modulus_mpa * strain
        if strain <= self.uniform_strain:
            x = (strain - self.yield_strain) / (self.uniform_strain - self.yield_strain)
            return self.yield_mpa + (self.uts_mpa - self.yield_mpa) * (
                1.0 - (1.0 - x) ** self.hardening_exponent
            )
        x = (strain - self.uniform_strain) / (self.fracture_strain - self.uniform_strain)
        return self.uts_mpa - (self.uts_mpa - self.fracture_mpa) * x**2


@dataclass(frozen=True)
class TensileProgram:
    idle_s: float = 240.0
    preload_s: float = 5.0
    preload_kn: float = 0.2
    strain_rate: float = 2.0e-3
    cycle_s: float = 405.0  # whole seconds keep the 1 Hz and 5 Hz sample grids aligned
    specimen: TensileSpecimen = field(default_factory=TensileSpecimen)

    @property
    def preload_strain(self) -> float:
        return self.preload_kn * 1000 / self.specimen.area_mm2 / self.specimen.modulus_mpa

    @property
    def test_s(self) -> float:
        return (self.specimen.fracture_strain - self.preload_strain) / self.strain_rate

    @property
    def post_s(self) -> float:
        return self.cycle_s - self.idle_s - self.preload_s - self.test_s

    def locate(self, t_ms: int) -> tuple[int, float]:
        cycle_ms = round(self.cycle_s * 1000)
        n, pos = divmod(t_ms - ANCHOR_MS, cycle_ms)
        return n, pos / 1000

    def state(self, t_ms: int) -> tuple[int, float, float]:
        """Return (state code, force kN, extension mm) without noise."""
        sp = self.specimen
        _, t = self.locate(t_ms)
        if t < self.idle_s:
            return 0, 0.0, 0.0
        t -= self.idle_s
        if t < self.preload_s:
            force = self.preload_kn * t / self.preload_s
            strain = force * 1000 / sp.area_mm2 / sp.modulus_mpa
            return 1, force, strain * sp.gauge_length_mm
        t -= self.preload_s
        if t < self.test_s:
            strain = self.preload_strain + self.strain_rate * t
            return 2, sp.stress(strain) * sp.area_mm2 / 1000, strain * sp.gauge_length_mm
        # fractured: elastic part recovers, plastic elongation remains
        plastic = sp.fracture_strain - sp.fracture_mpa / sp.modulus_mpa
        return 3, 0.0, plastic * sp.gauge_length_mm


@dataclass
class UtmSource:
    seed: int
    faults: tuple[Fault, ...] = ()
    program: TensileProgram = field(default_factory=TensileProgram)

    @property
    def profile(self) -> ProfileSpec:
        return UTM

    def cadence_ms(self, t_ms: int) -> int:
        code, _, _ = self.program.state(t_ms)
        return 1000 if code == 0 else 200

    def run_at(self, t_ms: int) -> RunInfo | None:
        p = self.program
        n, t = p.locate(t_ms)
        if t < p.idle_s:
            return None
        cycle_start = ANCHOR_MS + round(n * p.cycle_s * 1000)
        sp = p.specimen
        return RunInfo(
            key=f"c{n:06d}",
            name=f"Tensile test S-{n:05d}",
            kind="tensile_test",
            program_start_ms=cycle_start + round(p.idle_s * 1000),
            program_end_ms=cycle_start + round(p.cycle_s * 1000),
            metadata={
                "specimen_id": f"S-{n:05d}",
                "material": "simulated structural steel",
                "geometry": {
                    "shape": "round",
                    "diameter_mm": sp.diameter_mm,
                    "area_mm2": round(sp.area_mm2, 4),
                    "gauge_length_mm": sp.gauge_length_mm,
                },
                "strain_rate_per_s": p.strain_rate,
            },
        )

    def read(self, t_ms: int) -> SourceReading:
        active = active_faults(self.faults, t_ms)
        if any(f.kind is FaultKind.DROPOUT for f in active):
            raise SourceUnavailable("no response from test frame controller")
        code, force, ext = self.program.state(t_ms)
        force = round(force + noise(self.seed, t_ms, "force", 0.004), 4)
        ext = round(ext + noise(self.seed, t_ms, "ext", 0.0004), 5)
        ext_reading = ChannelReading("extension", ext, raw=f"{ext:.5f}")
        if any(f.kind is FaultKind.EXTENSOMETER_SLIP for f in active):
            ext_reading = ChannelReading.bad(
                "extension", QualityReason.VENDOR_STATUS_FAULT, "SLIP", "extensometer_slip"
            )
        return SourceReading(
            t_ms=t_ms,
            channels=(
                ChannelReading("force", force, raw=f"{force:.4f}"),
                ext_reading,
                ChannelReading("state", code, raw=str(code)),
            ),
            run=self.run_at(t_ms),
        )
