"""Deterministic furnace and ambient sources (known-answer values, scripted faults)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from labmon.contracts.envelope import Quality, QualityReason
from labmon.sim.sources import (
    AmbientSource,
    Fault,
    FaultKind,
    FurnaceProgram,
    FurnaceSource,
    SourceUnavailable,
)

ANCHOR = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())


def values(reading) -> dict:  # type: ignore[no-untyped-def]
    return {c.key: c.value for c in reading.channels}


def test_same_seed_same_tick_is_reproducible() -> None:
    a, b = FurnaceSource(seed=42), FurnaceSource(seed=42)
    for tick in (ANCHOR, ANCHOR + 1234, ANCHOR + 999_999):
        assert a.read(tick) == b.read(tick)
    assert values(FurnaceSource(seed=1).read(ANCHOR + 1000)) != values(a.read(ANCHOR + 1000))


@pytest.mark.parametrize(
    ("offset", "phase", "sp"),
    [(0, 0, 25.0), (299, 0, 25.0), (300, 1, 25.0), (2099, 1, 849.54), (2100, 2, 850.0),
     (3899, 2, 850.0), (3900, 3, 25.0), (6599, 3, 25.0), (6600, 0, 25.0)],
)
def test_program_phase_transitions(offset: int, phase: int, sp: float) -> None:
    p, s, _ = FurnaceProgram().state(ANCHOR + offset)
    assert p == phase
    assert s == pytest.approx(sp, abs=0.01)


def test_overshoot_known_answer() -> None:
    prog = FurnaceProgram()
    soak = range(ANCHOR + 2100, ANCHOR + 3900)
    peak = max(soak, key=lambda t: prog.state(t)[2])
    assert peak - ANCHOR == 2332
    assert prog.state(peak)[2] == pytest.approx(857.148, abs=0.001)
    # brief: back within the 5 °C tolerance well before the end of the soak
    assert abs(prog.state(ANCHOR + 3600)[2] - 850.0) < 5.0


def test_program_is_continuous_across_phase_boundaries() -> None:
    prog = FurnaceProgram()
    for boundary in (300, 2100, 3900, 6600):
        before = prog.state(ANCHOR + boundary - 1)[2]
        after = prog.state(ANCHOR + boundary)[2]
        assert abs(after - before) < 2.0, boundary


def test_noise_is_bounded_and_deterministic() -> None:
    src = FurnaceSource(seed=42)
    for tick in range(ANCHOR + 2500, ANCHOR + 2600):
        pv = values(src.read(tick))["pv"]
        assert abs(pv - FurnaceProgram().state(tick)[2]) < 1.5


def test_scripted_faults() -> None:
    faults = (
        Fault(FaultKind.DROPOUT, ANCHOR + 10, ANCHOR + 20),
        Fault(FaultKind.OPEN_PROBE, ANCHOR + 30, ANCHOR + 31),
        Fault(FaultKind.SPIKE, ANCHOR + 40, ANCHOR + 41, magnitude=120.0),
    )
    src, clean = FurnaceSource(seed=3, faults=faults), FurnaceSource(seed=3)
    with pytest.raises(SourceUnavailable):
        src.read(ANCHOR + 10)
    src.read(ANCHOR + 20)  # recovered
    open_pv = src.read(ANCHOR + 30).channels[0]
    assert open_pv.quality is Quality.BAD
    assert open_pv.quality_reason is QualityReason.SENSOR_OPEN
    assert open_pv.value is None and open_pv.raw == "OPEN"
    spike = values(src.read(ANCHOR + 40))["pv"] - values(clean.read(ANCHOR + 40))["pv"]
    assert spike == pytest.approx(120.0, abs=0.01)


def test_ambient_smoke_fixture_is_near_constant() -> None:
    src = AmbientSource(seed=7)
    vals = [src.read(ANCHOR + i).channels[0].value for i in range(0, 7200, 7)]
    assert all(22.5 < v < 23.5 for v in vals)  # type: ignore[operator]
