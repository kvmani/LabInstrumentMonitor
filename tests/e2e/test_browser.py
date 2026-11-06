"""Browser happy path and failure displays (Chromium via Playwright, real processes).

Screenshots for the UI review record are written to ``test-results/screenshots`` when
``LABMON_SCREENSHOTS=1``.
"""

from __future__ import annotations

import csv
import io
import os
import time
import zipfile
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
    stack.start_worker()
    stack.start_collector()
    wait_for(lambda: sample_count(stack) >= 30, 30, "first samples")
    return stack


def login(page, base: str) -> None:  # type: ignore[no-untyped-def]
    page.goto(base + "/")
    page.get_by_label("Username").fill("scientist")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()


def test_dashboard_workspace_csv_and_failure_states(running: Stack, tmp_path: Path) -> None:
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
        login(page, stack.base)

        # dashboard: one card per default-fleet instrument, all simulated
        cards = page.get_by_test_id("fleet-cards")
        cards.wait_for()
        furnace = page.get_by_test_id("card-furnace-01")
        for iid in ("furnace-01", "utm-01", "dil-01"):
            card = page.get_by_test_id(f"card-{iid}")
            assert "SIMULATED" in card.inner_text()
            wait_for(lambda c=card: "Receiving" in c.inner_text(), 20, f"{iid} receiving")
        assert "°C" in furnace.inner_text()
        assert "Pa" in page.get_by_test_id("card-dil-01").inner_text()
        assert "kN" in page.get_by_test_id("card-utm-01").inner_text()
        spare = page.get_by_test_id("card-furnace-02")
        assert "Waiting" in spare.inner_text()
        wait_for(lambda: page.locator(".spark polyline").count() >= 3, 20, "sparklines")
        assert page.get_by_test_id("kpi-all").inner_text().startswith("4")
        shot(page, "dashboard-1366x768")

        # KPI filter and table layout
        page.get_by_test_id("kpi-receiving").click()
        assert page.get_by_test_id("card-furnace-02").count() == 0
        page.get_by_test_id("kpi-receiving").click()
        page.get_by_role("button", name="Table", exact=True).click()
        row = page.get_by_test_id("fleet-row-furnace-01")
        assert "Receiving" in row.inner_text()
        shot(page, "fleet-table-1366x768")
        page.get_by_role("button", name="Cards", exact=True).click()

        # local help is shipped with the bundle and reachable from the top bar
        page.get_by_role("link", name="Help", exact=True).click()
        assert "Stale" in page.get_by_test_id("help").inner_text()
        page.get_by_role("link", name="Fleet", exact=True).click()
        page.get_by_test_id("fleet-cards").wait_for()

        # two interactions from the fleet to the last 24 hours
        page.get_by_test_id("card-furnace-01").click()
        page.get_by_role("button", name="24 h", exact=True).click()
        page.get_by_test_id("history-chart").locator("canvas").first.wait_for()
        assert "Live" in page.get_by_test_id("live-state").inner_text()
        page.get_by_role("button", name="15 min", exact=True).click()
        summary = page.get_by_test_id("series-summary")
        summary.wait_for()
        wait_for(lambda: "no gaps" in summary.inner_text(), 10, "summary without gaps")
        assert " IST" in summary.inner_text()
        assert page.get_by_test_id("process-state").inner_text() in {
            "Idle",
            "Ramp",
            "Soak",
            "Cooldown",
        }
        shot(page, "furnace-overview-1366x768")

        # zoom pauses live follow; Resume live is offered
        page.get_by_role("button", name="Zoom in").click()
        assert "Paused" in page.get_by_test_id("live-state").inner_text()
        page.get_by_role("button", name="Resume live").click()
        assert "Live" in page.get_by_test_id("live-state").inner_text()

        with page.expect_download() as dl:
            page.get_by_test_id("csv-link").click()
        download = dl.value
        assert download.suggested_filename.startswith("SIMULATED_furnace-01_")
        rows = list(csv.DictReader(io.StringIO(Path(download.path()).read_text())))
        assert rows and {r["simulated"] for r in rows} == {"true"}
        assert {r["channel_key"] for r in rows} == {"pv", "sp", "phase"}

        page.get_by_role("button", name="UTC", exact=True).click()
        wait_for(lambda: " UTC" in summary.inner_text(), 5, "UTC labels")
        page.get_by_role("button", name="IST", exact=True).click()

        # diagnostics and runs tabs
        page.get_by_test_id("tab-diagnostics").click()
        page.get_by_test_id("diagnostics").wait_for()
        assert "Collector online" in page.get_by_test_id("diagnostics").inner_text()
        wait_for(
            lambda: "acquisition started" in page.get_by_test_id("events-table").inner_text(),
            10,
            "events listed",
        )
        # RUN-01 through the UI: a user run record and an annotation
        page.get_by_test_id("tab-runs").click()
        page.get_by_test_id("new-run").click()
        form = page.get_by_test_id("new-run-form")
        form.get_by_label("Name").fill("Rehearsal anneal")
        form.get_by_label("Tags (comma separated)").fill("rehearsal")
        form.get_by_role("button", name="Create run").click()
        detail = page.get_by_test_id("run-detail")
        wait_for(lambda: "Rehearsal anneal" in detail.inner_text(), 10, "new run selected")
        assert (
            "Recorded by scientist" in detail.inner_text() and "#rehearsal" in detail.inner_text()
        )
        page.get_by_role("button", name="■ End run now").click()
        wait_for(lambda: "Complete" in detail.inner_text(), 10, "run ended")
        shot(page, "furnace-user-run-1366x768")

        page.get_by_test_id("tab-overview").click()
        notes = page.get_by_test_id("annotations")
        notes.get_by_role("button", name="✎ Add annotation").click()
        notes.get_by_label("Annotation text").fill("door opened for inspection")
        notes.get_by_role("button", name="Save").click()
        wait_for(
            lambda: "door opened for inspection" in notes.inner_text(), 10, "annotation listed"
        )
        notes.get_by_role("button", name="Correct").click()
        notes.get_by_label("Corrected text").fill("door opened for TC inspection")
        notes.get_by_label("Reason (recorded in the audit log)").fill("clarify the probe")
        notes.get_by_role("button", name="Save correction").click()
        wait_for(lambda: "corrected (rev 2)" in notes.inner_text(), 10, "annotation corrected")
        shot(page, "furnace-annotations-1366x768")

        # saved personal view: store the current range and axes, then re-apply it
        views = page.get_by_test_id("views")
        views.get_by_role("button", name="Save view…").click()
        views.get_by_label("View name").fill("rehearsal 1 h")
        views.get_by_role("button", name="Save", exact=True).click()
        wait_for(
            lambda: "rehearsal 1 h" in views.get_by_label("Saved views").inner_text(),
            10,
            "saved view listed",
        )
        page.get_by_role("button", name="24 h", exact=True).click()
        views.get_by_label("Saved views").select_option(label="rehearsal 1 h")
        wait_for(lambda: "Live" in page.get_by_test_id("live-state").inner_text(), 5, "applied")

        # durable export: queued, built by the worker process, downloaded with a manifest
        page.get_by_test_id("request-export").click()
        link = page.get_by_test_id("export-download").first
        link.wait_for(timeout=30000)
        with page.expect_download() as dl:
            link.click()
        with zipfile.ZipFile(dl.value.path()) as zf:
            assert sorted(zf.namelist()) == ["data.csv", "manifest.json"]
            assert '"simulated": true' in zf.read("manifest.json").decode()

        # dilatometer: log-scale pressure panel and signed length change tile
        page.goto(stack.base + "/#/instrument/dil-01")
        page.get_by_test_id("history-chart").locator("canvas").first.wait_for()
        assert page.get_by_label("Chamber pressure axis").input_value() == "log"
        shot(page, "dilatometer-overview-1366x768")

        for w, h, name in ((1920, 1080, "wide"), (1024, 768, "tablet"), (390, 844, "phone")):
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(400)
            shot(page, f"dilatometer-{name}-{w}x{h}")
        page.set_viewport_size({"width": 1366, "height": 768})

        # closing the browser does not affect collection
        context.close()
        n = sample_count(stack)
        time.sleep(4)
        assert sample_count(stack) > n

        # stale display after the collector stops, then the API going away
        context = browser.new_context(viewport={"width": 1366, "height": 768})
        page = context.new_page()
        login(page, stack.base)
        page.get_by_test_id("card-furnace-01").click()
        header = page.get_by_test_id("instrument-header")
        header.wait_for()
        stack.stop_collector()
        wait_for(
            lambda: "Stale" in header.get_by_test_id("acq-state").inner_text(),
            20,
            "Stale after the collector stopped",
        )
        assert "no new readings since" in page.get_by_test_id("acq-reason").inner_text()
        shot(page, "furnace-stale-1366x768")
        stack.kill_server()
        page.get_by_test_id("connection-banner").wait_for(timeout=20000)
        assert (
            "Cannot reach the monitor service"
            in page.get_by_test_id("connection-banner").inner_text()
        )
        shot(page, "furnace-api-down-1366x768")
        browser.close()

    assert foreign == [], f"unexpected external requests: {foreign}"
