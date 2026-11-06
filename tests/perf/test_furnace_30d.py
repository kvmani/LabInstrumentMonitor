"""M2 gate evidence: the 30-day furnace view keeps the one-sample peak and the dropout, with
recorded query latency. Simulated evidence; writes test-results/perf-furnace-30d.json."""

from __future__ import annotations

import json
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine

from labmon.contracts.envelope import format_utc
from labmon.server.fixture_loader import load_fixture
from labmon.server.worker import drain
from labmon.sim.fixtures import FIXTURES
from tests.integration.helpers import client_for, seed_world, sign_in

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.slow
def test_30_day_view_keeps_peak_and_dropout(engine: Engine, db_url: str) -> None:
    fx = FIXTURES["furnace-30d"]
    exp = fx.expected()
    world = seed_world(engine, fx.start - timedelta(days=1))
    t = time.perf_counter()
    loaded = load_fixture(
        engine, "furnace-30d", channels=("pv", "sp", "phase"), lab=("lab-a", "Lab A")
    )
    load_s = time.perf_counter() - t
    assert loaded["samples"] == 3 * exp["samples_per_channel"]
    t = time.perf_counter()
    minutes = drain(engine)
    rollup_s = time.perf_counter() - t
    assert minutes == 3 * (fx.days * 1440 - 150)  # 150 empty dropout minutes per channel

    now = datetime(2026, 11, 6, tzinfo=UTC)
    client = client_for(world, db_url, lambda: now)
    sign_in(client)
    params = {"start": format_utc(fx.start), "end": format_utc(fx.end), "max_points": 1200}

    def get(ch: str) -> dict:
        r = client.get(f"/api/v1/channels/{fx.instrument_id}.{ch}/series", params=params)
        assert r.status_code == 200, r.text
        return r.json()  # type: ignore[no-any-return]

    t = time.perf_counter()
    pv = get("pv")
    cold_s = time.perf_counter() - t
    assert pv["mode"] == "bucket" and pv["aggregation"] == "rollup_1m"
    assert pv["rebuilding_minutes"] == 0
    assert pv["sample_count"] == exp["samples_per_channel"]
    peak = max(pv["buckets"], key=lambda b: b["max"] or -1e9)
    assert peak["max"] == exp["pv_max"]["value"]
    assert peak["max_t"] == format_utc(exp["pv_max"]["time"])
    assert pv["gaps"] == [
        {
            "start": format_utc(exp["dropout"]["last_before"]),
            "end": format_utc(exp["dropout"]["first_after"]),
            "duration_s": exp["dropout"]["missing_seconds"] + 1.0,
            "open": False,
        }
    ]
    assert [e["type"] for e in pv["events"]] == [
        "acquisition_started",
        "source_unavailable",
        "source_recovered",
    ]

    # latency: the full instrument view (3 series) many times, warm
    timings = []
    for _ in range(15):
        t = time.perf_counter()
        for ch in ("pv", "sp", "phase"):
            get(ch)
        timings.append(time.perf_counter() - t)
    timings.sort()
    p50 = statistics.median(timings)
    p95 = timings[int(0.95 * (len(timings) - 1))]
    record = {
        "evidence": "simulated",
        "fixture": "furnace-30d",
        "samples": loaded["samples"],
        "load_s": round(load_s, 1),
        "rollup_worker_s": round(rollup_s, 1),
        "rollup_minutes": minutes,
        "cold_single_series_s": round(cold_s, 3),
        "warm_3_series_view_p50_s": round(p50, 3),
        "warm_3_series_view_p95_s": round(p95, 3),
        "buckets": len(pv["buckets"]),
        "bucket_s": pv["bucket_s"],
    }
    out = ROOT / "test-results"
    out.mkdir(exist_ok=True)
    (out / "perf-furnace-30d.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record, indent=2))
    assert p95 < 3.0  # SPECIFICATIONS §10 target for a warmed 30-day view
