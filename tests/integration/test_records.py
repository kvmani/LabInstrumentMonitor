"""RUN-01: user runs and annotations — roles, validation, audited corrections, retraction."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from labmon.contracts.envelope import format_utc
from labmon.server.models import AuditEvent
from labmon.server.security import CSRF_HEADER
from tests.conftest import FakeNow
from tests.integration.helpers import World, client_for, seed_world, sign_in

pytestmark = pytest.mark.integration
H = {CSRF_HEADER: "1"}


@pytest.fixture
def world(engine, t0: datetime) -> World:  # type: ignore[no-untyped-def]
    return seed_world(engine, t0 - timedelta(days=60))


def user(world: World, db_url: str, fake_now: FakeNow, name: str) -> TestClient:
    c = client_for(world, db_url, fake_now)
    sign_in(c, name)
    return c


def audits(world: World, action: str) -> list:
    with world.engine.connect() as c:
        return c.execute(
            select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id)
        ).all()


def test_scientist_creates_backdated_run_and_corrects_it_with_audit(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime
) -> None:
    alice = user(world, db_url, fake_now, "alice")
    body = {
        "name": "Anneal batch 7",
        "started_at": format_utc(t0 - timedelta(hours=5)),
        "notes": "coupons A1–A4",
        "tags": ["anneal", "batch-7"],
    }
    r = alice.post("/api/v1/instruments/furnace-01/runs", json=body, headers=H)
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["source"] == "user" and run["active"] and run["created_by"] == "alice"
    assert run["tags"] == ["anneal", "batch-7"] and run["simulated"] is True

    no_reason = alice.patch(
        f"/api/v1/runs/{run['id']}",
        headers=H,
        json={"ended_at": format_utc(t0 - timedelta(hours=1))},
    )
    assert no_reason.status_code == 422
    r = alice.patch(
        f"/api/v1/runs/{run['id']}",
        headers=H,
        json={
            "ended_at": format_utc(t0 - timedelta(hours=1)),
            "name": "Anneal batch 7 (850 °C)",
            "reason": "furnace log shows the end at 07:00",
        },
    )
    assert r.status_code == 200, r.text
    after = r.json()
    assert not after["active"] and after["end_reason"] == "completed"
    assert after["updated_at"] == format_utc(t0)
    [ev] = audits(world, "run_update")
    assert ev.detail["reason"] == "furnace log shows the end at 07:00"
    assert ev.detail["before"]["name"] == "Anneal batch 7"
    assert ev.detail["after"]["name"] == "Anneal batch 7 (850 °C)"
    assert len(audits(world, "run_create")) == 1
    listed = alice.get("/api/v1/instruments/furnace-01/runs").json()["items"]
    assert [x["id"] for x in listed] == [run["id"]]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"name": "x", "started_at": "2026-11-06T09:00:00Z"}, 422),  # in the future
        (
            {"name": "x", "started_at": "2026-11-06T07:00:00Z", "ended_at": "2026-11-06T06:00:00Z"},
            422,
        ),  # ends before it starts
        ({"name": "x", "started_at": "2026-11-06T07:00:00"}, 422),  # no offset
        ({"name": "", "started_at": "2026-11-06T07:00:00Z"}, 422),
        ({"name": "x", "started_at": "2026-11-06T07:00:00Z", "tags": ["<script>"]}, 422),
    ],
)
def test_run_validation(
    world: World, db_url: str, fake_now: FakeNow, body: dict, status: int
) -> None:
    alice = user(world, db_url, fake_now, "alice")
    assert (
        alice.post("/api/v1/instruments/furnace-01/runs", json=body, headers=H).status_code
        == status
    )


def test_roles_scope_and_csrf(world: World, db_url: str, fake_now: FakeNow, t0: datetime) -> None:
    body = {"name": "x", "started_at": format_utc(t0 - timedelta(hours=1))}
    carol = user(world, db_url, fake_now, "carol")  # viewer in lab-a
    assert (
        carol.post("/api/v1/instruments/furnace-01/runs", json=body, headers=H).status_code == 403
    )
    assert carol.get("/api/v1/instruments/furnace-01/runs").status_code == 200
    bob = user(world, db_url, fake_now, "bob")  # other lab
    assert bob.post("/api/v1/instruments/furnace-01/runs", json=body, headers=H).status_code == 404
    alice = user(world, db_url, fake_now, "alice")
    assert alice.post("/api/v1/instruments/furnace-01/runs", json=body).status_code == 403
    note = {"at": format_utc(t0 - timedelta(minutes=5)), "text": "door opened"}
    assert (
        carol.post("/api/v1/instruments/furnace-01/annotations", json=note, headers=H).status_code
        == 403
    )


def test_instrument_runs_keep_their_window(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime
) -> None:
    from sqlalchemy import insert

    from labmon.server.models import Run

    with world.engine.begin() as c:
        c.execute(
            insert(Run).values(
                id="furnace-01.c000001",
                instrument_id="furnace-01",
                run_key="c000001",
                name="Heat treatment #1",
                kind="heat_treatment",
                source="instrument",
                simulated=True,
                started_at=t0 - timedelta(hours=2),
                run_metadata={},
                first_seen_at=t0,
            )
        )
    alice = user(world, db_url, fake_now, "alice")
    bad = alice.patch(
        "/api/v1/runs/furnace-01.c000001",
        headers=H,
        json={"name": "renamed", "reason": "prefer my name"},
    )
    assert bad.status_code == 422
    ok = alice.patch(
        "/api/v1/runs/furnace-01.c000001",
        headers=H,
        json={"notes": "coupon 3 cracked", "reason": "observation"},
    )
    assert ok.status_code == 200 and ok.json()["notes"] == "coupon 3 cracked"
    assert ok.json()["name"] == "Heat treatment #1"


def test_annotation_lifecycle(world: World, db_url: str, fake_now: FakeNow, t0: datetime) -> None:
    alice = user(world, db_url, fake_now, "alice")
    at = t0 - timedelta(minutes=30)
    r = alice.post(
        "/api/v1/instruments/furnace-01/annotations",
        headers=H,
        json={
            "at": format_utc(at),
            "end_at": format_utc(at + timedelta(minutes=10)),
            "text": "thermocouple re-seated",
        },
    )
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["revision"] == 1 and a["author"] == "alice"
    rng = {"start": format_utc(t0 - timedelta(hours=1)), "end": format_utc(t0)}
    assert [
        x["id"]
        for x in alice.get("/api/v1/instruments/furnace-01/annotations", params=rng).json()["items"]
    ] == [a["id"]]
    # a window overlapping the range start is still returned
    late = {"start": format_utc(at + timedelta(minutes=5)), "end": format_utc(t0)}
    assert (
        len(alice.get("/api/v1/instruments/furnace-01/annotations", params=late).json()["items"])
        == 1
    )

    r = alice.patch(
        f"/api/v1/annotations/{a['id']}",
        headers=H,
        json={"text": "thermocouple TC2 re-seated", "reason": "name the probe"},
    )
    assert r.status_code == 200 and r.json()["revision"] == 2
    [ev] = audits(world, "annotation_update")
    assert ev.detail["before"]["text"] == "thermocouple re-seated"

    r = alice.post(
        f"/api/v1/annotations/{a['id']}/retract", headers=H, json={"reason": "wrong instrument"}
    )
    assert r.status_code == 200 and r.json()["retracted"]
    assert alice.get("/api/v1/instruments/furnace-01/annotations", params=rng).json()["items"] == []
    hidden = alice.get(
        "/api/v1/instruments/furnace-01/annotations", params={**rng, "include_retracted": "true"}
    ).json()["items"]
    assert hidden[0]["retract_reason"] == "wrong instrument"
    again = alice.patch(
        f"/api/v1/annotations/{a['id']}", headers=H, json={"text": "x", "reason": "try again"}
    )
    assert again.status_code == 409


def test_annotation_run_must_match_instrument(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime
) -> None:
    alice = user(world, db_url, fake_now, "alice")
    r = alice.post(
        "/api/v1/instruments/furnace-01/annotations",
        headers=H,
        json={"at": format_utc(t0 - timedelta(minutes=1)), "text": "x", "run_id": "furnace-b.c1"},
    )
    assert r.status_code == 422
