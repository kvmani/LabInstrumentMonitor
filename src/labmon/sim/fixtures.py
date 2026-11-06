"""Seeded long-history fixtures with machine-readable expected answers.

``furnace-30d``: the simulated furnace at 1 Hz for 30 days from 2026-09-01T00:00Z (seed 2026)
with exactly two scripted anomalies:

* a **one-sample over-temperature** of +420 °C on the process value at day 17, 12:34:56 UTC;
* a **dropout** (source unavailable) from day 9 03:00:00 to 05:30:00 UTC (9000 missing
  seconds), recorded as ``source_unavailable`` / ``source_recovered`` events.

The fixture is generated with the same source code as live acquisition and loaded through the
documented test path (``labmon-server load-fixture``), keeping original event times. Its
``expected()`` values are what the 30-day view must still show after aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from labmon.sim.base import Fault, FaultKind
from labmon.sim.furnace import FurnaceSource


@dataclass(frozen=True)
class Furnace30dFixture:
    key: str = "furnace-30d"
    instrument_id: str = "furnace-hist"
    name: str = "Furnace F-H (30-day fixture)"
    collector_id: str = "col-fixture"
    seed: int = 2026
    start: datetime = datetime(2026, 9, 1, tzinfo=UTC)
    days: int = 30
    spike_offset: timedelta = timedelta(days=17, hours=12, minutes=34, seconds=56)
    spike_c: float = 420.0
    dropout_offset: timedelta = timedelta(days=9, hours=3)
    dropout: timedelta = timedelta(hours=2, minutes=30)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(days=self.days)

    @property
    def spike_at(self) -> datetime:
        return self.start + self.spike_offset

    @property
    def dropout_start(self) -> datetime:
        return self.start + self.dropout_offset

    @property
    def dropout_end(self) -> datetime:
        return self.dropout_start + self.dropout

    def faults(self) -> tuple[Fault, ...]:
        spike = self.spike_at.timestamp()
        return (
            Fault(FaultKind.SPIKE, spike, spike + 1, magnitude=self.spike_c),
            Fault(FaultKind.DROPOUT, self.dropout_start.timestamp(), self.dropout_end.timestamp()),
        )

    def source(self) -> FurnaceSource:
        return FurnaceSource(seed=self.seed, faults=self.faults())

    def expected(self) -> dict[str, Any]:
        t_ms = int(self.spike_at.timestamp() * 1000)
        peak = next(c.value for c in self.source().read(t_ms).channels if c.key == "pv")
        total = self.days * 86400
        missing = int(self.dropout.total_seconds())
        return {
            "fixture": self.key,
            "range": {"start": self.start, "end": self.end},
            "samples_per_channel": total - missing,
            "coverage": (total - missing) / total,
            "pv_max": {"value": peak, "time": self.spike_at},
            "dropout": {
                "last_before": self.dropout_start - timedelta(seconds=1),
                "first_after": self.dropout_end,
                "missing_seconds": missing,
            },
        }


FIXTURES = {"furnace-30d": Furnace30dFixture()}
