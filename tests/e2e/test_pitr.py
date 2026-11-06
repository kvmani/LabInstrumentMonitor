"""Point-in-time recovery rehearsal on disposable PostgreSQL 16 clusters (simulated evidence).

Follows docs/operations/pitr.md: WAL archiving, a base backup, application writes, an accidental
delete, then recovery into a *separate* cluster to a time before the delete. The restored database
must hold every record committed before the target and the release's schema head.
"""

from __future__ import annotations

import os
import pwd
import shutil
import tempfile
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from labmon.devtools.pgcluster import Cluster
from labmon.server import migrate
from labmon.server.bootstrap import bootstrap_dev
from labmon.server.db import make_engine
from tests.conftest import free_port


def _owned_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        pw = pwd.getpwnam("postgres")
        os.chown(path, pw.pw_uid, pw.pw_gid)
    return path


@pytest.fixture
def workdir() -> Iterator[Path]:
    # pytest's tmp dirs are 0700 for root, so the postgres account could not traverse them
    root = Path(tempfile.mkdtemp(prefix="labmon-pitr-", dir="/tmp"))
    root.chmod(0o711)
    clusters: list[Cluster] = []
    try:
        yield root
    finally:
        for d in ("primary", "restored"):
            if (root / d / "PG_VERSION").exists():
                clusters.append(Cluster(root / d))
        for c in clusters:
            c.stop()
        shutil.rmtree(root, ignore_errors=True)


def _wait(pred, timeout: float, what: str) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def test_point_in_time_recovery(workdir: Path) -> None:
    archive = _owned_dir(workdir / "wal-archive")
    primary = Cluster(workdir / "primary", port=free_port())
    primary.init()
    with (primary.datadir / "postgresql.conf").open("a") as fh:
        fh.write(
            "wal_level = replica\n"
            "archive_mode = on\n"
            f"archive_command = 'test ! -f {archive}/%f && cp %p {archive}/%f'\n"
        )
    primary.start()
    url = primary.database_url()
    migrate.upgrade(url)
    engine = make_engine(url, pool_size=1)
    bootstrap_dev(engine, workdir / "collector", "http://127.0.0.1:9", None)

    # 1 · base backup (the weekly job in the runbook)
    base = _owned_dir(workdir / "base")
    primary._run(
        "pg_basebackup", "-h", "127.0.0.1", "-p", str(primary.port), "-U", "postgres",
        "-D", str(base / "data"), "-X", "stream", "-c", "fast",
    )  # fmt: skip

    # 2 · scientific records written after the base backup
    with engine.begin() as c:
        c.execute(text("CREATE TABLE pitr_probe (n integer PRIMARY KEY, note text)"))
        c.execute(text("INSERT INTO pitr_probe SELECT g, 'kept' FROM generate_series(1, 100) g"))
        labs_before = c.execute(text("SELECT count(*) FROM lab")).scalar_one()
    time.sleep(1.1)
    with engine.connect() as c:
        target: datetime = c.execute(text("SELECT clock_timestamp()")).scalar_one()
    time.sleep(1.1)

    # 3 · the incident: an operator deletes records and a lab after the target time
    with engine.begin() as c:
        c.execute(text("DELETE FROM pitr_probe WHERE n > 10"))
        c.execute(text("UPDATE lab SET name = 'wrong' "))
    last = primary.psql("SELECT pg_walfile_name(pg_switch_wal())")  # needs superuser
    engine.dispose()
    _wait(lambda: any(p.name >= last for p in archive.iterdir()), 30, "WAL archived")
    primary.stop()

    # 4 · recover into a separate cluster; never over the live data directory
    restored = Cluster(workdir / "restored", port=free_port())
    shutil.copytree(base / "data", restored.datadir)
    if os.geteuid() == 0:
        pw = pwd.getpwnam("postgres")
        for p in [restored.datadir, *restored.datadir.rglob("*")]:
            os.chown(p, pw.pw_uid, pw.pw_gid)
    restored.datadir.chmod(0o700)
    with (restored.datadir / "postgresql.conf").open("a") as fh:
        fh.write(
            "\n# point-in-time recovery\n"
            f"port = {restored.port}\n"
            f"unix_socket_directories = '{restored.datadir}'\n"
            "archive_mode = off\n"
            f"restore_command = 'cp {archive}/%f %p'\n"
            f"recovery_target_time = '{target.isoformat()}'\n"
            "recovery_target_action = 'promote'\n"
        )
    (restored.datadir / "recovery.signal").touch()
    restored.start()
    _wait(lambda: restored.psql("SELECT pg_is_in_recovery()") == "f", 60, "promotion")

    r_engine = make_engine(restored.database_url(), pool_size=1)
    try:
        with r_engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM pitr_probe")).scalar_one() == 100
            assert (
                c.execute(text("SELECT count(*) FROM lab WHERE name = 'wrong'")).scalar_one() == 0
            )
            assert c.execute(text("SELECT count(*) FROM lab")).scalar_one() == labs_before
        assert migrate.current_revision(r_engine) == migrate.head_revision()
    finally:
        r_engine.dispose()
