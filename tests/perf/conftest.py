"""Slow evidence suites (long fixtures, latency). Run with LABMON_RUN_SLOW=1 or
``scripts/check.sh --slow``; skipped otherwise to keep the default gate fast."""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("LABMON_RUN_SLOW") == "1":
        return
    skip = pytest.mark.skip(reason="slow suite: set LABMON_RUN_SLOW=1")
    for item in items:
        if "tests/perf" in str(item.fspath):
            item.add_marker(skip)
