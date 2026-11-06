"""PostgreSQL schema (SQLAlchemy 2). The Alembic migrations are the source of truth for DDL; this
module mirrors them for queries and is checked against them by tests/integration/test_migrations.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class Lab(Base):
    __tablename__ = "lab"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Instrument(Base):
    __tablename__ = "instrument"
    __table_args__ = (
        CheckConstraint(
            "lifecycle IN ('draft','active','maintenance','retired')", name="lifecycle"
        ),
    )
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    lab_id: Mapped[str] = mapped_column(ForeignKey("lab.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    asset_id: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    lifecycle: Mapped[str] = mapped_column(Text, server_default="active")
    simulated: Mapped[bool] = mapped_column(Boolean)
    profile_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class ProfileRevision(Base):
    """Immutable published source mapping."""

    __tablename__ = "profile_revision"
    __table_args__ = (UniqueConstraint("instrument_id", "revision"),)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instrument.id"))
    revision: Mapped[int] = mapped_column(Integer)
    profile_key: Mapped[str] = mapped_column(Text)
    adapter: Mapped[str] = mapped_column(Text)
    adapter_version: Mapped[str] = mapped_column(Text)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    definition_digest: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Channel(Base):
    __tablename__ = "channel"
    __table_args__ = (
        UniqueConstraint("instrument_id", "key"),
        CheckConstraint("data_type IN ('float','bool','enum')", name="data_type"),
    )
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instrument.id"))
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    quantity: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(Text)
    data_type: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    expected_cadence_s: Mapped[float] = mapped_column(Double)
    display_precision: Mapped[int] = mapped_column(Integer)
    enum_labels: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")


class Collector(Base):
    __tablename__ = "collector"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    lab_id: Mapped[str] = mapped_column(ForeignKey("lab.id"))
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(TS)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(TS)  # server receive time
    last_heartbeat_collector_time: Mapped[datetime | None] = mapped_column(TS)
    last_heartbeat: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class CollectorCredential(Base):
    __tablename__ = "collector_credential"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    collector_id: Mapped[str] = mapped_column(ForeignKey("collector.id"), index=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    revoked_at: Mapped[datetime | None] = mapped_column(TS)


class CollectorAssignment(Base):
    """Which collector may acquire for which instrument, and when (history is retained)."""

    __tablename__ = "collector_assignment"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    collector_id: Mapped[str] = mapped_column(ForeignKey("collector.id"))
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instrument.id"), index=True)
    valid_from: Mapped[datetime] = mapped_column(TS)
    valid_to: Mapped[datetime | None] = mapped_column(TS)
    cutoff_epoch: Mapped[int | None] = mapped_column(BigInteger)
    cutoff_seq: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class BatchReceipt(Base):
    __tablename__ = "batch_receipt"
    __table_args__ = (
        UniqueConstraint("collector_id", "batch_id"),
        CheckConstraint("outcome IN ('committed','rejected')", name="outcome"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    collector_id: Mapped[str] = mapped_column(ForeignKey("collector.id"))
    batch_id: Mapped[str] = mapped_column(Text)
    payload_digest: Mapped[str] = mapped_column(Text)
    protocol_version: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(Text)
    record_count: Mapped[int] = mapped_column(Integer)
    accepted: Mapped[int] = mapped_column(Integer)
    duplicates: Mapped[int] = mapped_column(Integer)
    rejected: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(TS)


class IngestIdentity(Base):
    """Global replay-identity ledger: one row per accepted record, independent of storage layout."""

    __tablename__ = "ingest_identity"
    __table_args__ = (PrimaryKeyConstraint("collector_id", "spool_epoch", "seq"),)
    collector_id: Mapped[str] = mapped_column(ForeignKey("collector.id"))
    spool_epoch: Mapped[int] = mapped_column(BigInteger)
    seq: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(Text)
    record_digest: Mapped[str] = mapped_column(Text)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("batch_receipt.id"))


class Sample(Base):
    __tablename__ = "sample"
    __table_args__ = (
        PrimaryKeyConstraint("collector_id", "spool_epoch", "seq"),
        Index(
            "ix_sample_channel_time",
            "channel_id",
            "event_time",
            "collector_id",
            "spool_epoch",
            "seq",
        ),
        CheckConstraint("quality IN ('good','uncertain','bad')", name="quality"),
        CheckConstraint("clock_confidence IN ('trusted','suspect')", name="clock_confidence"),
    )
    collector_id: Mapped[str] = mapped_column(Text)
    spool_epoch: Mapped[int] = mapped_column(BigInteger)
    seq: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channel.id"))
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instrument.id"))
    profile_revision_id: Mapped[str] = mapped_column(ForeignKey("profile_revision.id"))
    frame_id: Mapped[str | None] = mapped_column(Text)
    value_num: Mapped[float | None] = mapped_column(Double)
    value_int: Mapped[int | None] = mapped_column(BigInteger)
    value_bool: Mapped[bool | None] = mapped_column(Boolean)
    raw: Mapped[str | None] = mapped_column(Text)
    source_status: Mapped[str | None] = mapped_column(Text)
    quality: Mapped[str] = mapped_column(Text)
    quality_reason: Mapped[str | None] = mapped_column(Text)
    source_time: Mapped[datetime | None] = mapped_column(TS)
    source_time_meaning: Mapped[str] = mapped_column(Text)
    capture_time: Mapped[datetime] = mapped_column(TS)
    receive_time: Mapped[datetime] = mapped_column(TS)
    event_time: Mapped[datetime] = mapped_column(TS)
    event_time_source: Mapped[str] = mapped_column(Text)
    clock_confidence: Mapped[str] = mapped_column(Text)
    clock_flag: Mapped[str | None] = mapped_column(Text)
    simulated: Mapped[bool] = mapped_column(Boolean)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("batch_receipt.id"))


class AcquisitionEvent(Base):
    __tablename__ = "acquisition_event"
    __table_args__ = (
        PrimaryKeyConstraint("collector_id", "spool_epoch", "seq"),
        Index("ix_acquisition_event_instrument_time", "instrument_id", "capture_time"),
    )
    collector_id: Mapped[str] = mapped_column(Text)
    spool_epoch: Mapped[int] = mapped_column(BigInteger)
    seq: Mapped[int] = mapped_column(BigInteger)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instrument.id"))
    profile_revision_id: Mapped[str] = mapped_column(ForeignKey("profile_revision.id"))
    event_type: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    capture_time: Mapped[datetime] = mapped_column(TS)
    receive_time: Mapped[datetime] = mapped_column(TS)
    clock_confidence: Mapped[str] = mapped_column(Text)
    simulated: Mapped[bool] = mapped_column(Boolean)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("batch_receipt.id"))


class IngestConflict(Base):
    """Same identity offered with a different payload. Surfaced, never overwritten."""

    __tablename__ = "ingest_conflict"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    collector_id: Mapped[str] = mapped_column(Text)
    spool_epoch: Mapped[int] = mapped_column(BigInteger)
    seq: Mapped[int] = mapped_column(BigInteger)
    batch_id: Mapped[str] = mapped_column(Text)
    existing_digest: Mapped[str] = mapped_column(Text)
    offered_digest: Mapped[str] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(TS)


class AppUser(Base):
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    disabled_at: Mapped[datetime | None] = mapped_column(TS)


class LabGrant(Base):
    __tablename__ = "lab_grant"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "lab_id"),
        CheckConstraint(
            "role IN ('viewer','scientist','custodian','administrator','auditor')", name="role"
        ),
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    lab_id: Mapped[str] = mapped_column(ForeignKey("lab.id"))
    role: Mapped[str] = mapped_column(Text)


class UserSession(Base):
    __tablename__ = "user_session"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(TS)
    expires_at: Mapped[datetime] = mapped_column(TS)
    revoked_at: Mapped[datetime | None] = mapped_column(TS)


class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = (Index("ix_audit_event_at", "at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(TS)
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
