"""Background worker process (``labmon-server rollup-worker``).

Runs outside the API process so slow aggregation never blocks request workers. Safe to run more
than one instance: each minute is cleared only for the revision it recomputed.
"""

from __future__ import annotations

import logging
import signal
import threading
from datetime import UTC, datetime
from types import FrameType

from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from labmon.server.rollup import process_dirty

log = logging.getLogger("labmon.worker")


def drain(engine: Engine, batch: int = 5000) -> int:
    total = 0
    while True:
        n = process_dirty(engine, datetime.now(UTC), limit=batch)
        total += n
        if n == 0:
            return total


def run_rollup_worker(engine: Engine, once: bool = False, interval_s: float = 2.0) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if once:
        log.info("rollups recomputed: %d minutes", drain(engine))
        return 0
    stop = threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    log.info("rollup worker started (interval %.1fs)", interval_s)
    while not stop.is_set():
        try:
            n = drain(engine)
            if n:
                log.info("rollups recomputed: %d minutes", n)
        except OperationalError as exc:  # database restarting: retry after the interval
            log.warning("rollup pass failed: %s", exc.__class__.__name__)
        stop.wait(interval_s)
    return 0
