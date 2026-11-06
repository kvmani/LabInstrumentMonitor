"""Runs and XY analyses through the real collector code path on a virtual clock.

Acquisition → SQLite outbox → uploader → FastAPI → PostgreSQL, with known answers checked at the
API: tensile UTS and fracture order, DSC peak sign/temperature/area, run idempotency and scope.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from labmon.collector.acquisition import Acquisition
from labmon.collector.config import InstrumentBinding
from labmon.collector.outbox import Outbox
from labmon.collector.uploader import StepResult, Uploader
from labmon.profiles import PROFILES
from labmon.server.app import create_app
from labmon.server.bootstrap import (
    DILATOMETER_SEED,
    DSC_SEED,
    UTM_SEED,
    InstrumentSeed,
    assign,
    ensure_collector,
    ensure_instrument,
    ensure_lab,
    ensure_user,
)
from labmon.server.models import Run
from labmon.server.security import issue_collector_credential
from labmon.sim.base import ANCHOR_MS
from labmon.sim.clock import VirtualClock
from tests.integration.helpers import PASSWORD, settings_for, sign_in

pytestmark = pytest.mark.integration


def binding(seed: InstrumentSeed) -> InstrumentBinding:
    keys = [c.key for c in PROFILES[seed.profile_key].channels]
    return InstrumentBinding(
        instrument_id=seed.instrument_id,
        profile=seed.profile_key,
        profile_revision_id=f"{seed.instrument_id}-r1",
        seed=seed.seed,
        channels={k: f"{seed.instrument_id}.{k}" for k in keys},
    )


class Rig:
    """One collector process' worth of objects, all on a shared virtual clock."""

    def __init__(
        self,
        engine: Engine,
        db_url: str,
        tmp: Path,
        start: datetime,
        seeds: tuple[InstrumentSeed, ...],
    ) -> None:
        with engine.begin() as c:
            ensure_lab(c, "lab-demo", "Demo")
            ensure_lab(c, "lab-other", "Other")
            ensure_collector(c, "col-1", "lab-demo", "collector")
            for s in seeds:
                ensure_instrument(c, "lab-demo", s, "test")
                assign(c, "col-1", s.instrument_id, start - timedelta(days=1), "test")
            self.token = issue_collector_credential(c, "col-1")
            ensure_user(c, "alice", "Alice", PASSWORD, {"lab-demo": "scientist"})
            ensure_user(c, "bob", "Bob", PASSWORD, {"lab-other": "viewer"})
        self.clock = VirtualClock(start)
        app = create_app(settings_for(db_url), engine=engine, now=self.clock.now)
        self.client = TestClient(app)
        self.tmp = tmp
        self.seeds = seeds
        self.outbox = Outbox(tmp / "spool.sqlite3", "col-1", now=self.clock.now)
        self.uploader = Uploader(self.outbox, self.client, self.token, self.clock, compress=True)
        self.acq = Acquisition(self.outbox, self.clock, tuple(binding(s) for s in seeds))
        self.acq.start()

    def run_for(self, seconds: float, upload_every_s: float = 30.0) -> None:
        end = self.clock.now() + timedelta(seconds=seconds)
        next_upload = self.clock.now() + timedelta(seconds=upload_every_s)
        while self.clock.now() < end:
            self.clock.advance(max(self.acq.seconds_until_due(), 0))
            self.acq.run_due()
            if self.clock.now() >= next_upload:
                self.drain()
                next_upload = self.clock.now() + timedelta(seconds=upload_every_s)
        self.drain()

    def drain(self) -> None:
        results = self.uploader.drain()
        assert StepResult.FAULT not in results and StepResult.REJECTED not in results, results

    def restart_collector(self) -> None:
        self.acq = Acquisition(self.outbox, self.clock, tuple(binding(s) for s in self.seeds))
        self.acq.start()


def at_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def runs(client: TestClient, iid: str) -> list[dict]:
    r = client.get(f"/api/v1/instruments/{iid}/runs")
    assert r.status_code == 200, r.text
    return r.json()["items"]  # type: ignore[no-any-return]


def test_tensile_run_known_answers(engine: Engine, db_url: str, tmp_path: Path) -> None:
    rig = Rig(engine, db_url, tmp_path, at_ms(ANCHOR_MS + 200_000), (UTM_SEED,))
    rig.run_for(430)  # past the end of cycle 0 (405 s) into the next idle period
    sign_in(rig.client)
    items = runs(rig.client, "utm-01")
    assert [r["run_key"] for r in items] == ["c000000"]
    run = items[0]
    assert run["ended_at"] is not None and run["kind"] == "tensile_test" and run["simulated"]
    assert run["started_at"] == "2026-01-01T00:04:00.000000Z"  # program start, not first-seen
    body = rig.client.get(f"/api/v1/runs/{run['id']}").json()
    a = body["analysis"]
    assert a["kind"] == "tensile" and a["y"]["unit"] == "MPa"
    s = a["summary"]
    assert s["complete"] and s["reasons"] == [] and s["invalid_pairs"] == 0
    assert s["uts_mpa"] == pytest.approx(480.0, abs=0.6)  # noise on force ≈ 0.14 MPa (1σ)
    assert s["uts_strain"] == pytest.approx(0.18, abs=0.01)
    assert s["fracture_after_uts"] is True
    assert s["fracture_strain"] == pytest.approx(0.28, abs=0.002)
    assert s["max_force_kn"] == pytest.approx(13.572, abs=0.02)
    pts = [p for p in a["points"] if p is not None]
    assert len(pts) > 700  # 5 Hz over ~145 s of loading
    times = [p[2] for p in pts]
    assert times == sorted(times)  # acquisition order preserved
    assert pts[-1][3] == 3 and pts[-1][1] < 5.0  # the fracture load drop is the last point
    # 1 Hz while idle, 5 Hz while testing, measured from the stored samples
    status = rig.client.get("/api/v1/instruments/utm-01/status").json()
    assert status["display"]["analysis"] == "tensile"
    assert [p["key"] for p in status["display"]["panels"]] == ["force", "extension", "state"]


