"""Known-answer tests for the deterministic simulator fleet."""

from __future__ import annotations

import pytest

from labmon.contracts.envelope import Quality, QualityReason
from labmon.profiles import PROFILES
from labmon.sim.ambient import AmbientSource
from labmon.sim.base import ANCHOR_MS, Fault, FaultKind, SourceUnavailable
from labmon.sim.dilatometer import DilatometerProgram, DilatometerSource
from labmon.sim.dsc import DscProgram, DscSource
from labmon.sim.furnace import FurnaceProgram, FurnaceSource
from labmon.sim.sources import build_source, simulated_profiles
from labmon.sim.utm import TensileProgram, UtmSource

A_S = ANCHOR_MS // 1000


def values(reading) -> dict:  # type: ignore[no-untyped-def]
    return {c.key: c.value for c in reading.channels}


def ms(offset_s: float, cycle: int = 0, cycle_s: float = 0.0) -> int:
    return ANCHOR_MS + round((cycle * cycle_s + offset_s) * 1000)


# --- shared contract --------------------------------------------------------------------------


@pytest.mark.parametrize("key", simulated_profiles())
def test_every_source_is_reproducible_and_matches_its_profile(key: str) -> None:
    a, b = build_source(key, 42), build_source(key, 42)
    profile = PROFILES[key]
    for t in (ANCHOR_MS, ANCHOR_MS + 1_234_000, ANCHOR_MS + 987_654_000):
        ra, rb = a.read(t), b.read(t)
        assert ra == rb
        assert [c.key for c in ra.channels] == [c.key for c in profile.channels]
        assert a.cadence_ms(t) in (200, 1000)
    assert (
        values(build_source(key, 1).read(ANCHOR_MS + 5000)) != values(a.read(ANCHOR_MS + 5000))
        or key == "sim-utm"
    )  # UTM idle values may coincide


def test_unknown_profile_is_refused() -> None:
    with pytest.raises(ValueError):
        build_source("sim-unknown", 1)


# --- furnace ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "phase", "sp"),
    [
        (0, 0, 25.0),
        (299, 0, 25.0),
        (300, 1, 25.0),
        (2099, 1, 849.54),
        (2100, 2, 850.0),
        (3899, 2, 850.0),
        (3900, 3, 25.0),
        (6599, 3, 25.0),
        (6600, 0, 25.0),
    ],
)
def test_furnace_phase_transitions(offset: int, phase: int, sp: float) -> None:
    p, s, _ = FurnaceProgram().state(A_S + offset)
    assert p == phase
    assert s == pytest.approx(sp, abs=0.01)


def test_furnace_overshoot_known_answer_and_continuity() -> None:
    prog = FurnaceProgram()
    peak = max(range(A_S + 2100, A_S + 3900), key=lambda t: prog.state(t)[2])
    assert peak - A_S == 2332
    assert prog.state(peak)[2] == pytest.approx(857.148, abs=0.001)
    for boundary in (300, 2100, 3900, 6600):
        assert abs(prog.state(A_S + boundary)[2] - prog.state(A_S + boundary - 1)[2]) < 2.0


def test_furnace_scripted_faults_and_runs() -> None:
    faults = (
        Fault(FaultKind.DROPOUT, A_S + 10, A_S + 20),
        Fault(FaultKind.OPEN_PROBE, A_S + 30, A_S + 31),
        Fault(FaultKind.SPIKE, A_S + 40, A_S + 41, magnitude=120.0),
    )
    src, clean = FurnaceSource(seed=3, faults=faults), FurnaceSource(seed=3)
    with pytest.raises(SourceUnavailable):
        src.read(ms(10))
    open_pv = src.read(ms(30)).channels[0]
    assert (open_pv.quality, open_pv.quality_reason, open_pv.value) == (
        Quality.BAD,
        QualityReason.SENSOR_OPEN,
        None,
    )
    spike = values(src.read(ms(40)))["pv"] - values(clean.read(ms(40)))["pv"]
    assert spike == pytest.approx(120.0, abs=0.01)
    assert src.read(ms(100)).run is None  # idle
    run = src.read(ms(400)).run
    assert run is not None and run.key == "c000000" and run.kind == "heat_treatment"
    assert src.read(ms(400, 1, 6600)).run.key == "c000001"  # type: ignore[union-attr]


