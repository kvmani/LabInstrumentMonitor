"""Short real-process soak: API killed twice, outbox drains, ledger complete (simulated)."""

from __future__ import annotations

import json
from pathlib import Path

from labmon.devtools.soak import run
from tests.conftest import free_port


def test_short_soak_with_api_kills(db_url: str, tmp_path: Path) -> None:
    out = tmp_path / "soak"
    code = run(db_url, out, 45, 3, 15, free_port(), fast_backoff=True)
    summary = json.loads((out / "summary.json").read_text())
    assert code == 0, json.dumps(summary["checks"], indent=2)
    assert summary["evidence"] == "simulated"
    assert len(summary["faults"]) == 2
    assert all(f["recovered_after_s"] is not None for f in summary["faults"])
    assert sum(e["records"] for e in summary["ledger"]["epochs"]) > 0
