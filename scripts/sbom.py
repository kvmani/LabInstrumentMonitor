"""Minimal CycloneDX 1.5 SBOM (JSON) for a release: runtime Python packages from the frozen
lock export and the bundled web dependencies from web/package-lock.json, with licenses."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def python_components() -> list[dict]:
    req = subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--format",
            "requirements-txt",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    out = []
    for line in req.splitlines():
        line = line.split(";")[0].strip()
        if not line or line.startswith(("#", "-")) or "==" not in line:
            continue
        name, version = line.split("==", 1)
        name = name.split("[")[0]
        try:
            meta = metadata.metadata(name)
            lic = meta.get("License-Expression") or meta.get("License") or ""
            if not lic or len(lic) > 60:
                lic = next(
                    (
                        c.split("::")[-1].strip()
                        for c in meta.get_all("Classifier") or []
                        if c.startswith("License ::")
                    ),
                    lic[:60],
                )
        except metadata.PackageNotFoundError:
            lic = ""
        out.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{name.lower()}@{version}",
                "licenses": [{"license": {"name": lic or "UNKNOWN"}}],
            }
        )
    return out


def web_components() -> list[dict]:
    lock = json.loads((ROOT / "web" / "package-lock.json").read_text())
    out = []
    for path, info in lock.get("packages", {}).items():
        if not path or info.get("dev"):
            continue
        name = path.split("node_modules/")[-1]
        out.append(
            {
                "type": "library",
                "name": name,
                "version": info.get("version", ""),
                "purl": f"pkg:npm/{name}@{info.get('version', '')}",
                "licenses": [{"license": {"name": info.get("license", "UNKNOWN")}}],
            }
        )
    return out


def main() -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import labmon

    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "labmon",
                "version": labmon.__version__,
                "licenses": [{"license": {"id": "MIT"}}],
            }
        },
        "components": python_components() + web_components(),
    }
    print(json.dumps(bom, indent=2))


if __name__ == "__main__":
    main()
