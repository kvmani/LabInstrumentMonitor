"""runs and channel display metadata

Expand-only: adds the ``run`` table and two nullable/defaulted channel columns, so release N-1
keeps working against this schema. Runs are derived from ``acquisition_event`` rows (the
source of truth), so the downgrade loses no scientific record.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06 15:36:01.653549
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("run_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source IN ('instrument','user')", name=op.f("ck_run_source")),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.id"], name=op.f("fk_run_instrument_id_instrument")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run")),
        sa.UniqueConstraint("instrument_id", "run_key", name=op.f("uq_run_instrument_id_run_key")),
    )
    op.create_index(
        "ix_run_instrument_started", "run", ["instrument_id", "started_at"], unique=False
    )
    op.add_column(
        "channel", sa.Column("display_format", sa.Text(), server_default="fixed", nullable=False)
    )
    op.add_column("channel", sa.Column("note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("channel", "note")
    op.drop_column("channel", "display_format")
    op.drop_index("ix_run_instrument_started", table_name="run")
    op.drop_table("run")
