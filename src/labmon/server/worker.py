"""Background worker process (``labmon-server worker``): minute rollups and durable exports.

Runs outside the API process so aggregation and exports never block request workers. Safe to run
more than one instance: rollup minutes are cleared only for the revision recomputed, and export
jobs are claimed with leases (``FOR UPDATE SKIP LOCKED``).
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from datetime import UTC, datetime
from types import FrameType

from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from labmon.server.exports import process_exports
from labmon.server.rollup import process_dirty
from labmon.server.settings import Settings

log = logging.getLogger("labmon.worker")


def drain(engine: Engine, batch: int = 5000) -> int:
    total = 0
    while True:
        n = process_dirty(engine, datetime.now(UTC), limit=batch)
        total += n
        if n == 0:
            return total


def run_rollup_worker(
    engine: Engine, once: bool = False, interval_s: float = 2.0, settings: Settings | None = None
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker_id = f"{socket.gethostname()}:{os.getpid()}"

    def exports() -> int:
        if settings is None:
            return 0
        return process_exports(engine, settings, worker_id, lambda: datetime.now(UTC))

    if once:
        log.info("rollups recomputed: %d minutes; exports: %d", drain(engine), exports())
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
            e = exports()
            if e:
                log.info("exports processed: %d", e)
        except OperationalError as exc:  # database restarting: retry after the interval
            log.warning("rollup pass failed: %s", exc.__class__.__name__)
        stop.wait(interval_s)
    return 0
