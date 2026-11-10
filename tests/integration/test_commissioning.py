"""ONB-01 commissioning wizard and collector provisioning through the API (real PostgreSQL)."""

from __future__ import annotations

import dataclasses
import io
import tomllib
import zipfile
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from labmon.contracts.envelope import Envelope, RecordKind, format_utc
from labmon.server.bootstrap import ensure_user
from labmon.server.models import AuditEvent, CollectorAssignment, ProfileRevision
from labmon.server.security import CSRF_HEADER
from tests.conftest import FakeNow
from tests.integration.helpers import (
    PASSWORD,
    World,
    batch_body,
    client_for,
    post_batch,
    seed_world,
    settings_for,
    sign_in,
)

pytestmark = pytest.mark.integration
H = {CSRF_HEADER: "1"}
NEW = "dil-77"


@pytest.fixture
def world(engine, t0: datetime) -> World:  # type: ignore[no-untyped-def]
    w = seed_world(engine, t0 - timedelta(days=1))
    with engine.begin() as c:
        ensure_user(c, "dana", "Dana (custodian)", PASSWORD, {"lab-a": "custodian"})
        ensure_user(c, "erin", "Erin (administrator)", PASSWORD, {"lab-a": "administrator"})
    return w


def as_user(world: World, db_url: str, fake_now: FakeNow, name: str, **kw) -> TestClient:  # type: ignore[no-untyped-def]
    c = client_for(world, db_url, fake_now, **kw)
    sign_in(c, name)
    return c


def heartbeat(c: TestClient, token: str, at: datetime, sources: list[dict[str, str]]) -> int:
    body = {
        "v": 1,
        "collector_time": format_utc(at),
        "software_version": "t",
        "spool_epoch": 1000,
        "next_seq": 1,
        "unacknowledged": 0,
        "quarantined": 0,
        "spool_capacity": 100,
        "sampling": True,
        "sources": sources,
    }
    return c.post(
        "/api/v1/collectors/heartbeat", json=body, headers={"Authorization": f"Bearer {token}"}
    ).status_code


