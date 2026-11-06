"""Clocks. Scheduling uses the monotonic clock; records carry wall-clock UTC capture time.

``VirtualClock`` advances only when told to, so unit tests use fake time and never sleep.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def now(self) -> datetime: ...

    def sleep(self, seconds: float, stop: threading.Event | None = None) -> bool:
        """Sleep; return True if interrupted by ``stop``."""
        ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float, stop: threading.Event | None = None) -> bool:
        if seconds <= 0:
            return bool(stop and stop.is_set())
        if stop is None:
            time.sleep(seconds)
            return False
        return stop.wait(seconds)


class VirtualClock:
    """Deterministic clock: ``sleep`` advances time at once; ``jump_wall`` steps the wall clock."""

    def __init__(self, start: datetime, monotonic_start: float = 1000.0) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self._wall = start.astimezone(UTC)
        self._mono = monotonic_start
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self._mono

    def now(self) -> datetime:
        with self._lock:
            return self._wall

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._mono += seconds
            self._wall += timedelta(seconds=seconds)

    def jump_wall(self, seconds: float) -> None:
        """Step the wall clock without moving monotonic time (NTP step, manual change)."""
        with self._lock:
            self._wall += timedelta(seconds=seconds)

    def sleep(self, seconds: float, stop: threading.Event | None = None) -> bool:
        if stop is not None and stop.is_set():
            return True
        if seconds > 0:
            self.advance(seconds)
        return bool(stop and stop.is_set())
