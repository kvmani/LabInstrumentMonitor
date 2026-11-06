"""``labmon-collector`` command line.

labmon-collector run --config collector.toml       # acquire, spool, upload, heartbeat
labmon-collector status --config collector.toml    # pending / acknowledged counts
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
from pathlib import Path
from types import FrameType

import httpx

from labmon.collector.acquisition import Acquisition
from labmon.collector.config import CollectorConfig, load_config
from labmon.collector.outbox import Outbox
from labmon.collector.uploader import Heartbeat, Uploader
from labmon.sim.clock import SystemClock

log = logging.getLogger("labmon.collector")


def open_outbox(cfg: CollectorConfig) -> Outbox:
    return Outbox(
        cfg.spool_path,
        cfg.collector_id,
        capacity=cfg.spool_capacity,
        diagnostic_reserve=cfg.diagnostic_reserve,
        journal_mode=cfg.journal_mode,
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
    outbox = open_outbox(cfg)
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


def cmd_quarantine(cfg: CollectorConfig) -> int:
    outbox = open_outbox(cfg)
    try:
        print(json.dumps(outbox.quarantined(), indent=2))
    finally:
        outbox.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="labmon-collector")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "status", "quarantine"):
        p = sub.add_parser(name)
        p.add_argument("--config", type=Path, required=True)
        if name == "status":
            p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is noise
    cfg = load_config(args.config)
    if args.command == "run":
        return cmd_run(cfg)
    if args.command == "status":
        return cmd_status(cfg, args.json)
    return cmd_quarantine(cfg)


if __name__ == "__main__":
    sys.exit(main())
