"""Typed source profiles.

A profile declares the channels a source produces, their physical meaning and how the adapter's
timestamps should be interpreted. Profiles contain data only: no executable expressions.
A published profile revision is immutable; the collector stamps every record with the revision it
acquired under.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

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

    @property
    def freshness_window_s(self) -> float:
        """Starting default from the specification: max(3 x cadence, 5 s)."""
        return max(3.0 * self.expected_cadence_s, 5.0)


class ProfileSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=ID_PATTERN)
    title: str
    adapter: str
    adapter_version: str
    source_time_meaning: SourceTimeMeaning
    channels: tuple[ChannelSpec, ...]
    state_channel: str | None = None  # validated process-state channel, if any
    tolerance_c: float | None = None  # furnace soak tolerance (advisory display only in M1)

    def channel(self, key: str) -> ChannelSpec:
        for ch in self.channels:
            if ch.key == key:
                return ch
        raise KeyError(key)

    def definition_digest(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="json")))
