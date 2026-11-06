"""Real processes: the API (uvicorn) and the collector run as separate OS processes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from labmon.server.bootstrap import bootstrap_dev
from labmon.server.db import make_engine
from tests.conftest import free_port

ROOT = Path(__file__).resolve().parents[2]
BIN = Path(sys.executable).parent
PASSWORD = "e2e-password-123"


def wait_for(pred: Callable[[], bool], timeout: float, what: str, every: float = 0.25) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@dataclass
class Stack:
    db_url: str
    port: int
    workdir: Path
    logs: Path
    server: subprocess.Popen[bytes] | None = None
    collector: subprocess.Popen[bytes] | None = None
    handles: list[object] = field(default_factory=list)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def config(self) -> Path:
        return self.workdir / "collector.toml"

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("LABMON_")}
        env.update(
            LABMON_DATABASE_URL=self.db_url,
            LABMON_ENV="test",
            LABMON_STATIC_DIR=str(ROOT / "web" / "dist"),
            NO_PROXY="127.0.0.1,localhost",
        )
        return env

    def _log(self, name: str):  # type: ignore[no-untyped-def]
        fh = (self.logs / f"{name}.log").open("ab")
        self.handles.append(fh)
        return fh

    def start_server(self) -> None:
        self.server = subprocess.Popen(
            [str(BIN / "labmon-server"), "serve", "--port", str(self.port)],
            env=self._env(),
            stdout=self._log("server"),
            stderr=subprocess.STDOUT,
        )

        def ready() -> bool:
            try:
                return httpx.get(f"{self.base}/health/ready", timeout=1).status_code == 200
            except httpx.HTTPError:
                return False

        wait_for(ready, 30, "API readiness")

    def kill_server(self) -> None:
        assert self.server is not None
        self.server.send_signal(signal.SIGKILL)
        self.server.wait(10)
        self.server = None

    def start_collector(self) -> None:
        self.collector = subprocess.Popen(
            [str(BIN / "labmon-collector"), "run", "--config", str(self.config)],
            env=self._env(),
            stdout=self._log("collector"),
            stderr=subprocess.STDOUT,
        )

    def stop_collector(self, sig: int = signal.SIGTERM) -> None:
        assert self.collector is not None
        self.collector.send_signal(sig)
        self.collector.wait(20)
        self.collector = None

    def outbox(self) -> dict:  # type: ignore[type-arg]
        out = subprocess.run(
            [str(BIN / "labmon-collector"), "status", "--config", str(self.config), "--json"],
            env=self._env(),
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(out.stdout)  # type: ignore[no-any-return]

    def close(self) -> None:
        for proc in (self.collector, self.server):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(10)
        for fh in self.handles:
            fh.close()  # type: ignore[attr-defined]


@pytest.fixture
def stack(db_url: str, tmp_path: Path) -> Iterator[Stack]:
    port = free_port()
    workdir = tmp_path / "collector"
    engine = make_engine(db_url, pool_size=2)
    bootstrap_dev(engine, workdir, f"http://127.0.0.1:{port}", PASSWORD)
    engine.dispose()
    with (workdir / "collector.toml").open("a") as fh:
        fh.write("\n")  # keep the generated file valid when we append top-level keys
    text = (workdir / "collector.toml").read_text()
    # faster recovery for tests; inserted before the first [[instruments]] table
    text = text.replace(
        "[[instruments]]", "max_backoff_s = 2.0\nflush_s = 0.5\n\n[[instruments]]", 1
    )
    (workdir / "collector.toml").write_text(text)
    logs = tmp_path / "logs"
    logs.mkdir()
    s = Stack(db_url, port, workdir, logs)
    try:
        yield s
    finally:
        s.close()
        for name in ("server", "collector"):
            log = logs / f"{name}.log"
            if log.exists():
                sys.stderr.write(f"\n--- {name}.log (tail) ---\n")
                sys.stderr.write("\n".join(log.read_text(errors="replace").splitlines()[-15:]))
