"""Disposable local PostgreSQL 16 cluster for development and tests.

This is a developer convenience, never a deployment path. The cluster listens on 127.0.0.1 only,
uses ``trust`` authentication for loopback connections and lives in a throwaway directory.

When the current user is root (common in containers) PostgreSQL refuses to run, so the commands are
executed as the ``postgres`` system account via ``runuser``.

Usage::

    labmon-devdb start            # initialise (if needed) and start, print the database URL
    labmon-devdb url              # print the URL
    labmon-devdb stop
    labmon-devdb reset            # stop and delete the cluster (designated test state only)
"""

from __future__ import annotations

import argparse
import os
import pwd
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DIR = Path(".devdata/pg")
DEFAULT_PORT = 55432
DB_NAME = "labmon"
DB_USER = "labmon"


def _pg_bindir() -> Path:
    env = os.environ.get("LABMON_PG_BINDIR")
    if env:
        return Path(env)
    for candidate in (Path("/usr/lib/postgresql/16/bin"), Path("/usr/local/pgsql/bin")):
        if (candidate / "pg_ctl").exists():
            return candidate
    found = shutil.which("pg_ctl")
    if found:
        return Path(found).parent
    raise RuntimeError("PostgreSQL 16 binaries not found; set LABMON_PG_BINDIR")


def _as_service_user(cmd: list[str]) -> list[str]:
    if os.geteuid() == 0:
        return ["runuser", "-u", "postgres", "--", *cmd]
    return cmd


@dataclass
class Cluster:
    datadir: Path
    port: int = DEFAULT_PORT

    @property
    def server_url(self) -> str:
        """URL for the maintenance database (used to create/drop disposable databases)."""
        return f"postgresql+psycopg://postgres@127.0.0.1:{self.port}/postgres"

    def database_url(self, name: str = DB_NAME, user: str = DB_USER) -> str:
        return f"postgresql+psycopg://{user}@127.0.0.1:{self.port}/{name}"

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = _as_service_user([str(_pg_bindir() / args[0]), *args[1:]])
        return subprocess.run(cmd, check=check, capture_output=True, text=True)

    def initialised(self) -> bool:
        return (self.datadir / "PG_VERSION").exists()

    def init(self) -> None:
        self.datadir.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            pw = pwd.getpwnam("postgres")
            os.chown(self.datadir, pw.pw_uid, pw.pw_gid)
            # every parent must be traversable by the service account; never widen it silently
            for parent in self.datadir.resolve().parents:
                if not parent.stat().st_mode & 0o001:
                    raise RuntimeError(
                        f"{parent} is not traversable by the postgres account; "
                        "choose a cluster directory such as /tmp/labmon-pg with --dir"
                    )
        os.chmod(self.datadir, 0o700)
        self._run(
            "initdb",
            "-D",
            str(self.datadir),
            "-U",
            "postgres",
            "--auth-local=trust",
            "--auth-host=trust",
            "--encoding=UTF8",
            "--locale=C.UTF-8",
        )
        with (self.datadir / "postgresql.conf").open("a") as fh:
            fh.write(
                "\n# labmon disposable dev cluster\n"
                "listen_addresses = '127.0.0.1'\n"
                f"port = {self.port}\n"
                f"unix_socket_directories = '{self.datadir.resolve()}'\n"
                "fsync = on\n"
                "timezone = 'UTC'\n"
                "log_timezone = 'UTC'\n"
                "max_connections = 50\n"
            )

    def running(self) -> bool:
        res = self._run("pg_ctl", "-D", str(self.datadir), "status", check=False)
        return res.returncode == 0

    def start(self) -> None:
        if not self.initialised():
            self.init()
        if not self.running():
            log = self.datadir / "server.log"
            self._run("pg_ctl", "-D", str(self.datadir), "-l", str(log), "-w", "start")
        self._wait_ready()
        self._ensure_role_and_db()

    def _wait_ready(self, timeout_s: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            res = self._run("pg_isready", "-h", "127.0.0.1", "-p", str(self.port), check=False)
            if res.returncode == 0:
                return
            time.sleep(0.2)
        raise RuntimeError("PostgreSQL did not become ready")

    def psql(self, sql: str, db: str = "postgres") -> str:
        res = self._run(
            "psql",
            "-h",
            "127.0.0.1",
            "-p",
            str(self.port),
            "-U",
            "postgres",
            "-d",
            db,
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            sql,
        )
        return res.stdout.strip()

    def _ensure_role_and_db(self) -> None:
        if not self.psql(f"SELECT 1 FROM pg_roles WHERE rolname = '{DB_USER}'"):  # noqa: S608 - constants
            self.psql(f"CREATE ROLE {DB_USER} LOGIN CREATEDB")
        if not self.psql(f"SELECT 1 FROM pg_database WHERE datname = '{DB_NAME}'"):  # noqa: S608 - constants
            self.psql(f"CREATE DATABASE {DB_NAME} OWNER {DB_USER}")

    def create_database(self, name: str) -> str:
        self.psql(f'CREATE DATABASE "{name}" OWNER {DB_USER}')
        return self.database_url(name)

    def drop_database(self, name: str) -> None:
        self.psql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    def stop(self) -> None:
        if self.initialised() and self.running():
            self._run("pg_ctl", "-D", str(self.datadir), "-m", "fast", "-w", "stop")

    def reset(self) -> None:
        self.stop()
        shutil.rmtree(self.datadir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="labmon-devdb", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["start", "stop", "reset", "url", "status"])
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    cluster = Cluster(datadir=args.dir.resolve(), port=args.port)
    if args.command == "start":
        cluster.start()
        print(cluster.database_url())
    elif args.command == "stop":
        cluster.stop()
    elif args.command == "reset":
        cluster.reset()
    elif args.command == "url":
        print(cluster.database_url())
    elif args.command == "status":
        print("running" if cluster.initialised() and cluster.running() else "stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
