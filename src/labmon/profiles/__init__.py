"""Built-in simulator profiles.

Default fleet: furnace, universal tensile testing machine (UTM) and fictional vacuum dilatometer.
The DSC is an opt-in fourth profile. The ambient profile is an automated smoke fixture only.
"""

from __future__ import annotations

from labmon.contracts.envelope import SourceTimeMeaning
from labmon.profiles.base import AnalysisKind, ChannelSpec, DataType, PanelSpec, ProfileSpec

FURNACE_PHASES = {"0": "Idle", "1": "Ramp", "2": "Soak", "3": "Cooldown"}
UTM_STATES = {"0": "Idle", "1": "Preload", "2": "Testing", "3": "Fractured"}

FURNACE = ProfileSpec(
    key="sim-furnace",
    title="Heat-treatment furnace (simulated)",
    adapter="labmon.sim.furnace",
    adapter_version="2",
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
            note="recorded controller setpoint",
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
    panels=(
        PanelSpec(key="temperature", title="Temperature", channels=("pv", "sp"), tolerance_of="sp"),
        PanelSpec(key="phase", title="Program phase", channels=("phase",)),
    ),
)

UTM = ProfileSpec(
    key="sim-utm",
    title="Universal tensile testing machine (simulated)",
    adapter="labmon.sim.utm",
    adapter_version="1",
    source_time_meaning=SourceTimeMeaning.ABSENT,
    state_channel="state",
    analysis=AnalysisKind.TENSILE,
    cadence_note="5 Hz while a test runs, 1 Hz while idle (simulator setting)",
    channels=(
        ChannelSpec(
            key="force",
            name="Force",
            quantity="force",
            unit="kN",
            data_type=DataType.FLOAT,
            expected_cadence_s=1.0,
            display_precision=3,
            valid_min=-5.0,
            valid_max=100.0,
        ),
        ChannelSpec(
            key="extension",
            name="Extensometer extension",
            quantity="length",
            unit="mm",
            data_type=DataType.FLOAT,
            expected_cadence_s=1.0,
            display_precision=4,
            valid_min=-1.0,
            valid_max=25.0,
        ),
        ChannelSpec(
            key="state",
            name="Test state",
            quantity="state",
            unit="",
            data_type=DataType.ENUM,
            expected_cadence_s=1.0,
            display_precision=0,
            role="state",
            enum_labels=UTM_STATES,
        ),
    ),
    panels=(
        PanelSpec(key="force", title="Force", channels=("force",)),
        PanelSpec(key="extension", title="Extension", channels=("extension",)),
        PanelSpec(key="state", title="Test state", channels=("state",)),
    ),
)

DILATOMETER = ProfileSpec(
    key="sim-dilatometer",
    title="Vacuum dilatometer (fictional, simulated)",
    adapter="labmon.sim.dilatometer",
    adapter_version="1",
    source_time_meaning=SourceTimeMeaning.ABSENT,
    channels=(
        ChannelSpec(
            key="pressure",
            name="Chamber pressure (absolute)",
            quantity="pressure",
            unit="Pa",
            data_type=DataType.FLOAT,
            expected_cadence_s=1.0,
            display_precision=3,
            display="scientific",
            valid_min=1e-6,
            valid_max=1.2e5,
        ),
        ChannelSpec(
            key="temperature",
            name="Specimen temperature",
            quantity="temperature",
            unit="°C",
            data_type=DataType.FLOAT,
            expected_cadence_s=1.0,
            display_precision=1,
            valid_min=-50.0,
            valid_max=1600.0,
        ),
        ChannelSpec(
            key="dl",
            name="Length change ΔL",
            quantity="length change",
            unit="µm",
            data_type=DataType.FLOAT,
            expected_cadence_s=1.0,
            display_precision=2,
            valid_min=-2000.0,
            valid_max=2000.0,
            note="signed: negative is contraction",
        ),
    ),
    panels=(
        PanelSpec(
            key="pressure",
            title="Chamber pressure",
            channels=("pressure",),
            scale="log",
            log_allowed=True,
        ),
        PanelSpec(key="temperature", title="Specimen temperature", channels=("temperature",)),
        PanelSpec(key="dl", title="Length change ΔL", channels=("dl",)),
    ),
)

DSC = ProfileSpec(
    key="sim-dsc",
    title="Differential scanning calorimeter (simulated, opt-in)",
    adapter="labmon.sim.dsc",
    adapter_version="1",
    source_time_meaning=SourceTimeMeaning.ABSENT,
    analysis=AnalysisKind.DSC,
    sign_convention="exo up: positive heat flow is exothermic, endothermic peaks point down",
    opt_in=True,
    channels=(
        ChannelSpec(
            key="sample_t",
            name="Sample temperature",
            quantity="temperature",
            unit="°C",
            data_type=DataType.FLOAT,
            expected_cadence_s=1.0,
            display_precision=2,
            valid_min=-150.0,
            valid_max=725.0,
        ),
        ChannelSpec(
            key="program_t",
            name="Programmed temperature",
            quantity="temperature",
            unit="°C",
            data_type=DataType.FLOAT,
            expected_cadence_s=1.0,
            display_precision=2,
            role="setpoint",
            valid_min=-150.0,
            valid_max=725.0,
        ),
        ChannelSpec(
            key="heat_flow",
            name="Differential heat-flow rate",
            quantity="heat flow rate",
            unit="mW",
            data_type=DataType.FLOAT,
            expected_cadence_s=1.0,
            display_precision=3,
            valid_min=-500.0,
            valid_max=500.0,
            note="exo up",
        ),
    ),
    panels=(
        PanelSpec(key="temperature", title="Temperature", channels=("sample_t", "program_t")),
        PanelSpec(key="heat_flow", title="Heat flow (exo ↑)", channels=("heat_flow",)),
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
    panels=(PanelSpec(key="temp", title="Room temperature", channels=("temp",)),),
)

PROFILES: dict[str, ProfileSpec] = {p.key: p for p in (FURNACE, UTM, DILATOMETER, DSC, AMBIENT)}
DEFAULT_FLEET = (FURNACE.key, UTM.key, DILATOMETER.key)
