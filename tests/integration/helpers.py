"""Registry seeding and batch builders shared by integration tests."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from labmon.contracts.envelope import Envelope, EventType, RecordKind
from labmon.server.app import create_app
from labmon.server.bootstrap import (
    FURNACE_SEED,
    InstrumentSeed,
    assign,
    ensure_collector,
    ensure_instrument,
    ensure_lab,
    ensure_user,
)
from labmon.server.security import CSRF_HEADER, issue_collector_credential
from labmon.server.settings import Settings

PASSWORD = "correct horse battery"
EPOCH = 1000


@dataclass
class World:
    engine: Engine
    token: str
    other_token: str
    collector_id: str = "col-1"
    instrument_id: str = "furnace-01"
    revision_id: str = "furnace-01-r1"


def seed_world(engine: Engine, valid_from: datetime) -> World:
    """lab-a: furnace-01 on col-1 (user alice). lab-b: furnace-b on col-b (user bob)."""
    with engine.begin() as c:
        ensure_lab(c, "lab-a", "Lab A")
        ensure_lab(c, "lab-b", "Lab B")
        ensure_instrument(c, "lab-a", FURNACE_SEED, "test")
        ensure_instrument(
            c, "lab-b", InstrumentSeed("furnace-b", "Other furnace", "sim-furnace", 1), "test"
        )
        ensure_collector(c, "col-1", "lab-a", "collector 1")
        ensure_collector(c, "col-b", "lab-b", "collector b")
        assign(c, "col-1", "furnace-01", valid_from, "test")
        assign(c, "col-b", "furnace-b", valid_from, "test")
        token = issue_collector_credential(c, "col-1")
        other = issue_collector_credential(c, "col-b")
        ensure_user(c, "alice", "Alice", PASSWORD, {"lab-a": "scientist"})
        ensure_user(c, "bob", "Bob", PASSWORD, {"lab-b": "viewer"})
    return World(engine, token, other)


def settings_for(url: str) -> Settings:
    return Settings(database_url=url, environment="test", static_dir=None, cookie_secure=False)


def client_for(world: World, url: str, now, after_commit=None) -> TestClient:  # type: ignore[no-untyped-def]
    app = create_app(settings_for(url), engine=world.engine, now=now, after_commit=after_commit)
    return TestClient(app, raise_server_exceptions=os.environ.get("LABMON_TEST_RAISE") == "1")


def sign_in(client: TestClient, user: str = "alice") -> None:
    r = client.post(
        "/api/v1/session", json={"username": user, "password": PASSWORD}, headers={CSRF_HEADER: "1"}
    )
    assert r.status_code == 200, r.text


class Seq:
    def __init__(self, start: int = 1) -> None:
        self.n = start - 1

    def __call__(self) -> int:
        self.n += 1
        return self.n


def sample(
    seq: int,
    at: datetime,
    channel: str = "pv",
    value: object = 850.0,
    quality: str = "good",
    reason: str | None = None,
    collector: str = "col-1",
    instrument: str = "furnace-01",
    epoch: int = EPOCH,
    **kw: object,
) -> Envelope:
    return Envelope(
        kind=RecordKind.SAMPLE,
        collector_id=collector,
        spool_epoch=epoch,
        seq=seq,
        instrument_id=instrument,
        profile_revision_id=f"{instrument}-r1",
        channel_id=f"{instrument}.{channel}",
        value=value,
        quality=quality,  # type: ignore[arg-type]
        quality_reason=reason,
        capture_time=at,
        simulated=True,  # type: ignore[arg-type]
        frame_id=f"{instrument}:{int(at.timestamp())}",
        **kw,  # type: ignore[arg-type]
    )


def frame(
    seq: Seq, at: datetime, pv: float = 850.0, sp: float = 850.0, phase: int = 2
) -> list[Envelope]:
    return [
        sample(seq(), at, "pv", pv),
        sample(seq(), at, "sp", sp),
        sample(seq(), at, "phase", phase),
    ]


def event(seq: int, at: datetime, etype: EventType, detail: dict | None = None) -> Envelope:
    return Envelope(
        kind=RecordKind.EVENT,
        collector_id="col-1",
        spool_epoch=EPOCH,
        seq=seq,
        instrument_id="furnace-01",
        profile_revision_id="furnace-01-r1",
        capture_time=at,
        simulated=True,
        event_type=etype,
        detail=detail,
    )


def batch_body(
    records: list[Envelope], batch_id: str | None = None, collector: str = "col-1", attempt: int = 1
) -> tuple[str, bytes]:
    bid = batch_id or uuid.uuid4().hex
    body = {
        "v": 1,
        "batch_id": bid,
        "collector_id": collector,
        "attempt": attempt,
        "records": [json.loads(r.canonical()) for r in records],
    }
    return bid, json.dumps(body).encode()


def post_batch(client: TestClient, token: str, body: bytes, gzip_: bool = False):  # type: ignore[no-untyped-def]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if gzip_:
        import gzip

        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    return client.post("/api/v1/ingest/batches", content=body, headers=headers)


def frames(seq: Seq, start: datetime, n: int, step_s: float = 1.0, **kw: float) -> list[Envelope]:
    out: list[Envelope] = []
    for i in range(n):
        out += frame(seq, start + timedelta(seconds=i * step_s), **kw)  # type: ignore[arg-type]
    return out
