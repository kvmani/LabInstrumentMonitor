"""EXP-01 durable exports: manifest and checksums, leases, atomic publication, access, expiry."""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from labmon.contracts.envelope import format_utc
from labmon.server import exports
from labmon.server.security import CSRF_HEADER
from tests.conftest import FakeNow
from tests.integration.helpers import (
    Seq,
    World,
    batch_body,
    client_for,
    frames,
    post_batch,
    sample,
    seed_world,
    settings_for,
    sign_in,
)

pytestmark = pytest.mark.integration
H = {CSRF_HEADER: "1"}


@pytest.fixture
def env(engine, db_url: str, fake_now: FakeNow, t0: datetime, tmp_path: Path):  # type: ignore[no-untyped-def]
    world = seed_world(engine, t0 - timedelta(days=60))
    settings = dataclasses.replace(settings_for(db_url), export_dir=tmp_path / "exports")
    client = client_for(world, db_url, fake_now, settings=settings)
    sign_in(client)
    recs = frames(Seq(), t0 - timedelta(minutes=10), 120)  # 360 samples
    for i in range(0, len(recs), 200):
        assert post_batch(client, world.token, batch_body(recs[i : i + 200])[1]).status_code == 200
    bad = sample(
        9000,
        t0 - timedelta(minutes=5, seconds=30),
        value=None,
        quality="bad",
        reason="sensor_open",
        raw="=HYPERLINK(1)",
    )
    assert post_batch(client, world.token, batch_body([bad])[1]).status_code == 200
    return world, client, settings


def request_export(client: TestClient, t0: datetime, **extra: object) -> dict:
    body = {"start": format_utc(t0 - timedelta(hours=1)), "end": format_utc(t0), **extra}
    r = client.post("/api/v1/instruments/furnace-01/exports", json=body, headers=H)
    assert r.status_code == 202, r.text
    return r.json()  # type: ignore[no-any-return]


def run_worker(world: World, settings, fake_now: FakeNow) -> int:  # type: ignore[no-untyped-def]
    return exports.process_exports(world.engine, settings, "test-worker", fake_now)


def test_export_zip_manifest_and_checksums_agree(env, fake_now: FakeNow, t0: datetime) -> None:  # type: ignore[no-untyped-def]
    world, client, settings = env
    job = request_export(client, t0)
    assert job["status"] == "queued" and job["params"]["estimated_rows"] == 361
    assert run_worker(world, settings, fake_now) == 1
    view = client.get(f"/api/v1/exports/{job['id']}").json()
    assert view["status"] == "succeeded" and view["row_count"] == 361
    assert view["file_name"].startswith("SIMULATED_furnace-01_")
    r = client.get(f"/api/v1/exports/{job['id']}/download")
    assert r.status_code == 200
    assert hashlib.sha256(r.content).hexdigest() == view["sha256"] == r.headers["x-content-sha256"]
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(zf.namelist()) == ["data.csv", "manifest.json"]
    manifest = json.loads(zf.read("manifest.json"))
    data = zf.read("data.csv")
    assert hashlib.sha256(data).hexdigest() == manifest["files"]["data.csv"]["sha256"]
    rows = list(csv.DictReader(io.StringIO(data.decode())))
    assert len(rows) == manifest["counts"]["rows"] == 361
    assert manifest["counts"]["by_quality"] == {"good": 360, "bad": 1}
    assert manifest["counts"]["by_channel"] == {
        "furnace-01.pv": 121,
        "furnace-01.sp": 120,
        "furnace-01.phase": 120,
    }
    assert manifest["simulated"] is True and manifest["format"] == "labmon-export/1"
    assert manifest["range"]["interval"] == "[start,end)"
    assert manifest["cutoff"]["snapshot_xmin"]
    assert [r["id"] for r in manifest["profile_revisions"]] == ["furnace-01-r1"]
    assert [r["event_time_utc"] for r in rows] == sorted(r["event_time_utc"] for r in rows)
    bad = [r for r in rows if r["quality"] == "bad"][0]
    assert bad["value"] == "" and bad["raw"] == "'=HYPERLINK(1)"
    # no partial files left behind
    assert sorted(p.name for p in settings.export_dir.iterdir()) == [f"{job['id']}.zip"]


