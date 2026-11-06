"""Minute rollups: equivalence with raw aggregation, invalidation, revision safety, gaps, peaks."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text

from labmon.contracts.envelope import format_utc
from labmon.server import rollup
from labmon.server.models import RollupDirty, SampleRollup1m
from labmon.server.series import _buckets
from tests.conftest import FakeNow
from tests.integration.helpers import (
    batch_body,
    client_for,
    post_batch,
    sample,
    seed_world,
    sign_in,
)

pytestmark = pytest.mark.integration

N = 26 * 3600  # 26 h at 1 Hz
HOLE = (30_000, 30_180)  # 3-minute dropout
SHORT_HOLE = (40_010, 40_030)  # 20 s gap inside one minute
SPIKE_AT = 50_000


def bulk_load(engine: Engine, start: datetime) -> None:
    """Fast deterministic load straight into the historian (identity ledger included)."""
    with engine.begin() as c:
        rid = c.execute(
            text(
                "INSERT INTO batch_receipt (collector_id, batch_id, payload_digest,"
                " protocol_version,"
                " outcome, record_count, accepted, duplicates, rejected, received_at) VALUES"
                " ('col-1', repeat('a', 32), 'fixture', 1, 'committed', 0, 0, 0, '[]', :t)"
                " RETURNING id"
            ),
            {"t": start},
        ).scalar_one()
        c.execute(
            text(
                """
            INSERT INTO sample (collector_id, spool_epoch, seq, channel_id, instrument_id,
              profile_revision_id, value_num, quality, source_time_meaning, capture_time,
              receive_time, event_time, event_time_source, clock_confidence, simulated, receipt_id)
            SELECT 'col-1', 7, i + 1, 'furnace-01.pv', 'furnace-01', 'furnace-01-r1',
              CASE WHEN i = :spike THEN 1250.0 ELSE 850 + (i % 7) * 0.1 END, 'good', 'absent',
              :t0 + make_interval(secs => i), :t0, :t0 + make_interval(secs => i), 'capture',
              'trusted', true, :rid
            FROM generate_series(0, :n - 1) AS i
            WHERE NOT (i >= :h0 AND i < :h1) AND NOT (i >= :s0 AND i < :s1)
            """
            ),
            {
                "t0": start,
                "rid": rid,
                "n": N,
                "spike": SPIKE_AT,
                "h0": HOLE[0],
                "h1": HOLE[1],
                "s0": SHORT_HOLE[0],
                "s1": SHORT_HOLE[1],
            },
        )
        c.execute(
            text(
                "INSERT INTO rollup_dirty SELECT channel_id,"
                " date_bin(interval '1 minute', event_time,"
                " timestamptz '2000-01-01 00:00:00+00'), 1, now() FROM sample GROUP BY 1, 2"
            )
        )


@pytest.fixture
def loaded(engine: Engine, db_url: str, fake_now: FakeNow, t0: datetime):  # type: ignore[no-untyped-def]
    world = seed_world(engine, t0 - timedelta(days=60))
    start = t0 - timedelta(seconds=N)
    bulk_load(engine, start)
    client = client_for(world, db_url, fake_now)
    sign_in(client)
    return world, client, start


def series(client: TestClient, start: datetime, end: datetime) -> dict:
    r = client.get(
        "/api/v1/channels/furnace-01.pv/series",
        params={"start": format_utc(start), "end": format_utc(end)},
    )
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def comparable(buckets: list[dict]) -> list[tuple]:
    return [
        (
            b["t"],
            b["count"],
            b["valid_count"],
            round(b["mean"], 6),
            b["min"],
            b["min_t"],
            b["max"],
            b["max_t"],
            b["first_t"],
            b["last_t"],
        )
        for b in buckets
    ]


def test_rollups_equal_raw_aggregation_before_and_after_the_worker(
    loaded,
    engine: Engine,
    t0: datetime,  # type: ignore[no-untyped-def]
) -> None:
    _, client, start = loaded
    q_start, q_end = start + timedelta(seconds=17), t0 - timedelta(seconds=5)  # partial edges
    before = series(client, q_start, q_end)
    assert before["mode"] == "bucket" and before["aggregation"] == "rollup_1m"
    assert before["bucket_s"] == 120 and before["rebuilding_minutes"] > 1500
    with engine.connect() as c:
        ch = c.execute(text("SELECT * FROM channel WHERE id = 'furnace-01.pv'")).one()
        expected = _buckets(c, ch, q_start, q_end, 120)
    assert comparable(before["buckets"]) == comparable(expected)

    cleared = rollup.process_dirty(engine, t0, limit=100_000)
    assert cleared == 26 * 60 - 3  # every minute with data; the 3 empty dropout minutes vanish
    with engine.connect() as c:
        assert c.execute(select(func.count()).select_from(RollupDirty)).scalar_one() == 0
        assert c.execute(select(func.count()).select_from(SampleRollup1m)).scalar_one() == cleared
    after = series(client, q_start, q_end)
    assert after["rebuilding_minutes"] == 0
    assert comparable(after["buckets"]) == comparable(expected)
    assert after["sample_count"] == sum(b["count"] for b in expected)


def test_peak_and_gaps_survive_aggregation(loaded, engine: Engine, t0: datetime) -> None:  # type: ignore[no-untyped-def]
    _, client, start = loaded
    rollup.process_dirty(engine, t0, limit=100_000)
    out = series(client, start, t0)
    peak = max(out["buckets"], key=lambda b: b["max"])
    assert peak["max"] == 1250.0
    assert peak["max_t"] == format_utc(start + timedelta(seconds=SPIKE_AT))
    gaps = {(g["start"], g["end"]) for g in out["gaps"]}
    hole = (
        format_utc(start + timedelta(seconds=HOLE[0] - 1)),
        format_utc(start + timedelta(seconds=HOLE[1])),
    )
    short = (
        format_utc(start + timedelta(seconds=SHORT_HOLE[0] - 1)),
        format_utc(start + timedelta(seconds=SHORT_HOLE[1])),
    )
    assert hole in gaps and short in gaps
    assert len(out["gaps"]) == 2


def test_late_sample_invalidates_its_minute(
    loaded,
    engine: Engine,
    fake_now: FakeNow,  # type: ignore[no-untyped-def]
    t0: datetime,
) -> None:
    world, client, start = loaded
    rollup.process_dirty(engine, t0, limit=100_000)
    late_at = start + timedelta(seconds=HOLE[0] + 30)  # inside the dropout, arriving late
    r = post_batch(client, world.token, batch_body([sample(1, late_at, value=999.0)])[1])
    assert r.status_code == 200, r.text
    out = series(client, start, t0)
    assert out["rebuilding_minutes"] == 1  # visible at once through the raw fallback
    assert max(b["max"] for b in out["buckets"] if b["t"] <= format_utc(late_at)) >= 999.0
    rollup.process_dirty(engine, t0)
    again = series(client, start, t0)
    assert again["rebuilding_minutes"] == 0
    assert len(again["gaps"]) == 3  # the late sample split the dropout in two


def test_concurrent_invalidation_keeps_the_minute_dirty(
    loaded,
    engine: Engine,
    t0: datetime,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    _, _, start = loaded
    target = rollup.floor_minute(start + timedelta(minutes=5))
    real = rollup.raw_minutes
    fired = {"done": False}

    def racing_raw(conn, cid, dtype, lo, hi):  # type: ignore[no-untyped-def]
        rows = real(conn, cid, dtype, lo, hi)
        if not fired["done"] and lo <= target < hi:
            fired["done"] = True
            with engine.begin() as other:  # an ingest commits while the worker computes
                rollup.mark_dirty(other, [("furnace-01.pv", target)], t0)
        return rows

    monkeypatch.setattr(rollup, "raw_minutes", racing_raw)
    rollup.process_dirty(engine, t0, limit=100_000)
    with engine.connect() as c:
        left = c.execute(select(RollupDirty.bucket, RollupDirty.revision)).all()
    assert [(b, r) for b, r in left] == [(target, 2)]