def test_incomplete_tensile_run_reports_no_uts(engine: Engine, db_url: str, tmp_path: Path) -> None:
    rig = Rig(engine, db_url, tmp_path, at_ms(ANCHOR_MS + 230_000), (UTM_SEED,))
    rig.run_for(80)  # stops mid-test
    sign_in(rig.client)
    run = runs(rig.client, "utm-01")[0]
    assert run["active"] and run["ended_at"] is None
    s = rig.client.get(f"/api/v1/runs/{run['id']}").json()["analysis"]["summary"]
    assert s["complete"] is False and s["uts_mpa"] is None
    assert "run not complete" in s["reasons"]
    status = rig.client.get("/api/v1/instruments/utm-01/status").json()
    assert status["active_run"]["id"] == run["id"]
    assert status["process_state"]["state"] == "Testing"


def test_dsc_run_known_answers_and_sign(engine: Engine, db_url: str, tmp_path: Path) -> None:
    rig = Rig(engine, db_url, tmp_path, at_ms(ANCHOR_MS - 5_000), (DSC_SEED,))
    rig.run_for(2530, upload_every_s=120)
    sign_in(rig.client)
    items = runs(rig.client, "dsc-01")
    run = next(r for r in items if r["run_key"] == "c000000")
    assert run["ended_at"] is not None
    a = rig.client.get(f"/api/v1/runs/{run['id']}").json()["analysis"]
    assert a["kind"] == "dsc" and a["sign_convention"].startswith("exo up")
    endo, exo = a["summary"]["endotherm"], a["summary"]["exotherm"]
    assert endo["peak_mw"] < -25.0 and 157.5 < endo["sample_t_c"] < 159.5
    assert endo["area_mj"] == pytest.approx(-286.0, rel=0.03)
    assert endo["specific_j_per_g"] == pytest.approx(-28.6, rel=0.03)
    assert exo["peak_mw"] > 29.0 and 152.0 < exo["sample_t_c"] < 154.5
    assert exo["area_mj"] == pytest.approx(286.0, rel=0.05)
    segs = a["summary"]["segments"]
    assert segs["heating"] > 900 and segs["cooling"] > 900
    first_heating = next(p for p in a["points"] if p and p[3] == "heating")
    assert first_heating[0] < 40.0


def test_collector_restart_mid_run_is_idempotent(
    engine: Engine, db_url: str, tmp_path: Path
) -> None:
    rig = Rig(engine, db_url, tmp_path, at_ms(ANCHOR_MS + 130_000), (DILATOMETER_SEED,))
    rig.run_for(60)
    rig.restart_collector()  # re-announces the current run
    rig.run_for(60)
    with engine.connect() as c:
        n = c.execute(select(func.count()).select_from(Run)).scalar_one()
        started = c.execute(select(Run.started_at)).scalar_one()
    assert n == 1
    assert started == at_ms(ANCHOR_MS + 120_000)


def test_runs_are_scoped(engine: Engine, db_url: str, tmp_path: Path) -> None:
    rig = Rig(engine, db_url, tmp_path, at_ms(ANCHOR_MS + 238_000), (UTM_SEED,))
    rig.run_for(10)
    sign_in(rig.client, "bob")
    assert rig.client.get("/api/v1/instruments/utm-01/runs").status_code == 404
    assert rig.client.get("/api/v1/runs/utm-01.c000000").status_code == 404
    assert rig.client.get("/api/v1/instruments/utm-01/events").status_code == 404


def test_missed_run_end_is_closed_by_the_next_run(
    engine: Engine, db_url: str, tmp_path: Path
) -> None:
    """Collector stops mid-test and restarts after the test ended: the open run must not stay
    'running'; it is closed as end_not_reported once the next run starts."""
    rig = Rig(engine, db_url, tmp_path, at_ms(ANCHOR_MS + 250_000), (UTM_SEED,))
    rig.run_for(30)  # inside test c000000
    rig.clock.advance(300)  # collector down until the next cycle's idle period (t = 580 s)
    rig.restart_collector()
    sign_in(rig.client)
    status = rig.client.get("/api/v1/instruments/utm-01/status").json()
    assert status["active_run"] is None  # past its planned end: not shown as running
    rig.run_for(80)  # into the next test (preload at 645 s)
    items = {r["run_key"]: r for r in runs(rig.client, "utm-01")}
    old, new = items["c000000"], items["c000001"]
    assert old["ended_at"] == "2026-01-01T00:06:45.000000Z" and old["end_reason"] == (
        "end_not_reported"
    )
    assert not old["active"] and new["active"]
    s = rig.client.get(f"/api/v1/runs/{old['id']}").json()["analysis"]["summary"]
    assert s["uts_mpa"] is None and "fracture not observed" in s["reasons"]
