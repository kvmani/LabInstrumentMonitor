"""Status freshness rules, scoped history with gaps and buckets, CSV, sessions and scope checks."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from labmon.contracts.envelope import EventType, format_utc
from labmon.server.models import AuditEvent
from labmon.server.security import CSRF_HEADER
from tests.conftest import FakeNow
from tests.integration.helpers import (
    PASSWORD,
    Seq,
    World,
    batch_body,
    client_for,
    event,
    frames,
    post_batch,
    sample,
    seed_world,
    sign_in,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def world(engine, t0: datetime) -> World:  # type: ignore[no-untyped-def]
    return seed_world(engine, t0 - timedelta(days=60))


@pytest.fixture
def client(world: World, db_url: str, fake_now: FakeNow) -> TestClient:
    c = client_for(world, db_url, fake_now)
    sign_in(c)
    return c


def heartbeat(client: TestClient, token: str, at: datetime, **kw: object) -> None:
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
        "sources": [{"instrument_id": "furnace-01", "state": "ok"}],
        **kw,
    }
    r = client.post(
        "/api/v1/collectors/heartbeat", json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text


def status(client: TestClient) -> dict:
    r = client.get("/api/v1/instruments/furnace-01/status")
    assert r.status_code == 200, r.text
    return r.json()


def send(client: TestClient, world: World, recs) -> None:  # type: ignore[no-untyped-def]
    r = post_batch(client, world.token, batch_body(recs)[1])
    assert r.status_code == 200, r.text


# --- status ---------------------------------------------------------------------------------------


def test_waiting_before_any_sample(client: TestClient, world: World, fake_now: FakeNow) -> None:
    s = status(client)
    assert s["acquisition"]["state"] == "waiting"
    assert s["collector"]["connectivity"] == "never_seen"
    assert s["process_state"]["state"] == "unknown"
    assert s["instrument"]["simulated"] is True


def test_equal_readings_arriving_on_time_stay_receiving(
    client: TestClient, world: World, fake_now: FakeNow, t0: datetime
) -> None:
    seq = Seq()
    for i in range(10):  # an identical, valid temperature every second
        fake_now.t = t0 + timedelta(seconds=i)
        heartbeat(client, world.token, fake_now.t)
        send(client, world, frames(seq, fake_now.t, 1, pv=850.0))
        s = status(client)
        assert s["acquisition"]["state"] == "receiving", (i, s["acquisition"])
        assert s["collector"]["connectivity"] == "online"
    pv = s["channels"][0]
    assert pv["latest"]["value"] == 850.0 and pv["latest"]["age_s"] == 0
    assert s["process_state"] == {
        "state": "Soak",
        "code": 2,
        "source": "channel:phase",
        "since_check": format_utc(fake_now.t),
    }


def test_stale_and_offline_reasons(
    client: TestClient, world: World, fake_now: FakeNow, t0: datetime
) -> None:
    heartbeat(client, world.token, t0)
    send(client, world, frames(Seq(), t0, 1))
    fake_now.advance(6)  # beyond max(3 x 1 s, 5 s)
    heartbeat(client, world.token, fake_now.t)  # heartbeat alone cannot refresh freshness
    s = status(client)
    assert s["acquisition"]["state"] == "stale"
    assert s["acquisition"]["reason_code"] == "collector_online_no_new_readings"
    assert s["channels"][0]["last_good"]["event_time"] == format_utc(t0)
    assert s["process_state"]["state"] == "unknown"
    fake_now.advance(31)
    s = status(client)
    assert s["collector"]["connectivity"] == "offline"
    assert s["acquisition"]["reason_code"] == "collector_offline"
    assert s["acquisition"]["last_good_at"] == format_utc(t0)


def test_delivery_delayed_and_source_unavailable(
    client: TestClient, world: World, fake_now: FakeNow, t0: datetime
) -> None:
    send(client, world, frames(Seq(), t0, 1))
    fake_now.advance(20)
    # records merely in flight (oldest captured 1 s before the heartbeat) are not a delay
    heartbeat(
        client,
        world.token,
        fake_now.t,
        unacknowledged=12,
        oldest_pending_capture_time=format_utc(fake_now.t - timedelta(seconds=1)),
    )
    assert status(client)["acquisition"]["reason_code"] == "collector_online_no_new_readings"
    # an old backlog is evidence that sampling continues but delivery is delayed
    heartbeat(
        client,
        world.token,
        fake_now.t,
        unacknowledged=40,
        oldest_pending_capture_time=format_utc(fake_now.t - timedelta(seconds=19)),
    )
    assert status(client)["acquisition"]["reason_code"] == "collector_sampling_delivery_delayed"
    heartbeat(
        client,
        world.token,
        fake_now.t,
        sources=[{"instrument_id": "furnace-01", "state": "unavailable"}],
    )
    a = status(client)["acquisition"]
    assert (a["state"], a["reason_code"]) == ("source_unavailable", "collector_online_no_response")


def test_replayed_history_cannot_make_status_fresh(
    client: TestClient, world: World, fake_now: FakeNow, t0: datetime
) -> None:
    heartbeat(client, world.token, t0)
    # an hour of backlog delivered now: committed now, but measured long ago
    send(client, world, frames(Seq(), t0 - timedelta(hours=1), 100))
    s = status(client)
    assert s["acquisition"]["state"] == "stale"
    assert s["channels"][0]["latest"]["receive_lag_s"] > 3000


def test_bad_quality_is_degraded_and_last_good_is_kept(
    client: TestClient, world: World, fake_now: FakeNow, t0: datetime
) -> None:
    heartbeat(client, world.token, t0)
    send(client, world, frames(Seq(), t0 - timedelta(seconds=2), 1, pv=849.5))
    send(
        client, world, [sample(10, t0, value=None, quality="bad", reason="sensor_open", raw="OPEN")]
    )
    s = status(client)
    assert s["acquisition"]["state"] == "degraded"
    assert s["acquisition"]["channels"] == [{"key": "pv", "quality_reason": "sensor_open"}]
    pv = s["channels"][0]
    assert pv["latest"]["value"] is None and pv["latest"]["quality"] == "bad"
    assert pv["last_good"]["value"] == 849.5


def test_suspect_clock_does_not_count_as_fresh(
    client: TestClient, world: World, fake_now: FakeNow, t0: datetime
) -> None:
    heartbeat(client, world.token, t0)
    send(client, world, frames(Seq(), t0 + timedelta(minutes=10), 1))
    assert status(client)["acquisition"]["state"] == "waiting"


# --- history --------------------------------------------------------------------------------------


def series(
    client: TestClient, start: datetime, end: datetime, ch: str = "furnace-01.pv", **params: object
) -> dict:
    r = client.get(
        f"/api/v1/channels/{ch}/series",
        params={"start": format_utc(start), "end": format_utc(end), **params},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_raw_series_with_explicit_gaps(
    client: TestClient, world: World, fake_now: FakeNow, t0: datetime
) -> None:
    seq = Seq()
    start = t0 - timedelta(minutes=10)
    send(client, world, frames(seq, start, 60))  # 60 s of data
    send(client, world, frames(seq, start + timedelta(seconds=120), 60))  # after a 61 s dropout
    send(
        client,
        world,
        [
            sample(
                seq(),
                start + timedelta(seconds=180),
                value=None,
                quality="bad",
                reason="sensor_open",
            )
        ],
    )
    send(
        client,
        world,
        [
            event(
                9000,
                start + timedelta(seconds=60),
                EventType.SOURCE_UNAVAILABLE,
                {"reason": "no response"},
            )
        ],
    )
    out = series(client, start, t0)
    assert out["mode"] == "raw" and out["simulated"] is True
    assert out["sample_count"] == 121
    assert out["channel"]["unit"] == "°C" and out["channel"]["gap_threshold_s"] == 5.0
    gaps = out["gaps"]
    assert gaps[0] == {
        "start": format_utc(start + timedelta(seconds=59)),
        "end": format_utc(start + timedelta(seconds=120)),
        "duration_s": 61.0,
        "open": False,
    }
    assert gaps[-1]["open"] is True and gaps[-1]["end"] == format_utc(t0)
    bad = [p for p in out["points"] if p[2] == "bad"]
    assert len(bad) == 1 and bad[0][1] is None and bad[0][3] == "sensor_open"
    assert [e["type"] for e in out["events"]] == ["source_unavailable"]
    times = [p[0] for p in out["points"]]
    assert times == sorted(times)


def test_gap_crossing_range_start_is_clipped(
    client: TestClient, world: World, t0: datetime
) -> None:
    seq = Seq()
    send(client, world, frames(seq, t0 - timedelta(minutes=30), 1))
    send(client, world, frames(seq, t0 - timedelta(minutes=5), 10))
    out = series(client, t0 - timedelta(minutes=10), t0 - timedelta(minutes=4))
    assert out["gaps"][0]["start"] == format_utc(t0 - timedelta(minutes=10))
    assert out["gaps"][0]["end"] == format_utc(t0 - timedelta(minutes=5))


def test_bucketed_series_keeps_a_one_sample_peak(
    client: TestClient, world: World, fake_now: FakeNow, t0: datetime
) -> None:
    seq = Seq()
    start = t0 - timedelta(hours=2)
    recs = []
    for i in range(0, 7200, 1):
        pv = 1200.0 if i == 4321 else 850.0 + (i % 7) * 0.1
        recs.append(sample(seq(), start + timedelta(seconds=i), value=pv))
    for i in range(0, len(recs), 500):
        send(client, world, recs[i : i + 500])
    out = series(client, start, t0, max_points=600)
    assert out["mode"] == "bucket" and out["bucket_s"] == 15
    assert len(out["buckets"]) == 480
    assert sum(b["count"] for b in out["buckets"]) == 7200
    peak = max(out["buckets"], key=lambda b: b["max"])
    assert peak["max"] == 1200.0
    assert peak["max_t"] == format_utc(start + timedelta(seconds=4321))
    assert all(b["min"] >= 850.0 for b in out["buckets"])


def test_range_validation(client: TestClient, t0: datetime) -> None:
    bad = [
        {"start": format_utc(t0), "end": format_utc(t0 - timedelta(seconds=1))},
        {"start": "2026-11-06T08:00:00", "end": "2026-11-06T09:00:00"},
        {"start": format_utc(t0 - timedelta(days=40)), "end": format_utc(t0)},
        {"start": "yesterday", "end": "today"},
    ]
    for params in bad:
        r = client.get("/api/v1/channels/furnace-01.pv/series", params=params)
        assert r.status_code == 422, params


# --- CSV ------------------------------------------------------------------------------------------


def test_csv_export_rows_provenance_and_injection_safety(
    client: TestClient, world: World, db_url: str, t0: datetime
) -> None:
    seq = Seq()
    send(client, world, frames(seq, t0 - timedelta(seconds=10), 3))
    send(
        client,
        world,
        [
            sample(
                seq(),
                t0 - timedelta(seconds=5),
                value=None,
                quality="bad",
                reason="vendor_status_fault",
                raw="=cmd|' /C calc'!A0",
                source_status="@fault",
            )
        ],
    )
    r = client.get(
        "/api/v1/instruments/furnace-01/export.csv",
        params={"start": format_utc(t0 - timedelta(minutes=1)), "end": format_utc(t0)},
    )
    assert r.status_code == 200
    assert 'filename="SIMULATED_furnace-01_' in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 10
    pv = [x for x in rows if x["channel_key"] == "pv"][0]
    assert pv["unit"] == "°C" and pv["simulated"] == "true" and pv["quantity"] == "temperature"
    assert pv["collector_id"] == "col-1" and pv["seq"] == "1" and pv["value"] == "850.0"
    assert [x["event_time_utc"] for x in rows] == sorted(x["event_time_utc"] for x in rows)
    phase = [x for x in rows if x["channel_key"] == "phase"][0]
    assert phase["value"] == "2" and phase["value_label"] == "Soak"
    bad = rows[-1]
    assert bad["value"] == "" and bad["quality"] == "bad"
    assert bad["raw"].startswith("'=") and bad["source_status"] == "'@fault"
    with world.engine.connect() as c:
        actions = [a for (a,) in c.execute(select(AuditEvent.action))]
    assert "export_csv" in actions


# --- sessions and scope ---------------------------------------------------------------------------


def test_scope_is_enforced_on_every_id_bearing_endpoint(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime
) -> None:
    bob = client_for(world, db_url, fake_now)
    sign_in(bob, "bob")
    params = {"start": format_utc(t0 - timedelta(hours=1)), "end": format_utc(t0)}
    assert bob.get("/api/v1/instruments/furnace-01/status").status_code == 404
    assert bob.get("/api/v1/channels/furnace-01.pv/series", params=params).status_code == 404
    assert bob.get("/api/v1/instruments/furnace-01/export.csv", params=params).status_code == 404
    listing = bob.get("/api/v1/instruments").json()["items"]
    assert [i["instrument"]["id"] for i in listing] == ["furnace-b"]
    anon = client_for(world, db_url, fake_now)
    for path in ("/api/v1/instruments", "/api/v1/instruments/furnace-01/status"):
        assert anon.get(path).status_code == 401


def test_login_logout_csrf_and_throttle(world: World, db_url: str, fake_now: FakeNow) -> None:
    c = client_for(world, db_url, fake_now)
    body = {"username": "alice", "password": PASSWORD}
    assert c.post("/api/v1/session", json=body).status_code == 403  # no CSRF header
    assert (
        c.post(
            "/api/v1/session",
            json=body,
            headers={CSRF_HEADER: "1", "Origin": "http://evil.example"},
        ).status_code
        == 403
    )
    r = c.post("/api/v1/session", json=body, headers={CSRF_HEADER: "1"})
    assert r.status_code == 200 and r.json()["labs"] == {"lab-a": "scientist"}
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/v1/session").status_code == 200
    assert c.delete("/api/v1/session", headers={CSRF_HEADER: "1"}).status_code == 200
    c.cookies.clear()
    assert c.get("/api/v1/session").status_code == 401
    wrong = {"username": "alice", "password": "wrong password!!"}
    codes = [
        c.post("/api/v1/session", json=wrong, headers={CSRF_HEADER: "1"}).status_code
        for _ in range(6)
    ]
    assert codes == [401] * 5 + [429]


def test_health_endpoints(world: World, db_url: str, fake_now: FakeNow) -> None:
    c = client_for(world, db_url, fake_now)
    assert c.get("/health/live").json()["status"] == "ok"
    ready = c.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["schema"] == "0002"
    assert c.get("/api/v1/nope").status_code == 404
    assert c.get("/health/live").headers["x-content-type-options"] == "nosniff"