# --- UTM --------------------------------------------------------------------------------------


def test_utm_known_answer_uts_and_fracture_order() -> None:
    prog = TensileProgram()
    spec = prog.specimen
    assert spec.area_mm2 == pytest.approx(28.2743, abs=1e-4)
    grid = range(ms(245), ms(390), 200)
    peak_t = max(grid, key=lambda t: prog.state(t)[1])
    _, force, ext = prog.state(peak_t)
    stress = force * 1000 / spec.area_mm2
    assert stress == pytest.approx(480.0, abs=0.01)
    assert ext / spec.gauge_length_mm == pytest.approx(0.18, abs=0.0005)
    # fracture: the first fractured sample comes after the maximum, at strain 0.28
    fracture_t = next(t for t in grid if prog.state(t)[0] == 3)
    assert fracture_t > peak_t
    last_testing = max(t for t in grid if prog.state(t)[0] == 2)
    _, f_last, e_last = prog.state(last_testing)
    assert e_last / spec.gauge_length_mm == pytest.approx(0.28, abs=0.001)
    assert f_last * 1000 / spec.area_mm2 == pytest.approx(390.0, abs=2.0)
    assert prog.state(fracture_t)[1] == 0.0


def test_utm_elastic_slope_and_yield() -> None:
    spec = TensileProgram().specimen
    assert spec.stress(0.001) == pytest.approx(200.0)
    assert spec.stress(spec.yield_strain) == pytest.approx(350.0)
    strains = [i / 1000 for i in range(0, 281)]
    stresses = [spec.stress(e) for e in strains]
    assert max(stresses) == pytest.approx(480.0)
    assert strains[stresses.index(max(stresses))] == pytest.approx(0.18)


def test_utm_cadence_is_5hz_only_while_testing() -> None:
    src = UtmSource(seed=1)
    assert src.cadence_ms(ms(100)) == 1000  # idle
    assert src.cadence_ms(ms(239)) == 1000  # next sample lands exactly on the preload start
    assert src.cadence_ms(ms(240)) == 200
    assert src.cadence_ms(ms(300)) == 200
    assert src.cadence_ms(ms(405)) == 1000  # next cycle idle
    assert values(src.read(ms(240)))["state"] == 1


def test_utm_run_carries_geometry_and_distinct_ids() -> None:
    src = UtmSource(seed=1)
    run = src.read(ms(300)).run
    assert run is not None and run.name == "Tensile test S-00000"
    geo = run.metadata["geometry"]
    assert geo == {
        "shape": "round",
        "diameter_mm": 6.0,
        "area_mm2": 28.2743,
        "gauge_length_mm": 25.0,
    }
    assert src.read(ms(300, 1, 405)).run.key == "c000001"  # type: ignore[union-attr]
    assert src.read(ms(100)).run is None


def test_utm_extensometer_slip_gives_missing_pair() -> None:
    src = UtmSource(seed=1, faults=(Fault(FaultKind.EXTENSOMETER_SLIP, A_S + 300, A_S + 301),))
    r = src.read(ms(300.2))
    ext = r.channels[1]
    assert ext.quality is Quality.BAD and ext.value is None and ext.raw == "SLIP"
    assert r.channels[0].quality is Quality.GOOD  # force is still valid


# --- vacuum dilatometer -----------------------------------------------------------------------


def test_dilatometer_pump_down_is_monotonic_and_reaches_high_vacuum() -> None:
    prog = DilatometerProgram()
    pressures = [prog.state(ms(t))[1] for t in range(120, 720)]
    assert all(b <= a for a, b in zip(pressures, pressures[1:], strict=False))
    assert pressures[0] == pytest.approx(1.0e5, rel=1e-3)
    assert pressures[-1] < 0.2
    assert prog.state(ms(1500))[1] < 2e-3


