"""Typed source profiles.

A profile declares the channels a source produces, their physical meaning and how the adapter's
timestamps should be interpreted. Profiles contain data only: no executable expressions.
A published profile revision is immutable; the collector stamps every record with the revision it
acquired under.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from labmon.contracts.envelope import ID_PATTERN, SourceTimeMeaning, canonical_json, sha256_hex


class DataType(StrEnum):
    FLOAT = "float"
    BOOL = "bool"
    ENUM = "enum"


class ChannelSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    name: str = Field(max_length=64)
    quantity: str = Field(max_length=64)
    unit: str = Field(max_length=16)  # native engineering unit, "" for dimensionless/enum
    data_type: DataType
    expected_cadence_s: float = Field(gt=0, le=3600)
    display_precision: int = Field(ge=0, le=6)
    enum_labels: dict[str, str] | None = None
    role: str = Field(default="measurement", pattern=r"^(measurement|setpoint|state)$")
    valid_min: float | None = None
    valid_max: float | None = None
    display: str = Field(default="fixed", pattern=r"^(fixed|scientific)$")
    note: str | None = Field(default=None, max_length=200)

    @property
    def freshness_window_s(self) -> float:
        """Starting default from the specification: max(3 x cadence, 5 s)."""
        return max(3.0 * self.expected_cadence_s, 5.0)


class PanelSpec(BaseModel):
    """One plot panel of the default instrument view (small multiples share the time axis)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    title: str = Field(max_length=64)
    channels: tuple[str, ...] = Field(min_length=1, max_length=4)
    scale: str = Field(default="linear", pattern=r"^(linear|log)$")
    log_allowed: bool = False  # offer a log toggle (strictly positive quantities only)
    tolerance_of: str | None = None  # draw +/- tolerance around this setpoint channel


class AnalysisKind(StrEnum):
    TENSILE = "tensile"  # engineering stress-strain from force/extension + run geometry
    DSC = "dsc"  # heat flow vs sample temperature, heating/cooling segments


class ProfileSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=ID_PATTERN)
    title: str
    adapter: str
    adapter_version: str
    source_time_meaning: SourceTimeMeaning
    channels: tuple[ChannelSpec, ...]
    state_channel: str | None = None  # validated process-state channel, if any
    tolerance_c: float | None = None  # furnace soak tolerance (advisory display only)
    panels: tuple[PanelSpec, ...] = ()
    analysis: AnalysisKind | None = None
    sign_convention: str | None = Field(default=None, max_length=120)
    cadence_note: str | None = Field(default=None, max_length=200)
    opt_in: bool = False  # not part of the default fleet

    @model_validator(mode="after")
    def _check(self) -> ProfileSpec:
        keys = {c.key for c in self.channels}
        if len(keys) != len(self.channels):
            raise ValueError("channel keys must be unique")
        for p in self.panels:
            missing = set(p.channels) - keys
            if missing:
                raise ValueError(f"panel {p.key} references unknown channels {sorted(missing)}")
            if p.tolerance_of is not None and p.tolerance_of not in keys:
                raise ValueError(f"panel {p.key} tolerance_of is not a channel")
        if self.state_channel is not None and self.state_channel not in keys:
            raise ValueError("state_channel is not a channel")
        return self

    def channel(self, key: str) -> ChannelSpec:
        for ch in self.channels:
            if ch.key == key:
                return ch
        raise KeyError(key)

    def definition_digest(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="json")))
