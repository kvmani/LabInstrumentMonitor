"""Backup, verified restore into a scratch database, corrupt-transfer refusal, freshness."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from labmon.server import backup
from labmon.server.diagnostics import system_health
from tests.conftest import FakeNow, PgServer
from tests.integration.helpers import (
    Seq,
    World,
    batch_body,
    client_for,
    frames,
    post_batch,
    seed_world,
    settings_for,
)

pytestmark = pytest.mark.integration


def test_backup_restore_check_and_freshness(
    engine,
    db_url: str,
    pg_server: PgServer,  # type: ignore[no-untyped-def]
    fake_now: FakeNow,
    t0: datetime,
    tmp_path: Path,
) -> None:
    world: World = seed_world(engine, t0 - timedelta(days=60))
    client = client_for(world, db_url, fake_now)
    assert (
        post_batch(
            client, world.token, batch_body(frames(Seq(), t0 - timedelta(minutes=5), 50))[1]
        ).status_code
        == 200
    )
    status_file = tmp_path / "status" / "backup-status.json"
    out = tmp_path / "backups"
    st = backup.backup(db_url, out, keep_days=14, status_file=status_file)
    assert st["ok"] and st["counts"]["sample"] == 150
    dump = Path(st["file"])
    assert dump.exists() and not list(out.glob("*.partial"))
    assert json.loads(status_file.read_text())["sha256"] == st["sha256"]

    result = backup.restore_check(dump, pg_server.admin_url)
    assert result["ok"] and result["mismatches"] == {}
    assert result["counts"]["sample"] == 150

    corrupt = out / "corrupt.dump"
    corrupt.write_bytes(dump.read_bytes()[:-10] + b"0123456789")
    for suffix in (".sha256", ".counts.json"):
        (out / ("corrupt.dump" + suffix)).write_text((out / (dump.name + suffix)).read_text())
    with pytest.raises(backup.BackupError, match="checksum mismatch"):
        backup.restore_check(corrupt, pg_server.admin_url)

    settings = dataclasses.replace(
        settings_for(db_url), backup_status_file=status_file, export_dir=tmp_path / "exports"
    )
    with engine.connect() as c:
        fresh = system_health(c, settings, datetime.now(UTC))
        stale = system_health(c, settings, datetime.now(UTC) + timedelta(hours=30))
    assert fresh["backup"]["ok"] and "backup_stale" not in [f["code"] for f in fresh["faults"]]
    assert "backup_stale" in [f["code"] for f in stale["faults"]]
    unset = dataclasses.replace(settings, backup_status_file=None)
    with engine.connect() as c:
        codes = [f["code"] for f in system_health(c, unset, datetime.now(UTC))["faults"]]
    assert "backups_not_monitored" in codes
