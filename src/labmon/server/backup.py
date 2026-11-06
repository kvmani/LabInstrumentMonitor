"""Logical backups and verified restores (pilot procedure, SPECIFICATIONS §10).

* ``backup``: ``pg_dump --format=custom`` into a dated file written as ``*.partial`` and renamed
  when complete, its SHA-256, a ``pg_restore --list`` integrity check, retention by age, and an
  atomically replaced JSON status file that doctor and ``/api/v1/system/health`` read.
* ``restore_check``: restores a dump into an *isolated scratch database* (never the live one)
  and compares per-table row counts with the counts recorded at backup time.

Base backups with WAL archiving (point-in-time recovery) are required before relying on the
system for multi-day furnace runs; that is a site procedure documented separately.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from labmon.contracts.envelope import format_utc

COUNTED_TABLES = (
    "sample",
    "acquisition_event",
    "ingest_identity",
    "batch_receipt",
    "run",
    "annotation",
    "audit_event",
    "instrument",
    "channel",
    "app_user",
)


class BackupError(Exception):
    pass


def _bin(name: str) -> str:
    for d in (os.environ.get("LABMON_PG_BINDIR"), "/usr/lib/postgresql/16/bin"):
        if d and (Path(d) / name).exists():
            return str(Path(d) / name)
    found = shutil.which(name)
    if not found:
        raise BackupError(f"{name} not found; install PostgreSQL 16 client tools")
    return found


def _libpq(url: str) -> tuple[str, dict[str, str]]:
    """libpq URL without the SQLAlchemy driver suffix; password passed via the environment."""
    u = make_url(url)
    env = dict(os.environ)
    if u.password:
        env["PGPASSWORD"] = str(u.password)
    clean = u.set(drivername="postgresql", password=None)
    return clean.render_as_string(hide_password=False), env


def table_counts(url: str) -> dict[str, int]:
    engine = create_engine(url)
    try:
        with engine.connect() as c:
            return {
                t: int(c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one())  # noqa: S608
                for t in COUNTED_TABLES
            }
    finally:
        engine.dispose()


def _write_status(path: Path, status: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(status, indent=2))
    os.replace(tmp, path)


def backup(
    database_url: str,
    out_dir: Path,
    keep_days: float,
    status_file: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    started = now or datetime.now(UTC)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"labmon-{started:%Y%m%dT%H%M%SZ}.dump"
    final, partial = out_dir / name, out_dir / (name + ".partial")
    conninfo, env = _libpq(database_url)
    counts = table_counts(database_url)  # recorded for the restore check
    try:
        subprocess.run(
            [_bin("pg_dump"), "--format=custom", "--no-owner", "--file", str(partial), conninfo],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [_bin("pg_restore"), "--list", str(partial)],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        partial.unlink(missing_ok=True)
        _write_status(
            status_file,
            {"ok": False, "started_at": format_utc(started), "error": exc.stderr[-500:]},
        )
        raise BackupError(exc.stderr[-500:]) from exc
    with partial.open("rb") as fh:
        os.fsync(fh.fileno())
        digest = hashlib.file_digest(fh, "sha256").hexdigest()
    os.replace(partial, final)
    (out_dir / (name + ".sha256")).write_text(f"{digest}  {name}\n")
    (out_dir / (name + ".counts.json")).write_text(json.dumps(counts, indent=2))
    removed = []
    cutoff = started - timedelta(days=keep_days)
    for old in sorted(out_dir.glob("labmon-*.dump")):
        if old != final and datetime.fromtimestamp(old.stat().st_mtime, UTC) < cutoff:
            for p in (
                old,
                old.with_name(old.name + ".sha256"),
                old.with_name(old.name + ".counts.json"),
            ):
                p.unlink(missing_ok=True)
            removed.append(old.name)
    finished = datetime.now(UTC) if now is None else now
    status = {
        "ok": True,
        "started_at": format_utc(started),
        "finished_at": finished.isoformat(),
        "file": str(final),
        "bytes": final.stat().st_size,
        "sha256": digest,
        "counts": counts,
        "removed": removed,
    }
    _write_status(status_file, status)
    return status


def restore_check(dump: Path, scratch_admin_url: str) -> dict[str, Any]:
    """Restore into a new scratch database and compare counts; the scratch DB is dropped."""
    expected = json.loads((dump.with_name(dump.name + ".counts.json")).read_text())
    want = (dump.with_name(dump.name + ".sha256")).read_text().split()[0]
    with dump.open("rb") as fh:
        if hashlib.file_digest(fh, "sha256").hexdigest() != want:
            raise BackupError("checksum mismatch: refusing to restore a corrupt transfer")
    scratch = f"labmon_restore_check_{datetime.now(UTC):%Y%m%d%H%M%S}"
    admin = create_engine(scratch_admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{scratch}"'))
        url = (
            make_url(scratch_admin_url).set(database=scratch).render_as_string(hide_password=False)
        )
        conninfo, env = _libpq(url)
        res = subprocess.run(
            [_bin("pg_restore"), "--no-owner", "--exit-on-error", "--dbname", conninfo, str(dump)],
            env=env,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise BackupError(f"restore failed: {res.stderr[-500:]}")
        got = table_counts(url)
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        admin.dispose()
    mismatches = {
        t: (expected.get(t), got.get(t)) for t in COUNTED_TABLES if expected.get(t) != got.get(t)
    }
    return {"ok": not mismatches, "dump": str(dump), "counts": got, "mismatches": mismatches}


def backup_status(status_file: Path | None, now: datetime) -> dict[str, Any] | None:
    if status_file is None:
        return None
    try:
        info = json.loads(status_file.read_text())
    except (OSError, ValueError):
        return {"ok": False, "error": f"no readable status at {status_file}"}
    finished = info.get("finished_at")
    if finished:
        info["age_hours"] = round(
            (now - datetime.fromisoformat(finished)).total_seconds() / 3600, 2
        )
    return dict(info)
