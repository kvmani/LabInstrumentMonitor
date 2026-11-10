"""A custodian commissions a simulated instrument in the browser without developer help (ONB-01).

Real API, worker and collector processes. The administrator downloads the collector package from
the Collectors page, the collector is restarted with it, and the wizard shows the instrument as
active only after the collector reports the revision and committed samples arrive.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from labmon.server.bootstrap import ensure_user
from tests.e2e.conftest import PASSWORD, ROOT, Stack

pytestmark = pytest.mark.e2e
playwright = pytest.importorskip("playwright.sync_api")
SHOTS = ROOT / "test-results" / "screenshots"


def shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if os.environ.get("LABMON_SCREENSHOTS") == "1":
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def sign_in(page, base: str, user: str) -> None:  # type: ignore[no-untyped-def]
    page.goto(base + "/")
    page.get_by_label("Username").fill(user)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.get_by_role("button", name="Sign out").wait_for()


def test_custodian_commissions_and_collector_activates(stack: Stack, tmp_path: Path) -> None:
    if not (ROOT / "web" / "dist" / "index.html").exists():
        pytest.skip("UI not built (cd web && npm ci && npm run build)")
    eng = create_engine(stack.db_url)
    with eng.begin() as c:
        ensure_user(c, "tech", "Tess Technician", PASSWORD, {"lab-demo": "custodian"})
        ensure_user(c, "admin", "Ada Admin", PASSWORD, {"lab-demo": "administrator"})
    eng.dispose()
    stack.start_server()
    stack.start_worker()
    stack.start_collector()

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1366, "height": 768}, accept_downloads=True)
        page = ctx.new_page()
        page.set_default_timeout(20000)
        sign_in(page, stack.base, "tech")

        page.get_by_role("link", name="Commission", exact=True).click()
        page.get_by_test_id("commission-home").wait_for()
        page.get_by_label("Instrument id").fill("dil-02")
        page.get_by_role("button", name="Start draft").click()

        # 1 Identify (the collector check shows the running collector online)
        page.get_by_test_id("step-1").wait_for()
        page.get_by_label("Name", exact=True).fill("Vacuum dilatometer D-02")
        page.get_by_label("Location").fill("Bay 2")
        page.get_by_label("Custodian").select_option("tech")
        page.get_by_label("Collector").select_option("col-bay1-01")
        page.get_by_test_id("collector-check").get_by_text("online").wait_for()
        shot(page, "commission-1-identify")
        page.get_by_role("button", name="Next: Connect").click()

        # 2 Connect
        page.get_by_test_id("step-2").wait_for()
        page.get_by_label("Simulator profile").select_option("sim-dilatometer")
        page.get_by_label("Seed").fill("12")
        page.get_by_role("button", name="Next: Map").click()

        # an interrupted wizard resumes its draft: reload and continue from the home page
        page.get_by_test_id("step-3").wait_for()
        page.reload()
        page.get_by_role("link", name="Commission", exact=True).click()
        page.get_by_test_id("open-drafts").get_by_role("link").first.click()
        page.get_by_test_id("step-3").wait_for()
        assert "pressure" in page.get_by_test_id("channel-map").inner_text()
        shot(page, "commission-3-map")
        page.get_by_role("button", name="Next: Preview").click()

        # 4 Preview
        page.get_by_test_id("run-preview").click()
        assert page.get_by_test_id("preview-result").inner_text() == "Preview passed"
        assert "dropout drill" in page.get_by_test_id("checks").inner_text()
        shot(page, "commission-4-preview")
        page.get_by_role("button", name="Next: Approve").click()

        # 5 Approve and publish
        page.get_by_test_id("changes").wait_for()
        page.get_by_test_id("publish").click()
        state = page.get_by_test_id("verify-state")
        state.wait_for()
        assert state.inner_text() == "Waiting for the collector"
        shot(page, "commission-6-waiting")

        # administrator: configuration package for the collector (credential unchanged)
        admin = ctx.browser.new_context(accept_downloads=True).new_page()  # type: ignore[union-attr]
        admin.set_default_timeout(20000)
        sign_in(admin, stack.base, "admin")
        admin.goto(stack.base + "/#/collectors")
        row = admin.get_by_test_id("collector-col-bay1-01")
        row.wait_for()
        assert "dil-02" in row.inner_text()
        with admin.expect_download() as dl:
            row.get_by_role("button", name="Configuration").click()
        archive = tmp_path / "pkg.zip"
        dl.value.save_as(archive)
        shot(admin, "collectors")
        with zipfile.ZipFile(io.BytesIO(archive.read_bytes())) as z:
            assert "collector.token" not in z.namelist()
            toml = z.read("collector.toml").decode()
        assert 'instrument_id = "dil-02"' in toml and "dil-02-r1" in toml

        # install it on the "instrument PC" (keep the test's fast-recovery settings) and restart
        old = stack.config.read_text()
        extra = "".join(
            line + "\n"
            for line in old.splitlines()
            if line.startswith(("max_backoff_s", "flush_s"))
        )
        stack.stop_collector()
        stack.config.write_text(toml.replace("[[instruments]]", extra + "\n[[instruments]]", 1))
        stack.start_collector()

        # the wizard turns active only once the revision is applied and samples are committed
        page.wait_for_function(
            "() => document.querySelector('[data-testid=verify-state]')?.textContent"
            " === 'Active: receiving samples'",
            timeout=40000,
        )
        shot(page, "commission-6-active")
        if os.environ.get("LABMON_SCREENSHOTS") == "1":  # phone width, dark theme review
            page.set_viewport_size({"width": 390, "height": 844})
            page.emulate_media(color_scheme="dark")
            shot(page, "commission-6-active-phone-dark")
            admin.set_viewport_size({"width": 390, "height": 844})
            admin.emulate_media(color_scheme="dark")
            shot(admin, "collectors-phone-dark")
            page.set_viewport_size({"width": 1366, "height": 768})
        with page.expect_download() as rec:
            page.get_by_test_id("record").click()
        assert rec.value.suggested_filename.startswith("commissioning-dil-02-dil-02-r1")

        # the new instrument is in the fleet
        page.get_by_role("link", name="Fleet").click()
        page.get_by_test_id("card-dil-02").wait_for()
        browser.close()
