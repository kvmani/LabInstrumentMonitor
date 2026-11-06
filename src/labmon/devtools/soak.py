"""Simulated soak run (``labmon-soak``): the whole stack as real processes for hours.

Development and rehearsal only. It bootstraps the demo fleet into a *dedicated* database, starts
the API, the worker and the collector as separate OS processes, optionally kills and restarts the
API on a schedule, and samples every ``--interval`` seconds:

* resident memory and open file descriptors of each process (Linux ``/proc``);
* the collector outbox (unacknowledged records, oldest pending capture, upload fault);
* ingest latency (``receive_time - capture_time``) of samples received in the interval;
* staleness of the newest sample per instrument, database size, dirty rollup minutes.

At the end the collector is stopped gracefully, the outbox must drain, and the record ledger must
be complete (no sequence holes per spool epoch, no ingest conflicts). Samples go to
``samples.jsonl`` and the verdict to ``summary.json`` in ``--out``. Exit status 0 means every
check passed. All evidence it produces is **simulated**.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from labmon.server.bootstrap import bootstrap_dev
from labmon.server.db import make_engine
from labmon.server.migrate import upgrade

BIN = Path(sys.executable).parent

# thresholds for the verdict; generous enough for a shared CI host, tight enough to catch leaks
MAX_RSS_GROWTH_MB = 64.0
MAX_FD_GROWTH = 32
MAX_P95_LATENCY_S = 10.0
DRAIN_TIMEOUT_S = 120.0
MAX_RECOVERY_S = 90.0  # collector backoff cap (60 s by default) plus catch-up
STEADY_BACKLOG = 500  # records; a few seconds of the demo fleet


@dataclass
class Proc:
    name: str
    argv: list[str]
    popen: subprocess.Popen[bytes] | None = None
    restarts: int = 0


@dataclass
class Soak:
    db_url: str
    out: Path
    port: int
    restart_api_every_s: float | None
    fast_backoff: bool
    procs: dict[str, Proc] = field(default_factory=dict)
    logs: list[IO[bytes]] = field(default_factory=list)
    faults: list[dict[str, Any]] = field(default_factory=list)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def config(self) -> Path:
        return self.out / "collector" / "collector.toml"

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("LABMON_")}
        env.update(
            LABMON_DATABASE_URL=self.db_url,
            LABMON_ENV="test",
            LABMON_EXPORT_DIR=str(self.out / "exports"),
            NO_PROXY="127.0.0.1,localhost",
        )
        return env

    # --- processes -------------------------------------------------------------------------

    def prepare(self, engine: Engine) -> None:
        upgrade(self.db_url)
        bootstrap_dev(engine, self.out / "collector", self.base, None, with_dsc=True)
        if self.fast_backoff:
            cfg = self.config.read_text()
            self.config.write_text(
                cfg.replace("[[instruments]]", "max_backoff_s = 2.0\n\n[[instruments]]", 1)
            )
        self.procs = {
            "api": Proc("api", [str(BIN / "labmon-server"), "serve", "--port", str(self.port)]),
            "worker": Proc("worker", [str(BIN / "labmon-server"), "worker"]),
            "collector": Proc(
                "collector", [str(BIN / "labmon-collector"), "run", "--config", str(self.config)]
            ),
        }

    def start(self, name: str) -> None:
        p = self.procs[name]
        log = (self.out / f"{name}.log").open("ab")
        self.logs.append(log)
        p.popen = subprocess.Popen(p.argv, env=self.env(), stdout=log, stderr=subprocess.STDOUT)
        if name == "api":
            _wait(self._ready, 30, "API readiness")

    def stop(self, name: str, sig: int = signal.SIGTERM, timeout: float = 30) -> None:
        p = self.procs[name]
        if p.popen is not None and p.popen.poll() is None:
            p.popen.send_signal(sig)
            try:
                p.popen.wait(timeout)
            except subprocess.TimeoutExpired:
                p.popen.kill()
                p.popen.wait(10)
        p.popen = None

    def close(self) -> None:
        for name in ("collector", "worker", "api"):
            if name in self.procs:
                self.stop(name, signal.SIGKILL)
        for log in self.logs:
            log.close()

    def _ready(self) -> bool:
        try:
            return httpx.get(f"{self.base}/health/ready", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    def inject_api_kill(self) -> None:
        started = datetime.now(UTC)
        self.stop("api", signal.SIGKILL)
        time.sleep(5)
        self.start("api")
        self.procs["api"].restarts += 1
        self.faults.append({"kind": "api_killed", "at": started.isoformat(), "until": _now_iso()})

    # --- measurements ----------------------------------------------------------------------

    def outbox(self) -> dict[str, Any]:
        out = subprocess.run(
            [str(BIN / "labmon-collector"), "status", "--config", str(self.config), "--json"],
            env=self.env(),
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0:
            return {"error": out.stderr.strip()[-300:]}
        data: dict[str, Any] = json.loads(out.stdout)
        return data

    def sample(self, engine: Engine, since: datetime) -> dict[str, Any]:
        procs = {}
        for name, p in self.procs.items():
            pid = p.popen.pid if p.popen is not None and p.popen.poll() is None else None
            procs[name] = {"pid": pid, "restarts": p.restarts, **_proc_stats(pid)}
        with engine.connect() as c:
            lat = c.execute(
                text(
                    "SELECT count(*) AS n,"
                    " percentile_cont(0.95) WITHIN GROUP (ORDER BY"
                    "   extract(epoch FROM receive_time - capture_time)) AS p95,"
                    " max(extract(epoch FROM receive_time - capture_time)) AS worst"
                    " FROM sample WHERE receive_time > :since"
                ),
                {"since": since},
            ).one()
            stale = {
                r.instrument_id: round(float(r.age), 1)
                for r in c.execute(
                    text(
                        "SELECT instrument_id, extract(epoch FROM now() - max(event_time)) AS age"
                        " FROM sample GROUP BY instrument_id"
                    )
                )
            }
            db_bytes = c.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
            dirty = c.execute(text("SELECT count(*) FROM rollup_dirty")).scalar_one()
        box = self.outbox()
        return {
            "t": _now_iso(),
            "processes": procs,
            "ingest": {
                "samples": int(lat.n),
                "p95_latency_s": None if lat.p95 is None else round(float(lat.p95), 3),
                "max_latency_s": None if lat.worst is None else round(float(lat.worst), 3),
            },
            "staleness_s": stale,
            "outbox": {
                k: box.get(k)
                for k in (
                    "unacknowledged",
                    "pending",
                    "oldest_pending_capture_time",
                    "upload_fault",
                    "error",
                )
                if k in box
            },
            "db_bytes": int(db_bytes),
            "rollup_dirty_minutes": int(dirty),
        }


def ledger_check(engine: Engine) -> dict[str, Any]:
    """Every spooled record of each epoch reached the ledger exactly once and nothing conflicted."""
    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT collector_id, spool_epoch, count(*) AS n, min(seq) AS lo, max(seq) AS hi"
                " FROM ingest_identity GROUP BY collector_id, spool_epoch"
            )
        ).all()
        conflicts = c.execute(text("SELECT count(*) FROM ingest_conflict")).scalar_one()
    epochs = [
        {
            "collector": r.collector_id,
            "epoch": int(r.spool_epoch),
            "records": int(r.n),
            "holes": int(r.hi - r.lo + 1 - r.n),
        }
        for r in rows
    ]
    return {"epochs": epochs, "conflicts": int(conflicts)}


def verdict(
    samples: list[dict[str, Any]],
    drained: bool,
    ledger: dict[str, Any],
    faults: list[dict[str, Any]],
    interval_s: float,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    # memory and descriptors: compare after warm-up (first quarter) with the end, per process
    # lifetime (a restarted API starts a fresh baseline)
    warm = samples[len(samples) // 4 :] if len(samples) >= 4 else samples
    for name in ("api", "worker", "collector"):
        series = [s["processes"][name] for s in warm if s["processes"][name].get("rss_mb")]
        if not series:
            check(f"{name} memory", False, "no measurements")
            continue
        last_pid = series[-1]["pid"]
        same = [p for p in series if p["pid"] == last_pid]
        rss_growth = same[-1]["rss_mb"] - same[0]["rss_mb"]
        fd_growth = same[-1]["fds"] - same[0]["fds"]
        check(
            f"{name} memory",
            rss_growth <= MAX_RSS_GROWTH_MB,
            f"RSS {same[0]['rss_mb']:.1f} → {same[-1]['rss_mb']:.1f} MB over "
            f"{len(same)} samples (limit +{MAX_RSS_GROWTH_MB:.0f} MB)",
        )
        check(
            f"{name} descriptors",
            fd_growth <= MAX_FD_GROWTH,
            f"open fds {same[0]['fds']} → {same[-1]['fds']} (limit +{MAX_FD_GROWTH})",
        )

    # a fault lasts from the kill until the first later interval with fresh, prompt uploads and
    # a near-empty outbox; steady-state latency is judged outside those windows
    def ts(t: str) -> float:
        return datetime.fromisoformat(t).timestamp()

    windows: list[tuple[float, float]] = []
    recoveries: list[float | None] = []
    for f in faults:
        lo, up = ts(f["at"]), ts(f["until"])
        # the interval must *start* after the restart: the first sample after it mostly holds
        # readings uploaded before the kill and says nothing about catch-up
        rec = next(
            (
                ts(s["t"])
                for prev, s in zip(samples, samples[1:], strict=False)
                if ts(prev["t"]) >= up
                and s["ingest"]["p95_latency_s"] is not None
                and s["ingest"]["p95_latency_s"] <= MAX_P95_LATENCY_S
                and (s["outbox"].get("unacknowledged") or 0) <= STEADY_BACKLOG
            ),
            None,
        )
        f["recovered_after_s"] = None if rec is None else round(rec - up, 1)
        recoveries.append(f["recovered_after_s"])
        windows.append((lo, rec if rec is not None else float("inf")))

    clean = [
        s["ingest"]["p95_latency_s"]
        for s in samples
        if s["ingest"]["p95_latency_s"] is not None
        and not any(lo <= ts(s["t"]) <= hi for lo, hi in windows)
    ]
    worst = max(clean) if clean else None
    check(
        "ingest latency",
        worst is not None and worst <= MAX_P95_LATENCY_S,
        f"worst interval p95 {worst} s outside fault recovery (limit {MAX_P95_LATENCY_S} s, "
        f"{len(clean)} intervals)",
    )
    if faults:
        slowest = None if None in recoveries else max(r or 0.0 for r in recoveries)
        check(
            "fault recovery",
            slowest is not None and slowest <= MAX_RECOVERY_S,
            f"{len(faults)} API kills; slowest recovery {slowest} s after restart "
            f"(limit {MAX_RECOVERY_S:.0f} s)",
        )
    check(
        "outbox drained",
        drained,
        "collector stopped gracefully and the outbox emptied"
        if drained
        else f"records still unacknowledged after {DRAIN_TIMEOUT_S:.0f} s",
    )
    holes = sum(e["holes"] for e in ledger["epochs"])
    records = sum(e["records"] for e in ledger["epochs"])
    check(
        "ledger complete",
        holes == 0 and records > 0 and ledger["conflicts"] == 0,
        f"{records} records, {holes} sequence holes, {ledger['conflicts']} conflicts",
    )
    check(
        "API alive",
        bool(samples) and samples[-1]["processes"]["api"]["pid"] is not None,
        "API process running at the last sample",
    )
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def run(
    db_url: str,
    out: Path,
    duration_s: float,
    interval_s: float,
    restart_api_every_s: float | None,
    port: int,
    fast_backoff: bool = False,
) -> int:
    out.mkdir(parents=True, exist_ok=True)
    engine = make_engine(db_url, pool_size=1)
    soak = Soak(db_url, out, port, restart_api_every_s, fast_backoff)
    samples: list[dict[str, Any]] = []
    started = datetime.now(UTC)
    try:
        soak.prepare(engine)
        for name in ("api", "worker", "collector"):
            soak.start(name)
        t0 = time.monotonic()
        next_fault = t0 + restart_api_every_s if restart_api_every_s else None
        since = datetime.now(UTC)
        with (out / "samples.jsonl").open("w") as fh:
            while time.monotonic() - t0 < duration_s:
                time.sleep(interval_s)
                if next_fault is not None and time.monotonic() >= next_fault:
                    soak.inject_api_kill()
                    next_fault = time.monotonic() + (restart_api_every_s or 0)
                row = soak.sample(engine, since)
                since = datetime.fromisoformat(row["t"])
                samples.append(row)
                fh.write(json.dumps(row) + "\n")
                fh.flush()
        soak.stop("collector", signal.SIGTERM)
        drained = _drain(soak)
        ledger = ledger_check(engine)
        result = verdict(samples, drained, ledger, soak.faults, interval_s)
    finally:
        soak.close()
        engine.dispose()
    summary = {
        "evidence": "simulated",
        "started": started.isoformat(),
        "ended": _now_iso(),
        "duration_s": duration_s,
        "interval_s": interval_s,
        "faults": soak.faults,
        "ledger": ledger,
        **result,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    for c in result["checks"]:
        print(f"  {'✓' if c['ok'] else '✕'} {c['name']:22} {c['detail']}")
    print("soak:", "OK" if result["ok"] else "FAIL", f"({out / 'summary.json'})")
    return 0 if result["ok"] else 1


def _drain(soak: Soak) -> bool:
    """With the collector stopped, upload the remaining backlog with ``labmon-collector flush``."""
    out = subprocess.run(
        [
            str(BIN / "labmon-collector"),
            "flush",
            "--config",
            str(soak.config),
            "--timeout",
            str(DRAIN_TIMEOUT_S),
        ],
        env=soak.env(),
        capture_output=True,
        text=True,
        check=False,
    )
    return out.returncode == 0 and soak.outbox().get("unacknowledged") == 0


def _proc_stats(pid: int | None) -> dict[str, Any]:
    if pid is None:
        return {}
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        rss_kb = next(
            int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS:")
        )
        fds = len(os.listdir(f"/proc/{pid}/fd"))
    except (OSError, StopIteration):
        return {}
    return {"rss_mb": round(rss_kb / 1024, 1), "fds": fds}


def _wait(pred: Any, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.25)
    raise RuntimeError(f"timed out after {timeout}s waiting for {what}")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="labmon-soak", description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--database-url",
        default=os.environ.get("LABMON_DATABASE_URL"),
        help="a dedicated database; its demo lab is (re)bootstrapped",
    )
    ap.add_argument("--out", type=Path, default=Path("test-results/soak"))
    ap.add_argument("--hours", type=float, default=72.0)
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between samples")
    ap.add_argument(
        "--restart-api-every",
        type=float,
        default=None,
        metavar="MINUTES",
        help="SIGKILL and restart the API on this period",
    )
    ap.add_argument("--port", type=int, default=8077)
    args = ap.parse_args(argv)
    if os.environ.get("LABMON_ENV") == "production":
        ap.error("refusing to run with LABMON_ENV=production")
    if not args.database_url:
        ap.error("--database-url or LABMON_DATABASE_URL is required")
    every = args.restart_api_every * 60 if args.restart_api_every else None
    return run(args.database_url, args.out, args.hours * 3600, args.interval, every, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
