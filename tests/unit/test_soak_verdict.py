"""The soak verdict: leaks and slow recovery fail; backlog after an injected fault does not."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from labmon.devtools.soak import verdict

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
LEDGER = {"epochs": [{"collector": "c", "epoch": 1, "records": 100, "holes": 0}], "conflicts": 0}


def sample(i: int, rss: float = 80.0, p95: float | None = 0.9, backlog: int = 10) -> dict[str, Any]:
    proc = {"pid": 1, "restarts": 0, "rss_mb": rss, "fds": 8}
    return {
        "t": (T0 + timedelta(seconds=10 * i)).isoformat(),
        "processes": {"api": proc, "worker": proc, "collector": proc},
        "ingest": {"samples": 50, "p95_latency_s": p95, "max_latency_s": p95},
        "outbox": {"unacknowledged": backlog},
    }


def fault(at_s: int, until_s: int) -> dict[str, Any]:
    return {
        "kind": "api_killed",
        "at": (T0 + timedelta(seconds=at_s)).isoformat(),
        "until": (T0 + timedelta(seconds=until_s)).isoformat(),
    }


def by_name(result: dict[str, Any]) -> dict[str, bool]:
    return {c["name"]: c["ok"] for c in result["checks"]}


def test_steady_run_passes() -> None:
    result = verdict([sample(i) for i in range(20)], True, LEDGER, [], 10)
    assert result["ok"], result


def test_memory_growth_fails() -> None:
    samples = [sample(i, rss=80 + 10 * i) for i in range(20)]
    assert not by_name(verdict(samples, True, LEDGER, [], 10))["api memory"]


def test_backlog_after_fault_is_recovery_not_latency() -> None:
    samples = [sample(i) for i in range(20)]
    samples[6] = sample(6, p95=None, backlog=300)  # API down: nothing received
    samples[7] = sample(7, p95=25.0, backlog=2000)  # backlog catching up
    faults = [fault(55, 62)]
    checks = by_name(verdict(samples, True, LEDGER, faults, 10))
    assert checks["ingest latency"] and checks["fault recovery"]
    assert faults[0]["recovered_after_s"] == 18.0  # first prompt interval at t=80 s


def test_unrecovered_fault_and_holes_fail() -> None:
    samples = [sample(i) for i in range(10)] + [sample(i, p95=40.0) for i in range(10, 20)]
    ledger = {"epochs": [{"collector": "c", "epoch": 1, "records": 99, "holes": 1}], "conflicts": 0}
    checks = by_name(verdict(samples, False, ledger, [fault(95, 100)], 10))
    assert not checks["fault recovery"]
    assert not checks["ledger complete"] and not checks["outbox drained"]


def test_interval_spanning_the_restart_is_not_recovery() -> None:
    # regression: the first sample after the restart (t=60 s) mostly covers pre-kill uploads, so
    # its low p95 must not count as recovery; the catch-up interval after it is in the window
    samples = [sample(i) for i in range(20)]
    samples[6] = sample(6, p95=0.8, backlog=80)
    samples[7] = sample(7, p95=12.0)
    faults = [fault(52, 58)]
    checks = by_name(verdict(samples, True, LEDGER, faults, 10))
    assert checks["ingest latency"], "catch-up must be excluded as recovery"
    assert faults[0]["recovered_after_s"] == 22.0  # t=80 s, the first interval after catch-up
