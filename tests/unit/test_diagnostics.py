"""Fault classification: each rule yields one fault with a concrete next step."""

from __future__ import annotations

from typing import Any

import pytest

from labmon.server.diagnostics import instrument_faults


def status(
    collector: dict[str, Any] | None = None, acquisition: dict[str, Any] | None = None
) -> dict[str, Any]:
    c = {"connectivity": "online", "sources": [], "quarantined": 0, **(collector or {})}
    a = {"state": "receiving", "reason_code": "fresh", **(acquisition or {})}
    return {"collector": c, "acquisition": a}


def codes(s: dict[str, Any]) -> list[str]:
    return [f["code"] for f in instrument_faults(s)]


def test_healthy_instrument_has_no_faults() -> None:
    assert instrument_faults(status()) == []


@pytest.mark.parametrize(
    ("collector", "acquisition", "code", "severity"),
    [
        ({"connectivity": "offline"}, {"state": "stale"}, "collector_offline", "warning"),
        ({"connectivity": "never_seen"}, {"state": "waiting"}, "collector_never_seen", "warning"),
        ({"connectivity": "revoked"}, {}, "collector_revoked", "critical"),
        ({}, {"reason_code": "no_assignment", "state": "waiting"}, "no_collector", "info"),
        ({"upload_fault": "authentication refused (401)"}, {}, "upload_auth", "critical"),
        ({"upload_fault": "batch_conflict"}, {}, "upload_fault", "critical"),
        ({"clock_offset_s": -42.0}, {}, "clock_offset", "warning"),
        ({"backlog_age_s": 600, "unacknowledged": 9000}, {}, "delivery_delayed", "warning"),
        (
            {"spool_disk_free_bytes": 100 * 2**20, "spool_disk_total_bytes": 100 * 2**30},
            {},
            "spool_disk_low",
            "critical",
        ),
        (
            {"spool_disk_free_bytes": 5 * 2**30, "spool_disk_total_bytes": 100 * 2**30},
            {},
            "spool_disk_low",
            "warning",
        ),
        (
            {"sources": [{"instrument_id": "x", "state": "spool_full"}]},
            {},
            "spool_full",
            "critical",
        ),
        ({"quarantined": 3}, {}, "quarantined", "warning"),
        ({}, {"state": "source_unavailable"}, "source_unavailable", "warning"),
        (
            {},
            {"state": "degraded", "reason_code": "bad_quality", "channels": [{"key": "pv"}]},
            "bad_quality",
            "warning",
        ),
    ],
)
def test_each_rule(collector: dict, acquisition: dict, code: str, severity: str) -> None:
    faults = instrument_faults(status(collector, acquisition))
    match = [f for f in faults if f["code"] == code]
    assert len(match) == 1, faults
    assert match[0]["severity"] == severity
    assert len(match[0]["next_step"]) > 20


def test_online_only_checks_are_skipped_when_offline() -> None:
    s = status({"connectivity": "offline", "clock_offset_s": 99, "backlog_age_s": 999})
    assert codes(s) == ["collector_offline"]


def test_critical_faults_come_first() -> None:
    s = status({"clock_offset_s": 30, "upload_fault": "authentication refused (401)"})
    assert codes(s) == ["upload_auth", "clock_offset"]
