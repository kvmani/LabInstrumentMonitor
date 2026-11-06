"""``labmon-collector`` command line.

labmon-collector run --config collector.toml       # acquire, spool, upload, heartbeat
labmon-collector status --config collector.toml    # pending / acknowledged counts
labmon-collector flush --config collector.toml     # upload the backlog only, then exit
labmon-collector quarantine --config collector.toml
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import ssl
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType

import httpx

from labmon.collector.acquisition import Acquisition
from labmon.collector.config import CollectorConfig, load_config
from labmon.collector.outbox import Outbox, OutboxError
from labmon.collector.uploader import Heartbeat, StepResult, Uploader
from labmon.sim.clock import SystemClock, VirtualClock

log = logging.getLogger("labmon.collector")


def open_outbox(cfg: CollectorConfig, read_only: bool = False) -> Outbox:
    return Outbox(
        cfg.spool_path,
        cfg.collector_id,
        capacity=cfg.spool_capacity,
        diagnostic_reserve=cfg.diagnostic_reserve,
        journal_mode=cfg.journal_mode,
        read_only=read_only,
    )


def make_client(cfg: CollectorConfig) -> httpx.Client:
    verify: ssl.SSLContext | bool = True
    if cfg.ca_file is not None:
        verify = ssl.create_default_context(cafile=str(cfg.ca_file))
    return httpx.Client(
        base_url=cfg.base_url,
        timeout=cfg.request_timeout_s,
        verify=verify,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
    )


def cmd_run(cfg: CollectorConfig) -> int:
    clock = SystemClock()
    token = cfg.read_token()
    outbox = open_outbox(cfg)
    stop = threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("signal %s: stopping", signum)
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    acq = Acquisition(outbox, clock, cfg.instruments, simulated=True)
    up_client, hb_client = make_client(cfg), make_client(cfg)
    uploader = Uploader(
        outbox, up_client, token, clock, max_backoff_s=cfg.max_backoff_s, flush_s=cfg.flush_s
    )
    hb = Heartbeat(
        outbox, hb_client, token, clock, acq.source_states, lambda: acq.sampling, uploader
    )
    threads = [
        threading.Thread(target=acq.run, args=(stop,), name="acquisition"),
        threading.Thread(
            target=uploader.run,
            args=(stop, cfg.replay_retention_days * 86400),
            name="uploader",
            daemon=True,
        ),
        threading.Thread(
            target=hb.run, args=(stop, cfg.heartbeat_s), name="heartbeat", daemon=True
        ),
    ]
    c = outbox.counts()
    log.info(
        "collector %s epoch %d journal=%s sqlite=%s unacknowledged=%d",
        cfg.collector_id,
        c.epoch,
        c.journal_mode,
        c.sqlite_version,
        c.unacknowledged,
    )
    for t in threads:
        t.start()
    exit_code = 0
    try:
        while not stop.is_set():
            stop.wait(1.0)
            if not threads[0].is_alive() and not stop.is_set():
                # acquisition died (e.g. spool I/O error): exit non-zero so the service manager
                # restarts the collector; persisted records are recovered from the outbox
                log.critical("acquisition thread stopped unexpectedly; exiting")
                exit_code = 1
                break
    finally:
        stop.set()
        threads[0].join(timeout=10)
        for t in threads[1:]:
            t.join(timeout=cfg.request_timeout_s + 2)
        up_client.close()
        hb_client.close()
        outbox.close()
    return exit_code


def cmd_status(cfg: CollectorConfig, as_json: bool) -> int:
    try:
        outbox = open_outbox(cfg, read_only=True)
    except OutboxError as exc:
        print(f"labmon-collector: {exc}", file=sys.stderr)
        return 3
    try:
        c = outbox.counts()
    finally:
        outbox.close()
    if as_json:
        print(json.dumps(c.as_dict(), indent=2))
    else:
        print(f"collector        {c.collector_id}")
        print(f"spool epoch      {c.epoch} (next seq {c.next_seq})")
        print(f"pending          {c.pending}")
        print(f"in open batches  {c.batched} ({c.open_batches} batches)")
        print(f"acknowledged     {c.acked_cached} (replay cache)")
        print(f"quarantined      {c.quarantined}")
        print(f"oldest pending   {c.oldest_pending_capture_time or '-'}")
        print(f"capacity         {c.unacknowledged}/{c.capacity}")
        print(f"journal          {c.journal_mode} (SQLite {c.sqlite_version})")
        print(f"upload fault     {c.upload_fault or '-'}")
    return 0


def cmd_backfill(cfg: CollectorConfig, hours: float) -> int:
    """Development/rehearsal only: spool simulated history for the last ``hours`` on a virtual
    clock. Records carry their original (past) capture times; acquisition events are marked
    ``mode=historical_playback`` so playback stays distinguishable from live acquisition.
    Stop the live collector first: one process owns the spool. ``run`` uploads the backlog."""
    if not 0 < hours <= 72:
        print("--hours must be in (0, 72]", file=sys.stderr)
        return 2
    real_now = datetime.now(UTC)
    clock = VirtualClock(real_now - timedelta(hours=hours))
    outbox = Outbox(
        cfg.spool_path,
        cfg.collector_id,
        capacity=cfg.spool_capacity,
        diagnostic_reserve=cfg.diagnostic_reserve,
        journal_mode=cfg.journal_mode,
        now=clock.now,
    )
    try:
        acq = Acquisition(
            outbox, clock, cfg.instruments, simulated=True, mode="historical_playback"
        )
        acq.start()
        while clock.now() < real_now:
            clock.advance(max(acq.seconds_until_due(), 0.0))
            acq.run_due()
        acq.stop()
        c = outbox.counts()
        print(
            f"spooled {acq.stats.frames} frames ({acq.stats.samples} samples); "
            f"{c.unacknowledged} records await upload"
        )
    finally:
        outbox.close()
    return 0


def cmd_flush(cfg: CollectorConfig, timeout_s: float) -> int:
    """Upload what the outbox holds without acquiring, then exit. Use with the collector service
    stopped (it takes the spool lock), e.g. before retiring a collector host. Exit 0 when the
    outbox is empty, 1 on a fault or when the timeout passes with records still unacknowledged."""
    clock = SystemClock()
    token = cfg.read_token()
    try:
        outbox = open_outbox(cfg)
    except OutboxError as exc:
        print(f"labmon-collector: {exc}", file=sys.stderr)
        return 3
    client = make_client(cfg)
    uploader = Uploader(outbox, client, token, clock, max_backoff_s=min(cfg.max_backoff_s, 5.0))
    deadline = clock.monotonic() + timeout_s
    try:
        while True:
            result, wait = uploader.step()
            if result is StepResult.IDLE and outbox.counts().unacknowledged == 0:
                print("outbox empty: every record is acknowledged")
                return 0
            if result is StepResult.FAULT:
                print(f"labmon-collector: upload fault: {uploader.fault}", file=sys.stderr)
                return 1
            if clock.monotonic() + wait > deadline:
                c = outbox.counts()
                print(
                    f"labmon-collector: {c.unacknowledged} records still unacknowledged "
                    f"after {timeout_s:.0f} s",
                    file=sys.stderr,
                )
                return 1
            if wait > 0:
                clock.sleep(wait, threading.Event())
    finally:
        client.close()
        outbox.close()


def cmd_quarantine(cfg: CollectorConfig) -> int:
    outbox = open_outbox(cfg, read_only=True)
    try:
        print(json.dumps(outbox.quarantined(), indent=2))
    finally:
        outbox.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="labmon-collector")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "status", "quarantine", "backfill", "flush"):
        p = sub.add_parser(name)
        p.add_argument("--config", type=Path, required=True)
        if name == "status":
            p.add_argument("--json", action="store_true")
        if name == "backfill":
            p.add_argument("--hours", type=float, default=2.0)
        if name == "flush":
            p.add_argument("--timeout", type=float, default=300.0, help="seconds")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is noise
    cfg = load_config(args.config)
    if args.command == "run":
        return cmd_run(cfg)
    if args.command == "backfill":
        return cmd_backfill(cfg, args.hours)
    if args.command == "flush":
        return cmd_flush(cfg, args.timeout)
    if args.command == "status":
        return cmd_status(cfg, args.json)
    return cmd_quarantine(cfg)


if __name__ == "__main__":
    sys.exit(main())
