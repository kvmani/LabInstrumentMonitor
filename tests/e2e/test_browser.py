"""Browser happy path and failure displays (Chromium via Playwright, real processes).

Screenshots for the UI review record are written to ``test-results/screenshots`` when
``LABMON_SCREENSHOTS=1``.
"""

from __future__ import annotations

import csv
import io
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from labmon.server.bootstrap import InstrumentSeed, ensure_instrument
from tests.e2e.conftest import PASSWORD, ROOT, Stack, wait_for

pytestmark = pytest.mark.e2e

playwright = pytest.importorskip("playwright.sync_api")
SHOTS = ROOT / "test-results" / "screenshots"


def sample_count(stack: Stack) -> int:
    eng = create_engine(stack.db_url)
    with eng.connect() as c:
        n = int(c.execute(text("SELECT count(*) FROM sample")).scalar_one())
    eng.dispose()
    return n


def shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if os.environ.get("LABMON_SCREENSHOTS") == "1":
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


@pytest.fixture
def running(stack: Stack) -> Stack:
    if not (ROOT / "web" / "dist" / "index.html").exists():
        pytest.skip("UI not built (cd web && npm ci && npm run build)")
    eng = create_engine(stack.db_url)
    with eng.begin() as c:  # an enrolled instrument with no collector: the empty state
        ensure_instrument(
            c,
            "lab-demo",
            InstrumentSeed("furnace-02", "Furnace F-02 (spare)", "sim-furnace", 5),
            "e2e",
        )
    eng.dispose()
    stack.start_server()
    stack.start_collector()
    wait_for(lambda: sample_count(stack) >= 30, 30, "first samples")
    return stack


def test_fleet_to_history_csv_and_failure_states(running: Stack, tmp_path: Path) -> None:
    stack = running
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            accept_downloads=True,
            timezone_id="Asia/Kolkata",
        )
        page = context.new_page()
        foreign: list[str] = []
        page.on(
            "request",
            lambda r: (
                foreign.append(r.url)
                if not r.url.startswith(stack.base) and not r.url.startswith("data:")
                else None
            ),
        )

        page.goto(stack.base + "/")
        page.get_by_label("Username").fill("scientist")
        page.get_by_label("Password").fill(PASSWORD)
        page.get_by_role("button", name="Sign in").click()

        row = page.get_by_test_id("fleet-row-furnace-01")
        row.wait_for()
        assert "SIMULATED" in row.inner_text()
        wait_for(lambda: "Receiving" in row.inner_text(), 15, "Receiving badge in fleet")
        assert "°C" in row.inner_text()
        spare = page.get_by_test_id("fleet-row-furnace-02")
        assert "Waiting" in spare.inner_text() and "no collector assigned" in spare.inner_text()
        shot(page, "fleet-1366x768")

        # two interactions from the fleet to the last 24 hours
        row.get_by_role("link", name="Heat-treatment furnace F-01", exact=True).click()
        page.get_by_role("button", name="24 h", exact=True).click()
        page.get_by_test_id("history-chart").locator("canvas").first.wait_for()
        assert "Live" in page.get_by_test_id("live-state").inner_text()
        page.get_by_role("button", name="15 min", exact=True).click()
        page.get_by_test_id("series-summary").wait_for()
        wait_for(
            lambda: "no gaps" in page.get_by_test_id("series-summary").inner_text(),
            10,
            "summary without gaps",
        )
        assert page.get_by_test_id("process-state").inner_text() in {
            "Idle",
            "Ramp",
            "Soak",
            "Cooldown",
        }
        assert " IST" in page.get_by_test_id("series-summary").inner_text()
        shot(page, "instrument-live-1366x768")

        # zooming pauses live follow explicitly; Resume live is always offered
        page.get_by_role("button", name="Zoom in").click()
        assert "Paused" in page.get_by_test_id("live-state").inner_text()
        page.get_by_role("button", name="Resume live").click()
        assert "Live" in page.get_by_test_id("live-state").inner_text()

        # CSV of the displayed range
        with page.expect_download() as dl:
            page.get_by_test_id("csv-link").click()
        download = dl.value
        assert download.suggested_filename.startswith("SIMULATED_furnace-01_")
        rows = list(csv.DictReader(io.StringIO(Path(download.path()).read_text())))
        assert rows and {r["simulated"] for r in rows} == {"true"}
        assert {r["channel_key"] for r in rows} == {"pv", "sp", "phase"}

        # UTC toggle changes the axis labelling without refetching a different window
        page.get_by_role("button", name="UTC", exact=True).click()
        wait_for(
            lambda: " UTC" in page.get_by_test_id("series-summary").inner_text(), 5, "UTC labels"
        )
        page.get_by_role("button", name="IST", exact=True).click()

        for w, h, name in ((1920, 1080, "wide"), (1024, 768, "tablet")):
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(500)
            shot(page, f"instrument-{name}-{w}x{h}")
        page.set_viewport_size({"width": 1366, "height": 768})

        # closing the browser does not affect collection
        context.close()
        n = sample_count(stack)
        time.sleep(4)
        assert sample_count(stack) > n

        # stale display after the collector stops
        context = browser.new_context(viewport={"width": 1366, "height": 768})
        page = context.new_page()
        page.goto(stack.base + "/")
        page.get_by_label("Username").fill("scientist")
        page.get_by_label("Password").fill(PASSWORD)
        page.get_by_role("button", name="Sign in").click()
        page.get_by_test_id("fleet-row-furnace-01").get_by_role("link").first.click()
        header = page.get_by_test_id("instrument-header")
        header.wait_for()
        stack.stop_collector()
        wait_for(
            lambda: "Stale" in header.get_by_test_id("acq-state").inner_text(),
            20,
            "Stale after the collector stopped",
        )
        assert "no new readings since" in page.get_by_test_id("acq-reason").inner_text()
        shot(page, "instrument-stale-1366x768")

        # API down: the page says so and keeps showing the last data with its age
        stack.kill_server()
        page.get_by_test_id("connection-banner").wait_for(timeout=20000)
        assert (
            "Cannot reach the monitor service"
            in page.get_by_test_id("connection-banner").inner_text()
        )
        shot(page, "instrument-api-down-1366x768")
        browser.close()

    assert foreign == [], f"unexpected external requests: {foreign}"
