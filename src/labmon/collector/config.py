"""Collector configuration (TOML). The credential lives in a separate, owner-only file."""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from labmon.contracts.envelope import ID_PATTERN
from labmon.profiles import PROFILES
from labmon.sim.sources import Fault

StableId = Annotated[str, Field(pattern=ID_PATTERN)]


class InstrumentBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: StableId
    profile: str
    profile_revision_id: StableId
    seed: int = 0
    channels: dict[str, StableId]  # profile channel key -> central channel id
    faults: tuple[Fault, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> InstrumentBinding:
        if self.profile not in PROFILES:
            raise ValueError(f"unknown profile {self.profile!r}")
        expected = {c.key for c in PROFILES[self.profile].channels}
        if set(self.channels) != expected:
            raise ValueError(f"channels must map exactly {sorted(expected)}")
        return self


class CollectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    collector_id: StableId
    server_url: str
    token_file: Path
    spool_path: Path
    ca_file: Path | None = None
    allow_insecure_http: bool = False  # development only; production uses approved HTTPS
    cadence_s: float = Field(default=1.0, gt=0.05, le=3600)
    heartbeat_s: float = Field(default=10.0, gt=0.5, le=300)
    flush_s: float = Field(default=1.0, gt=0.05, le=60)
    request_timeout_s: float = Field(default=10.0, gt=0.5, le=120)
    max_backoff_s: float = Field(default=60.0, ge=1, le=900)
    replay_retention_days: float = Field(default=7.0, ge=0, le=90)
    spool_capacity: int = Field(default=500_000, ge=100)
    diagnostic_reserve: int = Field(default=2_000, ge=10)
    journal_mode: str = Field(default="auto", pattern=r"^(auto|wal|delete)$")
    instruments: tuple[InstrumentBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> CollectorConfig:
        url = urlsplit(self.server_url)
        if url.scheme not in ("https", "http") or not url.hostname:
            raise ValueError("server_url must be an http(s) URL with a host")
        if url.scheme == "http" and not self.allow_insecure_http:
            raise ValueError("plain http requires allow_insecure_http = true (development only)")
        if url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError("server_url must not carry a path, query or fragment")
        ids = [i.instrument_id for i in self.instruments]
        if len(ids) != len(set(ids)):
            raise ValueError("instrument ids must be unique")
        return self

    @property
    def base_url(self) -> str:
        return self.server_url.rstrip("/")

    def read_token(self) -> str:
        mode = self.token_file.stat().st_mode
        if os.name == "posix" and mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError(f"{self.token_file} must not be readable by group/others")
        token = self.token_file.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise ValueError("collector token too short")
        return token


def load_config(path: Path) -> CollectorConfig:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    for key in ("token_file", "spool_path", "ca_file"):
        if key in data and not Path(data[key]).is_absolute():
            data[key] = str((base / data[key]).resolve())
    return CollectorConfig.model_validate(data)
