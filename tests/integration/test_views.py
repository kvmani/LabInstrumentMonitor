"""HIST-01 saved views: personal views stay private; lab templates need custodian/admin."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from labmon.server.security import CSRF_HEADER
from tests.conftest import FakeNow
from tests.integration.helpers import World, client_for, seed_world, sign_in

pytestmark = pytest.mark.integration
H = {CSRF_HEADER: "1"}
CFG = {"preset": "24h", "scales": {"temperature": {"mode": "fixed", "min": 0, "max": 900}}}


@pytest.fixture
def world(engine, t0: datetime) -> World:  # type: ignore[no-untyped-def]
    w = seed_world(engine, t0 - timedelta(days=60))
    with engine.begin() as c:  # make bob a custodian of lab-a as well
        c.execute(
            text(
                "INSERT INTO lab_grant SELECT id, 'lab-a', 'custodian' FROM app_user "
                "WHERE username = 'bob'"
            )
        )
    return w


def login(world: World, db_url: str, fake_now: FakeNow, user: str):  # type: ignore[no-untyped-def]
    c = client_for(world, db_url, fake_now)
    sign_in(c, user)
    return c


def test_personal_views_are_private_and_templates_are_shared(
    world: World, db_url: str, fake_now: FakeNow
) -> None:
    alice = login(world, db_url, fake_now, "alice")  # scientist
    bob = login(world, db_url, fake_now, "bob")  # custodian
    carol = login(world, db_url, fake_now, "carol")  # viewer
    url = "/api/v1/instruments/furnace-01/views"
    mine = alice.post(url, json={"name": "my 24 h", "config": CFG}, headers=H)
    assert mine.status_code == 201, mine.text
    assert (
        alice.post(url, json={"name": "lab", "scope": "lab", "config": CFG}, headers=H).status_code
        == 403
    )
    tpl = bob.post(
        url, json={"name": "Soak check", "scope": "lab", "config": {"preset": "1h"}}, headers=H
    )
    assert tpl.status_code == 201
    assert (
        carol.post(
            url, json={"name": "viewer view", "config": {"preset": "15m"}}, headers=H
        ).status_code
        == 201
    )  # personal views are fine for viewers
    names = lambda c: sorted(v["name"] for v in c.get(url).json()["items"])  # noqa: E731
    assert names(alice) == ["Soak check", "my 24 h"]
    assert names(carol) == ["Soak check", "viewer view"]
    assert alice.get(url).json()["items"][1]["config"]["scales"]["temperature"]["max"] == 900
    # deleting: own personal view yes; others' personal no; template only custodian/admin
    assert carol.delete(f"/api/v1/views/{mine.json()['id']}", headers=H).status_code == 404
    assert alice.delete(f"/api/v1/views/{tpl.json()['id']}", headers=H).status_code == 403
    assert bob.delete(f"/api/v1/views/{tpl.json()['id']}", headers=H).status_code == 200
    assert alice.delete(f"/api/v1/views/{mine.json()['id']}", headers=H).status_code == 200
    assert names(alice) == []


@pytest.mark.parametrize(
    "config",
    [
        {},  # neither preset nor window
        {"preset": "2h"},
        {"preset": "1h", "start": "2026-11-06T06:00:00Z", "end": "2026-11-06T07:00:00Z"},
        {"start": "2026-11-06T07:00:00Z", "end": "2026-11-06T06:00:00Z"},
        {"preset": "1h", "scales": {"Bad Key": {"mode": "auto"}}},
        {"preset": "1h", "scales": {"t": {"mode": "fixed", "min": "nan"}}},
        {"preset": "1h", "script": "alert(1)"},
    ],
)
def test_view_validation(world: World, db_url: str, fake_now: FakeNow, config: dict) -> None:
    alice = login(world, db_url, fake_now, "alice")
    r = alice.post(
        "/api/v1/instruments/furnace-01/views", json={"name": "x", "config": config}, headers=H
    )
    assert r.status_code == 422