def test_channel_filter_and_validation(env, fake_now: FakeNow, t0: datetime) -> None:  # type: ignore[no-untyped-def]
    world, client, settings = env
    job = request_export(client, t0, channels=["pv"])
    run_worker(world, settings, fake_now)
    view = client.get(f"/api/v1/exports/{job['id']}").json()
    assert view["row_count"] == 121
    bad = [
        {"start": format_utc(t0), "end": format_utc(t0 - timedelta(hours=1))},
        {"start": format_utc(t0 - timedelta(days=40)), "end": format_utc(t0)},
        {"start": format_utc(t0 - timedelta(hours=1)), "end": format_utc(t0), "channels": ["nope"]},
    ]
    for body in bad:
        assert (
            client.post("/api/v1/instruments/furnace-01/exports", json=body, headers=H).status_code
            == 422
        )


def test_killed_worker_lease_expires_and_another_worker_finishes(
    env,
    fake_now: FakeNow,
    t0: datetime,  # type: ignore[no-untyped-def]
) -> None:
    world, client, settings = env
    job = request_export(client, t0)
    claimed = exports.claim_job(world.engine, "worker-a", fake_now(), settings.export_lease_s)
    assert claimed is not None and claimed.id == job["id"]
    # worker-a dies here; nothing is published and nobody else can claim before the lease ends
    assert exports.claim_job(world.engine, "worker-b", fake_now(), 300) is None
    assert client.get(f"/api/v1/exports/{job['id']}/download").status_code == 409
    fake_now.advance(settings.export_lease_s + 1)
    assert run_worker(world, settings, fake_now) == 1
    view = client.get(f"/api/v1/exports/{job['id']}").json()
    assert view["status"] == "succeeded" and view["attempts"] == 2


def test_failure_before_publication_leaves_no_file_and_retries(
    env,
    fake_now: FakeNow,
    t0: datetime,  # type: ignore[no-untyped-def]
) -> None:
    world, client, settings = env
    job = request_export(client, t0)

    def boom(_job_id: str) -> None:
        raise OSError("disk full")

    for attempt in range(1, settings.export_max_attempts + 1):
        claimed = exports.claim_job(world.engine, "w", fake_now(), 300)
        assert claimed is not None and claimed.attempts == attempt
        status = exports.run_claimed(world.engine, claimed, settings, fake_now, fault=boom)
        assert status == ("failed" if attempt == settings.export_max_attempts else "queued")
    view = client.get(f"/api/v1/exports/{job['id']}").json()
    assert view["status"] == "failed" and "disk full" in view["error"]
    assert client.get(f"/api/v1/exports/{job['id']}/download").status_code == 409
    leftovers = [p.name for p in settings.export_dir.iterdir() if p.name.startswith(".")]
    assert leftovers == []  # partial files are replaced or never published


def test_access_is_rechecked_at_download_and_files_expire(
    env,
    db_url: str,
    fake_now: FakeNow,
    t0: datetime,  # type: ignore[no-untyped-def]
) -> None:
    world, client, settings = env
    job = request_export(client, t0)
    run_worker(world, settings, fake_now)
    other = client_for(world, db_url, fake_now, settings=settings)
    sign_in(other, "carol")  # same lab, different user: not her export
    assert other.get(f"/api/v1/exports/{job['id']}").status_code == 404
    with world.engine.begin() as c:  # alice loses access to the lab after requesting
        c.execute(
            text(
                "DELETE FROM lab_grant WHERE user_id = "
                "(SELECT id FROM app_user WHERE username = 'alice')"
            )
        )
    assert client.get(f"/api/v1/exports/{job['id']}/download").status_code == 404
    with world.engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO lab_grant SELECT id, 'lab-a', 'scientist' FROM app_user "
                "WHERE username = 'alice'"
            )
        )
    assert client.get(f"/api/v1/exports/{job['id']}/download").status_code == 200
    fake_now.advance(settings.export_retention_days * 86400 + 1)
    sign_in(client)  # the 12 h session expired too
    assert client.get(f"/api/v1/exports/{job['id']}/download").status_code == 410
    run_worker(world, settings, fake_now)  # expiry pass deletes the file
    assert client.get(f"/api/v1/exports/{job['id']}").json()["status"] == "expired"
    assert not exports.export_path(settings, job["id"]).exists()
    listed = client.get("/api/v1/exports", params={"instrument_id": "furnace-01"}).json()
    assert [x["id"] for x in listed["items"]] == [job["id"]]
