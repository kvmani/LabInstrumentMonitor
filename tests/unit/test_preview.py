"""Commissioning preview: deterministic per seed, the collector's range rule, a fault drill."""

from __future__ import annotations

import pytest

from labmon.contracts.envelope import Quality, QualityReason
from labmon.profiles import PROFILES
from labmon.sim.preview import preview, range_checked

T = 1_790_000_000_000
CYCLES_S = {"sim-furnace": 6600, "sim-utm": 405, "sim-dilatometer": 3630, "sim-dsc": 8000}


def test_same_seed_same_preview() -> None:
    assert preview("sim-furnace", 3, T) == preview("sim-furnace", 3, T)
    assert preview("sim-furnace", 3, T)["trend"] != preview("sim-furnace", 4, T)["trend"]


@pytest.mark.parametrize("profile", sorted(CYCLES_S))
def test_every_profile_passes_across_its_cycle(profile: str) -> None:
    # scripted in-cycle faults (e.g. the dilatometer gauge error) are flagged, not failures
    for k in range(12):
        r = preview(profile, 7, T + int(k * CYCLES_S[profile] / 12 * 1000))
        assert r["ok"], [c for c in r["checks"] if not c["ok"]]
        assert r["drill"]["unavailable_frames"] > 0


def test_range_rule_matches_the_collector() -> None:
    pv = PROFILES["sim-furnace"].channel("pv")
    assert range_checked(pv, 2000.0, Quality.GOOD, None) == (Quality.BAD, QualityReason.OVERRANGE)
    assert range_checked(pv, 850.0, Quality.GOOD, None) == (Quality.GOOD, None)
    flagged = (Quality.BAD, QualityReason.SENSOR_OPEN)
    assert range_checked(pv, None, *flagged) == flagged


def test_unknown_profile_is_refused() -> None:
    with pytest.raises(ValueError):
        preview("sim-nope", 1, T)
