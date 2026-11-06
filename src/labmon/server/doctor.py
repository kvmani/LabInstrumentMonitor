"""Read-only preflight (``labmon-server doctor``): checks before activation and after upgrades.

Every check is read-only; the command changes nothing. Exit status 0 means all checks passed,
1 means at least one failed (warnings do not fail). Output is human-readable or ``--json``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from labmon import __version__
from labmon.server import migrate
from labmon.server.backup import backup_status
from labmon.server.db import make_engine
from labmon.server.settings import Settings

MIN_FREE_BYTES = 2 * 2**30


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str


def run_checks(settings: Settings) -> list[Check]:
    out: list[Check] = []

    def add(name: str, ok: bool, detail: str, warn_only: bool = False) -> None:
        out.append(Check(name, "ok" if ok else ("warn" if warn_only else "fail"), detail))

    add(
        "python",
        sys.version_info[:2] == (3, 12),
        f"{sys.version.split()[0]} (baseline 3.12)",
        warn_only=True,
    )
    add("sqlite", True, f"linked SQLite {sqlite3.sqlite_version} (collector outbox)")
    add(
        "environment",
        settings.environment in ("production", "development", "test"),
        settings.environment,
    )
    if settings.environment == "production":
        add(
            "cookies",
            settings.cookie_secure,
            "Secure session cookies"
            if settings.cookie_secure
            else "LABMON_COOKIE_SECURE must be true in production",
        )
    try:
        ZoneInfo(settings.display_timezone)
        add("display time zone", True, settings.display_timezone)
    except ZoneInfoNotFoundError:
        add("display time zone", False, f"unknown zone {settings.display_timezone!r}")

    engine = make_engine(settings.database_url, pool_size=1)
    try:
        with engine.connect() as c:
            version = c.execute(text("SHOW server_version")).scalar_one()
            add("database", True, f"PostgreSQL {version}")
            add(
                "database version",
                str(version).startswith("16"),
                f"{version} (baseline 16)",
                warn_only=True,
            )
            tz = c.execute(text("SHOW TimeZone")).scalar_one()
            add("session time zone", tz == "UTC", f"session TimeZone={tz}")
        current, head = migrate.current_revision(engine), migrate.head_revision()
        add(
            "schema",
            current == head,
            f"database at {current}, release expects {head}"
            + ("" if current == head else " — run `labmon-server migrate`"),
        )
    except OperationalError as exc:
        add("database", False, f"cannot connect: {exc.__class__.__name__}")
    finally:
        engine.dispose()

    static = settings.static_dir
    add(
        "web bundle",
        static is not None and (static / "index.html").is_file(),
        str(static) if static else "LABMON_STATIC_DIR not set and web/dist not found",
    )
    try:
        settings.export_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.export_dir / ".doctor-probe"
        probe.write_text("ok")
        probe.unlink()
        free = shutil.disk_usage(settings.export_dir).free
        add("export directory", True, f"{settings.export_dir} writable")
        add("export disk", free >= MIN_FREE_BYTES, f"{free / 2**30:.1f} GiB free", warn_only=True)
    except OSError as exc:
        add("export directory", False, f"{settings.export_dir}: {exc}")
    # the release directory holds the virtualenv (``<release>/venv``); state must live elsewhere
    release_root = Path(os.environ.get("LABMON_RELEASE_DIR") or Path(sys.prefix).resolve().parent)
    inside = settings.export_dir.resolve().is_relative_to(release_root)
    add(
        "state outside release",
        not inside or settings.environment != "production",
        "export directory is outside the code tree"
        if not inside
        else f"{settings.export_dir} is inside the release {release_root}",
        warn_only=True,
    )
    info = backup_status(settings.backup_status_file, datetime.now(UTC))
    if info is None:
        add("backup freshness", False, "LABMON_BACKUP_STATUS_FILE not set", warn_only=True)
    elif not info.get("ok"):
        add("backup freshness", False, str(info.get("error", "last backup failed")), warn_only=True)
    else:
        add(
            "backup freshness",
            info.get("age_hours", 0) <= 26,
            f"last backup {info.get('age_hours')} h ago ({info.get('file')})",
            warn_only=True,
        )
    return out


def main(settings: Settings, as_json: bool) -> int:
    checks = run_checks(settings)
    failed = any(c.status == "fail" for c in checks)
    if as_json:
        print(
            json.dumps(
                {"version": __version__, "ok": not failed, "checks": [asdict(c) for c in checks]},
                indent=2,
            )
        )
    else:
        mark = {"ok": "✓", "warn": "!", "fail": "✕"}
        print(f"labmon {__version__} doctor")
        for c in checks:
            print(f"  {mark[c.status]} {c.name:22} {c.detail}")
        print("result:", "FAIL" if failed else "OK")
    return 1 if failed else 0