def new_draft(c: TestClient, instrument_id: str = NEW, **data: object) -> dict:
    r = c.post(
        "/api/v1/commissioning/drafts",
        json={"lab_id": "lab-a", "instrument_id": instrument_id, "data": data},
        headers=H,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_custodian_commissions_a_simulated_instrument_end_to_end(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime
) -> None:
    dana = as_user(world, db_url, fake_now, "dana")
    opts = dana.get("/api/v1/commissioning/options").json()
    assert [lab["id"] for lab in opts["labs"]] == ["lab-a"]
    assert "sim-dilatometer" in [p["key"] for p in opts["profiles"]]
    assert "sim-ambient" not in [p["key"] for p in opts["profiles"]]
    assert [c["id"] for c in opts["collectors"]] == ["col-1"]  # lab-b's collector is invisible
    assert opts["can_provision"] == []

    # 1 Identify
    d = new_draft(dana, name="Vacuum dilatometer 77", location="Bay 3", asset_id="A-77")
    assert d["step"] == 1 and d["state"] == "draft" and d["existing"] is None
    assert set(d["missing"]) == {"collector_id", "profile", "seed"}
    # 2 Connect (and an interrupted session: a fresh sign-in resumes the same draft)
    r = dana.patch(
        f"/api/v1/commissioning/drafts/{d['id']}",
        json={
            "step": 3,
            "data": {
                **d["data"],
                "collector_id": "col-1",
                "profile": "sim-dilatometer",
                "seed": 77,
                "custodian": "dana",
                "interface_ref": "simulator ADR 0004",
            },
        },
        headers=H,
    )
    assert r.status_code == 200, r.text
    again = as_user(world, db_url, fake_now, "dana")
    [resumed] = again.get("/api/v1/commissioning/drafts").json()["items"]
    assert resumed["id"] == d["id"] and resumed["step"] == 3 and resumed["missing"] == []

    # publish refuses without a preview
    r = again.post(f"/api/v1/commissioning/drafts/{d['id']}/publish", headers=H)
    assert r.status_code == 422 and "preview" in r.json()["detail"]
    # 4 Preview: deterministic simulator, collector's range rule, dropout drill
    pv = again.post(f"/api/v1/commissioning/drafts/{d['id']}/preview", headers=H).json()
    assert pv["ok"] and pv["simulated"] and pv["seed"] == 77
    assert {c["name"] for c in pv["checks"]} == {
        "source answers",
        "channel map",
        "range and units",
        "dropout drill",
    }
    assert {c["key"] for c in pv["channels"]} >= {"pressure", "temperature"}
    assert pv["drill"]["unavailable_frames"] > 0
    # 5 Approve
    view = again.get(f"/api/v1/commissioning/drafts/{d['id']}").json()
    assert view["preview"]["ok"] and not view["preview"]["stale"]
    assert {c["field"] for c in view["changes"]} >= {"name", "profile", "seed", "collector_id"}
    r = again.post(f"/api/v1/commissioning/drafts/{d['id']}/publish", headers=H)
    assert r.status_code == 200, r.text
    pub = r.json()
    assert pub["state"] == "published" and pub["published_revision_id"] == f"{NEW}-r1"
    with world.engine.connect() as c:
        rev = c.execute(select(ProfileRevision).where(ProfileRevision.id == f"{NEW}-r1")).one()
        assert rev.definition["seed"] == 77 and rev.created_by == "user:dana"
        assert (
            c.execute(
                select(CollectorAssignment.collector_id).where(
                    CollectorAssignment.instrument_id == NEW, CollectorAssignment.valid_to.is_(None)
                )
            ).scalar()
            == "col-1"
        )
        [ev] = c.execute(
            select(AuditEvent).where(AuditEvent.action == "instrument_commission")
        ).all()
        assert ev.target == NEW and ev.detail["simulated"] is True
    # the new instrument is in the fleet, still without data
    items = again.get("/api/v1/instruments").json()["items"]
    assert NEW in [i["instrument"]["id"] for i in items]

    # 6 Verify: activation only after the collector applies the revision and samples commit
    verify = f"/api/v1/commissioning/drafts/{d['id']}/verify"
    assert again.get(verify).json()["state"] == "awaiting_collector"
    old = [{"instrument_id": "furnace-01", "profile_revision_id": "furnace-01-r1", "state": "ok"}]
    assert heartbeat(again, world.token, t0, old) == 200
    assert again.get(verify).json()["state"] == "awaiting_collector"
    applied = [*old, {"instrument_id": NEW, "profile_revision_id": f"{NEW}-r1", "state": "ok"}]
    assert heartbeat(again, world.token, t0, applied) == 200
    v = again.get(verify).json()
    assert v["state"] == "awaiting_samples" and v["collector"]["applied_revision_id"] == f"{NEW}-r1"
    recs = [
        Envelope(
            kind=RecordKind.SAMPLE,
            collector_id="col-1",
            spool_epoch=1000,
            seq=i + 1,
            instrument_id=NEW,
            profile_revision_id=f"{NEW}-r1",
            channel_id=f"{NEW}.temperature",
            value=25.0 + i,
            quality="good",  # type: ignore[arg-type]
            capture_time=t0 + timedelta(seconds=i),
            simulated=True,  # type: ignore[arg-type]
            frame_id=f"{NEW}:{i}",
        )
        for i in range(3)
    ]
    _, body = batch_body(recs)
    assert post_batch(again, world.token, body).status_code == 200
    v = again.get(verify).json()
    assert v["state"] == "receiving" and v["samples"]["count"] == 3
    [latest] = v["latest"]  # only the temperature channel has samples
    assert latest["channel_id"] == f"{NEW}.temperature" and latest["value"] == 27.0

    record = again.get(f"/api/v1/commissioning/drafts/{d['id']}/record").json()
    assert record["evidence"].startswith("simulated")
    assert record["revision"]["id"] == f"{NEW}-r1" and record["instrument"]["asset_id"] == "A-77"
    assert record["verification"]["state"] == "receiving"
    assert [e["action"] for e in record["audit"]] == [
        "commissioning_draft_create",
        "instrument_commission",
    ]


def test_roles_and_scope(world: World, db_url: str, fake_now: FakeNow) -> None:
    alice = as_user(world, db_url, fake_now, "alice")  # scientist: may record, not commission
    r = alice.post(
        "/api/v1/commissioning/drafts",
        json={"lab_id": "lab-a", "instrument_id": "x-1"},
        headers=H,
    )
    assert r.status_code == 403
    bob = as_user(world, db_url, fake_now, "bob")  # other lab
    assert (
        bob.post(
            "/api/v1/commissioning/drafts",
            json={"lab_id": "lab-a", "instrument_id": "x-1"},
            headers=H,
        ).status_code
        == 404
    )
    dana = as_user(world, db_url, fake_now, "dana")
    d = new_draft(dana, "x-1")
    assert bob.get(f"/api/v1/commissioning/drafts/{d['id']}").status_code == 404
    assert alice.get(f"/api/v1/commissioning/drafts/{d['id']}").status_code == 403
    assert bob.get("/api/v1/collectors").json()["items"] == []
    # one open draft per instrument; another lab's instrument id is reported as taken
    assert (
        dana.post(
            "/api/v1/commissioning/drafts",
            json={"lab_id": "lab-a", "instrument_id": "x-1"},
            headers=H,
        ).status_code
        == 409
    )
    assert (
        dana.post(
            "/api/v1/commissioning/drafts",
            json={"lab_id": "lab-a", "instrument_id": "furnace-b"},
            headers=H,
        ).status_code
        == 409
    )
    # writes need the CSRF header
    assert dana.post(f"/api/v1/commissioning/drafts/{d['id']}/abandon").status_code == 403
    assert (
        dana.post(f"/api/v1/commissioning/drafts/{d['id']}/abandon", headers=H).json()["state"]
        == "abandoned"
    )
    # custodians commission but do not provision collectors
    assert (
        dana.post(
            "/api/v1/collectors",
            json={"collector_id": "col-9", "lab_id": "lab-a", "name": "x"},
            headers=H,
        ).status_code
        == 403
    )
    assert (
        dana.post(
            "/api/v1/collectors/col-1/package", json={"server_url": "https://m.example"}, headers=H
        ).status_code
        == 403
    )


def test_stale_preview_and_recommission_rules(world: World, db_url: str, fake_now: FakeNow) -> None:
    dana = as_user(world, db_url, fake_now, "dana")
    d = new_draft(dana, "furnace-01")  # re-commission starts from the active revision
    assert d["existing"]["revision"] == 1 and d["data"]["profile"] == "sim-furnace"
    assert d["changes"] == []
    url = f"/api/v1/commissioning/drafts/{d['id']}"
    assert dana.post(f"{url}/preview", headers=H).json()["ok"]
    r = dana.post(f"{url}/publish", headers=H)
    assert r.status_code == 409 and "nothing changed" in r.json()["detail"]
    dana.patch(url, json={"data": {**d["data"], "seed": 99}}, headers=H)
    assert dana.get(url).json()["preview"]["stale"] is True
    assert dana.post(f"{url}/publish", headers=H).status_code == 422
    dana.patch(url, json={"data": {**d["data"], "profile": "sim-utm"}}, headers=H)
    dana.post(f"{url}/preview", headers=H)
    r = dana.post(f"{url}/publish", headers=H)
    assert r.status_code == 409 and "different profile" in r.json()["detail"]
    dana.patch(url, json={"data": {**d["data"], "seed": 99}}, headers=H)
    dana.post(f"{url}/preview", headers=H)
    r = dana.post(f"{url}/publish", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["published_revision_id"] == "furnace-01-r2"
    with world.engine.connect() as c:
        revs = c.execute(
            select(ProfileRevision.id, ProfileRevision.definition)
            .where(ProfileRevision.instrument_id == "furnace-01")
            .order_by(ProfileRevision.revision)
        ).all()
    assert [r.id for r in revs] == ["furnace-01-r1", "furnace-01-r2"]  # r1 is kept, immutable
    assert revs[1].definition["seed"] == 99


def test_administrator_provisions_packages_and_revokes_collectors(
    world: World, db_url: str, fake_now: FakeNow
) -> None:
    erin = as_user(world, db_url, fake_now, "erin")
    r = erin.post(
        "/api/v1/collectors",
        json={"collector_id": "col-9", "lab_id": "lab-a", "name": "Bay 9 PC"},
        headers=H,
    )
    assert r.status_code == 201 and r.json()["assigned_instruments"] == []
    # a collector without instruments has nothing to configure yet
    assert (
        erin.post(
            "/api/v1/collectors/col-9/package", json={"server_url": "https://m.example"}, headers=H
        ).status_code
        == 409
    )

    bad = ["https://m.example/path", "ftp://m.example", "https://u:p@m.example", "https://"]
    for u in bad:
        assert (
            erin.post(
                "/api/v1/collectors/col-1/package", json={"server_url": u}, headers=H
            ).status_code
            == 422
        ), u

    r = erin.post(
        "/api/v1/collectors/col-1/package",
        json={"server_url": "https://monitor.lab.example:8443"},
        headers=H,
    )
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["README.txt", "collector.toml"]
    cfg = tomllib.loads(z.read("collector.toml").decode())
    assert cfg["server_url"] == "https://monitor.lab.example:8443"
    assert cfg["allow_insecure_http"] is False
    assert [i["instrument_id"] for i in cfg["instruments"]] == ["furnace-01"]
    assert heartbeat(erin, world.token, fake_now(), []) == 200  # config only: token untouched

    r = erin.post(
        "/api/v1/collectors/col-1/package",
        json={"server_url": "https://monitor.lab.example", "issue_token": True},
        headers=H,
    )
    z = zipfile.ZipFile(io.BytesIO(r.content))
    token = z.read("collector.token").decode().strip()
    assert token.startswith("lmc_") and token != world.token
    assert heartbeat(erin, world.token, fake_now(), []) == 401  # previous credential revoked
    assert heartbeat(erin, token, fake_now(), []) == 200
    with world.engine.connect() as c:
        events = c.execute(select(AuditEvent).where(AuditEvent.action == "collector_package")).all()
    assert [e.detail["token_issued"] for e in events] == [False, True]
    assert all(token not in str(e.detail) for e in events)  # never audited in clear

    r = erin.post("/api/v1/collectors/col-1/revoke", json={"reason": "PC retired"}, headers=H)
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert heartbeat(erin, token, fake_now(), []) == 401


def test_production_requires_https_packages(world: World, db_url: str, fake_now: FakeNow) -> None:
    prod = dataclasses.replace(settings_for(db_url), environment="production")
    erin = client_for(world, db_url, fake_now, settings=prod)
    r = erin.post("/api/v1/session", json={"username": "erin", "password": PASSWORD}, headers=H)
    assert r.status_code == 200
    # production cookies are Secure, so TestClient over http would drop them; send explicitly
    erin.cookies.set("labmon_session", r.cookies.get("labmon_session") or "")
    r = erin.post(
        "/api/v1/collectors/col-1/package", json={"server_url": "http://m.example"}, headers=H
    )
    assert r.status_code == 422 and "HTTPS" in r.json()["detail"]
