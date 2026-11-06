"""Built-in simulator profiles. M1 ships the furnace and the ambient smoke fixture only."""

from __future__ import annotations

from labmon.contracts.envelope import SourceTimeMeaning
from labmon.profiles.base import ChannelSpec, DataType, ProfileSpec

FURNACE_PHASES = {"0": "Idle", "1": "Ramp", "2": "Soak", "3": "Cooldown"}

FURNACE = ProfileSpec(
    key="sim-furnace",
    title="Heat-treatment furnace (simulated)",
    adapter="labmon.sim.furnace",
    adapter_version="1",
    source_time_meaning=SourceTimeMeaning.ABSENT,
    state_channel="phase",
    tolerance_c=5.0,
    channels=(
        ChannelSpec(
            key="pv",
            name="Process temperature",
            quantity="temperature",
            unit="°C",
            data_type=DataType.FLOAT,
            expected_cadence_s=1.0,
            display_precision=1,
            role="measurement",
            valid_min=-50.0,
            valid_max=1400.0,
        ),
        ChannelSpec(
            key="sp",
            name="Setpoint",
            quantity="temperature",
            unit="°C",
            data_type=DataType.FLOAT,
            expected_cadence_s=1.0,
            display_precision=1,
            role="setpoint",
            valid_min=-50.0,
            valid_max=1400.0,
        ),
        ChannelSpec(
            key="phase",
            name="Program phase",
            quantity="state",
            unit="",
            data_type=DataType.ENUM,
            expected_cadence_s=1.0,
            display_precision=0,
            role="state",
            enum_labels=FURNACE_PHASES,
        ),
    ),
)

AMBIENT = ProfileSpec(
    key="sim-ambient",
    title="Ambient room temperature (simulated smoke fixture)",
    adapter="labmon.sim.ambient",
    adapter_version="1",
    source_time_meaning=SourceTimeMeaning.ABSENT,
    channels=(
        ChannelSpec(
            key="temp",
            name="Room temperature",
            quantity="temperature",
            unit="°C",
            data_type=DataType.FLOAT,
            expected_cadence_s=1.0,
            display_precision=2,
            valid_min=-20.0,
            valid_max=60.0,
        ),
    ),
)

PROFILES: dict[str, ProfileSpec] = {p.key: p for p in (FURNACE, AMBIENT)}
