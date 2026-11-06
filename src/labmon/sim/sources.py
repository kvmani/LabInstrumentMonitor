"""Registry of simulated sources by profile key."""

from __future__ import annotations

from labmon.sim.ambient import AmbientSource
from labmon.sim.base import (
    ChannelReading,
    Fault,
    FaultKind,
    RunInfo,
    Source,
    SourceReading,
    SourceUnavailable,
)
from labmon.sim.dilatometer import DilatometerSource
from labmon.sim.dsc import DscSource
from labmon.sim.furnace import FurnaceSource
from labmon.sim.utm import UtmSource

_REGISTRY: dict[str, type] = {
    "sim-furnace": FurnaceSource,
    "sim-utm": UtmSource,
    "sim-dilatometer": DilatometerSource,
    "sim-dsc": DscSource,
    "sim-ambient": AmbientSource,
}

__all__ = [
    "AmbientSource",
    "ChannelReading",
    "DilatometerSource",
    "DscSource",
    "Fault",
    "FaultKind",
    "FurnaceSource",
    "RunInfo",
    "Source",
    "SourceReading",
    "SourceUnavailable",
    "UtmSource",
    "build_source",
    "simulated_profiles",
]


def simulated_profiles() -> list[str]:
    return list(_REGISTRY)


def build_source(profile_key: str, seed: int, faults: tuple[Fault, ...] = ()) -> Source:
    try:
        cls = _REGISTRY[profile_key]
    except KeyError:
        raise ValueError(f"no simulator for profile {profile_key!r}") from None
    source: Source = cls(seed=seed, faults=faults)
    return source
