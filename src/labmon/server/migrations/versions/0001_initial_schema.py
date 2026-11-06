"""initial schema

First migration: metadata, scoped access, ingest receipts, identity ledger and raw samples.
Rollback floor: this is the first revision. Its downgrade drops every table and is only for
disposable test databases; never run it against a database holding scientific records.

Revision ID: 0001
Revises:
Create Date: 2026-11-06 13:16:22.538922
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("username", name=op.f("uq_app_user_username")),
    )
    op.create_table(
        "audit_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_event")),
    )
    op.create_index("ix_audit_event_at", "audit_event", ["at"], unique=False)
    op.create_table(
        "ingest_conflict",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("collector_id", sa.Text(), nullable=False),
        sa.Column("spool_epoch", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("existing_digest", sa.Text(), nullable=False),
        sa.Column("offered_digest", sa.Text(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_conflict")),
    )
    op.create_table(
        "lab",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lab")),
    )
    op.create_table(
        "collector",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("lab_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_collector_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["lab_id"], ["lab.id"], name=op.f("fk_collector_lab_id_lab")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collector")),
    )
    op.create_table(
        "instrument",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("lab_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("asset_id", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("lifecycle", sa.Text(), server_default="active", nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("profile_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "lifecycle IN ('draft','active','maintenance','retired')",
            name=op.f("ck_instrument_lifecycle"),
        ),
        sa.ForeignKeyConstraint(["lab_id"], ["lab.id"], name=op.f("fk_instrument_lab_id_lab")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instrument")),
    )
    op.create_index(op.f("ix_instrument_lab_id"), "instrument", ["lab_id"], unique=False)
    op.create_table(
        "lab_grant",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("lab_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "role IN ('viewer','scientist','custodian','administrator','auditor')",
            name=op.f("ck_lab_grant_role"),
        ),
        sa.ForeignKeyConstraint(["lab_id"], ["lab.id"], name=op.f("fk_lab_grant_lab_id_lab")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_lab_grant_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("user_id", "lab_id", name=op.f("pk_lab_grant")),
    )
    op.create_table(
        "user_session",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_user_session_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_user_session_token_hash")),
    )
    op.create_index(op.f("ix_user_session_user_id"), "user_session", ["user_id"], unique=False)
    op.create_table(
        "batch_receipt",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("collector_id", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("payload_digest", sa.Text(), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("accepted", sa.Integer(), nullable=False),
        sa.Column("duplicates", sa.Integer(), nullable=False),
        sa.Column("rejected", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('committed','rejected')", name=op.f("ck_batch_receipt_outcome")
        ),
        sa.ForeignKeyConstraint(
            ["collector_id"], ["collector.id"], name=op.f("fk_batch_receipt_collector_id_collector")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_batch_receipt")),
        sa.UniqueConstraint(
            "collector_id", "batch_id", name=op.f("uq_batch_receipt_collector_id_batch_id")
        ),
    )
    op.create_table(
        "channel",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("data_type", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("expected_cadence_s", sa.Double(), nullable=False),
        sa.Column("display_precision", sa.Integer(), nullable=False),
        sa.Column("enum_labels", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "data_type IN ('float','bool','enum')", name=op.f("ck_channel_data_type")
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.id"], name=op.f("fk_channel_instrument_id_instrument")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel")),
        sa.UniqueConstraint("instrument_id", "key", name=op.f("uq_channel_instrument_id_key")),
    )
    op.create_table(
        "collector_assignment",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("collector_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cutoff_epoch", sa.BigInteger(), nullable=True),
        sa.Column("cutoff_seq", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["collector_id"],
            ["collector.id"],
            name=op.f("fk_collector_assignment_collector_id_collector"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_collector_assignment_instrument_id_instrument"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collector_assignment")),
    )
    op.create_index(
        op.f("ix_collector_assignment_instrument_id"),
        "collector_assignment",
        ["instrument_id"],
        unique=False,
    )
    op.create_table(
        "collector_credential",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("collector_id", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["collector_id"],
            ["collector.id"],
            name=op.f("fk_collector_credential_collector_id_collector"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collector_credential")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_collector_credential_token_hash")),
    )
    op.create_index(
        op.f("ix_collector_credential_collector_id"),
        "collector_credential",
        ["collector_id"],
        unique=False,
    )
    op.create_table(
        "profile_revision",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("profile_key", sa.Text(), nullable=False),
        sa.Column("adapter", sa.Text(), nullable=False),
        sa.Column("adapter_version", sa.Text(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("definition_digest", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_profile_revision_instrument_id_instrument"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_revision")),
        sa.UniqueConstraint(
            "instrument_id", "revision", name=op.f("uq_profile_revision_instrument_id_revision")
        ),
    )
    op.create_table(
        "acquisition_event",
        sa.Column("collector_id", sa.Text(), nullable=False),
        sa.Column("spool_epoch", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("profile_revision_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("capture_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receive_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("clock_confidence", sa.Text(), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_acquisition_event_instrument_id_instrument"),
        ),
        sa.ForeignKeyConstraint(
            ["profile_revision_id"],
            ["profile_revision.id"],
            name=op.f("fk_acquisition_event_profile_revision_id_profile_revision"),
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"],
            ["batch_receipt.id"],
            name=op.f("fk_acquisition_event_receipt_id_batch_receipt"),
        ),
        sa.PrimaryKeyConstraint(
            "collector_id", "spool_epoch", "seq", name=op.f("pk_acquisition_event")
        ),
    )
    op.create_index(
        "ix_acquisition_event_instrument_time",
        "acquisition_event",
        ["instrument_id", "capture_time"],
        unique=False,
    )
    op.create_table(
        "ingest_identity",
        sa.Column("collector_id", sa.Text(), nullable=False),
        sa.Column("spool_epoch", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("record_digest", sa.Text(), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["collector_id"],
            ["collector.id"],
            name=op.f("fk_ingest_identity_collector_id_collector"),
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"],
            ["batch_receipt.id"],
            name=op.f("fk_ingest_identity_receipt_id_batch_receipt"),
        ),
        sa.PrimaryKeyConstraint(
            "collector_id", "spool_epoch", "seq", name=op.f("pk_ingest_identity")
        ),
    )
    op.create_table(
        "sample",
        sa.Column("collector_id", sa.Text(), nullable=False),
        sa.Column("spool_epoch", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("profile_revision_id", sa.Text(), nullable=False),
        sa.Column("frame_id", sa.Text(), nullable=True),
        sa.Column("value_num", sa.Double(), nullable=True),
        sa.Column("value_int", sa.BigInteger(), nullable=True),
        sa.Column("value_bool", sa.Boolean(), nullable=True),
        sa.Column("raw", sa.Text(), nullable=True),
        sa.Column("source_status", sa.Text(), nullable=True),
        sa.Column("quality", sa.Text(), nullable=False),
        sa.Column("quality_reason", sa.Text(), nullable=True),
        sa.Column("source_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_time_meaning", sa.Text(), nullable=False),
        sa.Column("capture_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receive_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_time_source", sa.Text(), nullable=False),
        sa.Column("clock_confidence", sa.Text(), nullable=False),
        sa.Column("clock_flag", sa.Text(), nullable=True),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "clock_confidence IN ('trusted','suspect')", name=op.f("ck_sample_clock_confidence")
        ),
        sa.CheckConstraint("quality IN ('good','uncertain','bad')", name=op.f("ck_sample_quality")),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channel.id"], name=op.f("fk_sample_channel_id_channel")
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.id"], name=op.f("fk_sample_instrument_id_instrument")
        ),
        sa.ForeignKeyConstraint(
            ["profile_revision_id"],
            ["profile_revision.id"],
            name=op.f("fk_sample_profile_revision_id_profile_revision"),
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["batch_receipt.id"], name=op.f("fk_sample_receipt_id_batch_receipt")
        ),
        sa.PrimaryKeyConstraint("collector_id", "spool_epoch", "seq", name=op.f("pk_sample")),
    )
    op.create_index(
        "ix_sample_channel_time",
        "sample",
        ["channel_id", "event_time", "collector_id", "spool_epoch", "seq"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_sample_channel_time", table_name="sample")
    op.drop_table("sample")
    op.drop_table("ingest_identity")
    op.drop_index("ix_acquisition_event_instrument_time", table_name="acquisition_event")
    op.drop_table("acquisition_event")
    op.drop_table("profile_revision")
    op.drop_index(op.f("ix_collector_credential_collector_id"), table_name="collector_credential")
    op.drop_table("collector_credential")
    op.drop_index(op.f("ix_collector_assignment_instrument_id"), table_name="collector_assignment")
    op.drop_table("collector_assignment")
    op.drop_table("channel")
    op.drop_table("batch_receipt")
    op.drop_index(op.f("ix_user_session_user_id"), table_name="user_session")
    op.drop_table("user_session")
    op.drop_table("lab_grant")
    op.drop_index(op.f("ix_instrument_lab_id"), table_name="instrument")
    op.drop_table("instrument")
    op.drop_table("collector")
    op.drop_table("lab")
    op.drop_table("ingest_conflict")
    op.drop_index("ix_audit_event_at", table_name="audit_event")
    op.drop_table("audit_event")
    op.drop_table("app_user")
