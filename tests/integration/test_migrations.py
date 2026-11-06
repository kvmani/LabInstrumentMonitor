"""First migration on a real, disposable PostgreSQL 16 database."""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from labmon.server import migrate
from labmon.server.models import Base

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "lab",
    "instrument",
    "profile_revision",
    "channel",
    "collector",
    "collector_credential",
    "collector_assignment",
    "batch_receipt",
    "ingest_identity",
    "sample",
    "acquisition_event",
    "ingest_conflict",
    "app_user",
    "lab_grant",
    "user_session",
    "audit_event",
}


def test_upgrade_downgrade_upgrade(empty_db_url: str) -> None:
    engine = create_engine(empty_db_url)
    assert migrate.current_revision(engine) is None
    migrate.upgrade(empty_db_url)
    assert migrate.current_revision(engine) == migrate.head_revision() == "0001"
    tables = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES <= tables
    with engine.connect() as c:
        assert c.execute(text("SELECT version()")).scalar_one().startswith("PostgreSQL 16")
    migrate.downgrade(empty_db_url, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    migrate.upgrade(empty_db_url)
    assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_models_match_migrated_schema(db_url: str) -> None:
    engine = create_engine(db_url)
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diffs == []


def test_identity_constraints_exist(db_url: str) -> None:
    insp = inspect(create_engine(db_url))
    assert insp.get_pk_constraint("sample")["constrained_columns"] == [
        "collector_id",
        "spool_epoch",
        "seq",
    ]
    assert insp.get_pk_constraint("ingest_identity")["constrained_columns"] == [
        "collector_id",
        "spool_epoch",
        "seq",
    ]
    uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints("batch_receipt")}
    assert ("collector_id", "batch_id") in uniques
    idx = {i["name"]: i["column_names"] for i in insp.get_indexes("sample")}
    assert idx["ix_sample_channel_time"][:2] == ["channel_id", "event_time"]
