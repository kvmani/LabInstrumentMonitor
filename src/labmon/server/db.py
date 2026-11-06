"""Database engine construction."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event


def make_engine(url: str, pool_size: int = 10) -> Engine:
    engine = create_engine(
        url, pool_size=pool_size, max_overflow=5, pool_pre_ping=True, pool_timeout=10, future=True
    )

    @event.listens_for(engine, "connect")
    def _session_defaults(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        with dbapi_conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute("SET statement_timeout = '30s'")
        dbapi_conn.commit()

    return engine