def test_dilatometer_pressure_disturbance_and_recovery() -> None:
    prog = DilatometerProgram()
    hold_start = 120 + 600 + 1050
    assert prog.state(ms(hold_start + 215))[1] == pytest.approx(0.301, abs=0.002)
    assert prog.state(ms(hold_start + 300))[1] < 2e-3


def test_dilatometer_signed_length_change() -> None:
    prog = DilatometerProgram()
    heat_start = 720
    # thermal expansion 0.35 µm/K before the transformation
    _, _, t, dl = prog.state(ms(heat_start + 120))
    assert dl == pytest.approx(0.35 * (t - 25.0), abs=0.01)
    # contraction through 740 °C on heating
    before = prog.state(ms(heat_start + 820))  # ~708 °C
    after = prog.state(ms(heat_start + 900))  # 775 °C
    assert after[3] - before[3] < 0.35 * (after[2] - before[2]) - 30
    # negative (contraction) at the end of the cycle; zero for the next specimen
    end = prog.state(ms(3569))
    assert end[0] == "cooling" and end[3] == pytest.approx(-9.37, abs=0.05)
    assert prog.state(ms(3630))[3] == 0.0


def test_dilatometer_gauge_fault_and_positive_pressure() -> None:
    src = DilatometerSource(seed=9)
    bad = src.read(ms(720 + 125)).channels[0]
    assert bad.quality is Quality.BAD and bad.raw == "ERR" and bad.value is None
    ok = src.read(ms(720 + 140)).channels[0]
    assert ok.quality is Quality.GOOD and ok.value > 0  # type: ignore[operator]
    readings = [src.read(ms(t)).channels[0].value for t in range(0, 3630, 37)]
    assert all(v > 0 for v in readings if v is not None)  # type: ignore[operator]


# --- DSC --------------------------------------------------------------------------------------


def test_dsc_melting_endotherm_sign_temperature_and_area() -> None:
    prog = DscProgram()
    heat = [(t,) + prog.state(ms(t))[1:] for t in range(120, 1140)]
    t_min, _, sample_min, hf_min = min(heat, key=lambda x: x[3])
    assert hf_min < -25.0  # exo up: melting points down
    assert 157.5 < sample_min < 159.5
    # integrate the peak against the local baseline (straight line across the peak)
    window = [x for x in heat if 140 <= x[2] <= 175]
    base = [x for x in window if abs(x[2] - 156.6) > 8 and x[2] < 156.6 or x[2] > 166]
    b0, b1 = base[0], base[-1]
    slope = (b1[3] - b0[3]) / (b1[0] - b0[0])
    area = sum(x[3] - (b0[3] + slope * (x[0] - b0[0])) for x in window)  # mW × 1 s = mJ
    assert area == pytest.approx(-286.0, rel=0.02)


def test_dsc_crystallisation_exotherm_on_cooling() -> None:
    prog = DscProgram()
    cool = [prog.state(ms(t)) for t in range(1260, 2280)]
    seg, _, sample, hf = max(cool, key=lambda x: x[3])
    assert seg == "cooling" and hf > 30.0 and 152.0 < sample < 154.5


def test_dsc_sample_lags_program_and_runs_cover_each_cycle() -> None:
    src = DscSource(seed=2)
    v = values(src.read(ms(600)))
    assert v["program_t"] - v["sample_t"] == pytest.approx(1.0, abs=0.05)  # 10 K/min × 6 s
    assert src.read(ms(10)).run.key == "c000000"  # type: ignore[union-attr]
    assert src.read(ms(2520 + 10)).run.key == "c000001"  # type: ignore[union-attr]
    assert src.read(ms(10)).run.metadata["sign_convention"] == "exo_up"  # type: ignore[union-attr]


# --- ambient smoke fixture --------------------------------------------------------------------


def test_ambient_smoke_fixture_is_near_constant() -> None:
    src = AmbientSource(seed=7)
    vals = [src.read(ms(i)).channels[0].value for i in range(0, 7200, 7)]
    assert all(22.5 < v < 23.5 for v in vals)  # type: ignore[operator]
