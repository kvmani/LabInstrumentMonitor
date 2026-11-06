"""Shared fixtures.

PostgreSQL: set ``LABMON_TEST_PG_ADMIN_URL`` (a role allowed to CREATE DATABASE, e.g.
``postgresql+psycopg://postgres@127.0.0.1:5432/postgres``) or let the suite start a disposable
cluster with the local PostgreSQL 16 binaries. Each test gets its own database cloned from a
migrated template, so tests never share state.
"""

from __future__ import annotations

import os
import socket
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

FIXTURES = Path(__file__).parent / "fixtures"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class PgServer:
    admin_url: str
    cluster: object | None = None

    def url_for(self, dbname: str) -> str:
        return make_url(self.admin_url).set(database=dbname).render_as_string(hide_password=False)

    def admin(self) -> Engine:
        return create_engine(self.admin_url, isolation_level="AUTOCOMMIT")


@pytest.fixture(scope="session")
def pg_server() -> Iterator[PgServer]:
    url = os.environ.get("LABMON_TEST_PG_ADMIN_URL")
    if url:
        yield PgServer(url)
        return
    from labmon.devtools.pgcluster import Cluster

    try:
        datadir = Path(tempfile.mkdtemp(prefix="labmon-test-pg-", dir="/tmp"))  # noqa: S108
        os.chmod(datadir, 0o755)  # noqa: S103 - the postgres account must traverse it
        cluster = Cluster(datadir=datadir / "data", port=free_port())
        cluster.start()
    except (RuntimeError, OSError) as exc:
        pytest.skip(f"no PostgreSQL available: {exc}")
    try:
        yield PgServer(cluster.server_url, cluster)
    finally:
        cluster.reset()
        import shutil

        shutil.rmtree(datadir, ignore_errors=True)


@pytest.fixture(scope="session")
def template_db(pg_server: PgServer) -> str:
    from labmon.server import migrate

    name = f"labmon_tpl_{uuid.uuid4().hex[:8]}"
    eng = pg_server.admin()
    with eng.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    migrate.upgrade(pg_server.url_for(name))
    eng.dispose()
    return name


@pytest.fixture
def db_url(pg_server: PgServer, template_db: str) -> Iterator[str]:
    name = f"labmon_t_{uuid.uuid4().hex[:10]}"
    eng = pg_server.admin()
    with eng.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"'))
    yield pg_server.url_for(name)
    with eng.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    eng.dispose()


@pytest.fixture
def empty_db_url(pg_server: PgServer) -> Iterator[str]:
    name = f"labmon_e_{uuid.uuid4().hex[:10]}"
    eng = pg_server.admin()
    with eng.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    yield pg_server.url_for(name)
    with eng.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    eng.dispose()


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    from labmon.server.db import make_engine

    eng = make_engine(db_url, pool_size=8)
    yield eng
    eng.dispose()


class FakeNow:
    """Mutable server clock for deterministic status tests."""

    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


@pytest.fixture
def t0() -> datetime:
    return datetime(2026, 11, 6, 8, 0, 0, tzinfo=UTC)


@pytest.fixture
def fake_now(t0: datetime) -> FakeNow:
    return FakeNow(t0)
