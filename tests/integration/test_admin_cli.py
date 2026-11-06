"""Provision a lab, user, collector and simulated instrument with the admin CLI only."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from labmon.collector.config import load_config
from labmon.server.cli import main
from labmon.server.models import AuditEvent
from labmon.server.security import CSRF_HEADER
from tests.conftest import FakeNow
from tests.integration.helpers import World, batch_body, client_for, post_batch, sample

pytestmark = pytest.mark.integration
PASSWORD = "a long admin-set password"


@pytest.fixture
def cli(db_url: str, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LABMON_DATABASE_URL", db_url)
    monkeypatch.setenv("LABMON_ENV", "production")  # admin works without dev tools

    def run(*args: str, stdin: str = "") -> int:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        return main(["admin", *args])

    return run


def test_provisioning_end_to_end(
    cli,
    engine: Engine,
    db_url: str,
    fake_now: FakeNow,  # type: ignore[no-untyped-def]
    t0: datetime,
    tmp_path: Path,
) -> None:
    assert cli("lab-add", "metals", "Metals lab") == 0
    assert cli("lab-add", "metals", "again") == 2  # duplicates refused
    assert (
        cli("user-add", "dana", "--display-name", "Dana", "--password-stdin", stdin=PASSWORD + "\n")
        == 0
    )
    assert cli("grant", "dana", "metals", "superuser") == 2
    assert cli("grant", "dana", "metals", "scientist") == 0
    assert cli("collector-add", "col-m1", "--lab", "metals", "--name", "Metals PC") == 0
    assert (
        cli(
            "instrument-enrol",
            "utm-m1",
            "--lab",
            "metals",
            "--name",
            "UTM M1",
            "--profile",
            "sim-utm",
            "--seed",
            "5",
            "--collector",
            "col-m1",
        )
        == 0
    )
    token = tmp_path / "collector.token"
    assert cli("collector-token", "col-m1", "--out", str(token)) == 0
    assert oct(token.stat().st_mode & 0o777) == "0o600"
    assert (
        cli(
            "collector-config",
            "col-m1",
            "--server-url",
            "http://127.0.0.1:9",
            "--out-dir",
            str(tmp_path),
        )
        == 0
    )
    cfg = load_config(tmp_path / "collector.toml")
    assert [i.instrument_id for i in cfg.instruments] == ["utm-m1"]
    assert cfg.instruments[0].seed == 5 and cfg.read_token() == token.read_text().strip()

    # the provisioned user signs in and sees only the new lab's instrument
    world = World(engine, cfg.read_token(), "")
    client = client_for(world, db_url, fake_now)
    r = client.post(
        "/api/v1/session",
        json={"username": "dana", "password": PASSWORD},
        headers={CSRF_HEADER: "1"},
    )
    assert r.status_code == 200 and r.json()["labs"] == {"metals": "scientist"}
    ids = [i["instrument"]["id"] for i in client.get("/api/v1/instruments").json()["items"]]
    assert ids == ["utm-m1"]
    # the provisioned collector can upload
    fake_now.t = datetime.now(UTC)  # enrolment starts the assignment at the real time
    rec = sample(
        1, fake_now.t, channel="force", value=0.01, collector="col-m1", instrument="utm-m1"
    )
    assert (
        post_batch(client, cfg.read_token(), batch_body([rec], collector="col-m1")[1]).status_code
        == 200
    )

    # rotation revokes the old credential; revocation stops the collector
    old = cfg.read_token()
    assert cli("collector-token", "col-m1", "--out", str(token)) == 0
    assert post_batch(client, old, batch_body([rec], collector="col-m1")[1]).status_code == 401
    assert cli("collector-revoke", "col-m1") == 0
    assert (
        post_batch(
            client, token.read_text().strip(), batch_body([rec], collector="col-m1")[1]
        ).status_code
        == 401
    )

    # disabling a user ends their sessions at once
    assert cli("user-disable", "dana") == 0
    assert client.get("/api/v1/instruments").status_code == 401

    with engine.connect() as c:
        actions = [a for (a,) in c.execute(select(AuditEvent.action)) if not a.startswith("login")]
    assert {
        "lab_add",
        "user_add",
        "grant",
        "collector_add",
        "instrument_enrol",
        "collector_token_issue",
        "collector_revoke",
        "user_disable",
    } <= set(actions)
