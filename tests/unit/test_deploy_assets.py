"""Static checks of the deployment assets: offline-only installers, consistent service files."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


def test_installers_never_download() -> None:
    for path in (DEPLOY / "install.sh", DEPLOY / "windows" / "install-collector.ps1"):
        body = path.read_text()
        assert "--no-index" in body and "--require-hashes" in body, path
        assert not re.search(
            r"Invoke-WebRequest|Start-BitsTransfer|wget |curl -o|https?://(?!127\.0\.0\.1)", body
        ), path


def test_windows_service_definition() -> None:
    svc = ET.parse(DEPLOY / "windows" / "labmon-collector.xml").getroot()  # noqa: S314 - repo file
    assert svc.findtext("id") == "labmon-collector"
    exe = svc.findtext("executable") or ""
    assert exe.endswith(r"\labmon\current\venv\Scripts\labmon-collector.exe")
    args = svc.findtext("arguments") or ""
    assert args.startswith("run --config") and r"%ProgramData%\labmon\collector" in args
    assert svc.findtext("startmode") == "Automatic"
    assert [f.get("action") for f in svc.findall("onfailure")] == ["restart", "restart"]
    # state (logs, spool, config) lives under ProgramData, never inside the release
    assert r"%ProgramData%" in (svc.findtext("logpath") or "")


def test_linux_units_run_the_current_release() -> None:
    for unit in (DEPLOY / "systemd").glob("labmon-*.service"):
        body = unit.read_text()
        exec_start = next(line for line in body.splitlines() if line.startswith("ExecStart="))
        assert "/opt/labmon/current/venv/bin/" in exec_start, unit
        assert "User=root" not in body, unit


def test_bundle_carries_every_deploy_asset() -> None:
    build = (ROOT / "scripts" / "build-release.sh").read_text()
    for asset in ("deploy/systemd", "deploy/nginx", "deploy/windows", "deploy/install.sh"):
        assert asset in build, asset
    assert "wheelhouse-win" in build
    # releases keep their installer so `--activate` rollbacks work from the active release
    assert 'cp -r "$BUNDLE/deploy" "$DEST/deploy"' in (DEPLOY / "install.sh").read_text()
