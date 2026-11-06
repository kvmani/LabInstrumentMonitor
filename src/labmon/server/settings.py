"""Central API settings from environment variables. Mutable state lives outside the release."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}


def _default_static_dir() -> Path | None:
    candidate = Path(__file__).resolve().parents[3] / "web" / "dist"
    return candidate if candidate.is_dir() else None


@dataclass(frozen=True)
class Settings:
    database_url: str
    environment: str = "production"  # production | development | test
    static_dir: Path | None = field(default_factory=_default_static_dir)
    cookie_secure: bool = True
    session_ttl_hours: float = 12.0
    display_timezone: str = "Asia/Kolkata"
    heartbeat_offline_s: float = 30.0
    future_tolerance_s: float = 5.0
    max_future_s: float = 86_400.0
    max_series_range_days: float = 31.0
    raw_point_limit: int = 5_000
    max_buckets: int = 1_200
    csv_row_limit: int = 500_000
    export_dir: Path = field(default_factory=lambda: Path("var/exports"))
    export_row_limit: int = 10_000_000
    export_retention_days: float = 7.0
    export_max_attempts: int = 3
    export_lease_s: float = 300.0
    backup_status_file: Path | None = None
    login_attempts_per_15min: int = 5

    @property
    def dev_tools_allowed(self) -> bool:
        return self.environment in ("development", "test")

    @classmethod
    def from_env(cls) -> Settings:
        url = os.environ.get("LABMON_DATABASE_URL")
        if not url:
            raise RuntimeError("LABMON_DATABASE_URL is required")
        env = os.environ.get("LABMON_ENV", "production")
        if env not in ("production", "development", "test"):
            raise RuntimeError("LABMON_ENV must be production, development or test")
        static = os.environ.get("LABMON_STATIC_DIR")
        secure_default = "false" if env != "production" else "true"
        return cls(
            database_url=url,
            environment=env,
            static_dir=Path(static) if static else _default_static_dir(),
            cookie_secure=os.environ.get("LABMON_COOKIE_SECURE", secure_default).lower() in _TRUE,
            session_ttl_hours=float(os.environ.get("LABMON_SESSION_TTL_HOURS", "12")),
            display_timezone=os.environ.get("LABMON_DISPLAY_TZ", "Asia/Kolkata"),
            export_dir=Path(os.environ.get("LABMON_EXPORT_DIR", "var/exports")),
            backup_status_file=Path(os.environ["LABMON_BACKUP_STATUS_FILE"])
            if os.environ.get("LABMON_BACKUP_STATUS_FILE")
            else None,
        )
