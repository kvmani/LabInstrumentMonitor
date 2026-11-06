"""user runs and annotations

Expand-only: nullable/defaulted run columns (notes, tags, created_by, updated_at) and a new
``annotation`` table. Release N-1 keeps working. The downgrade drops annotations, so take a
backup first if users have written any (they are operator records, not telemetry).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-06 20:00:11.393812
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "annotation",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retracted_by", sa.BigInteger(), nullable=True),
        sa.Column("retract_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["author_id"], ["app_user.id"], name=op.f("fk_annotation_author_id_app_user")
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_annotation_instrument_id_instrument"),
        ),
        sa.ForeignKeyConstraint(
            ["retracted_by"], ["app_user.id"], name=op.f("fk_annotation_retracted_by_app_user")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"], name=op.f("fk_annotation_run_id_run")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_annotation")),
    )
    op.create_index(
        "ix_annotation_instrument_at", "annotation", ["instrument_id", "at"], unique=False
    )
    op.add_column("run", sa.Column("notes", sa.Text(), nullable=True))
    op.add_column(
        "run",
        sa.Column(
            "tags", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
    )
    op.add_column("run", sa.Column("created_by", sa.Text(), nullable=True))
    op.add_column("run", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("run", "updated_at")
    op.drop_column("run", "created_by")
    op.drop_column("run", "tags")
    op.drop_column("run", "notes")
    op.drop_index("ix_annotation_instrument_at", table_name="annotation")
    op.drop_table("annotation")
