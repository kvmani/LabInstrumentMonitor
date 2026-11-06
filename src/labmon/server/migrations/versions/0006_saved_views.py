"""saved views

Expand-only: a new ``saved_view`` table (soft delete). No telemetry is touched.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-06 20:39:23.941227
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_view",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("scope IN ('personal','lab')", name=op.f("ck_saved_view_scope")),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_saved_view_instrument_id_instrument"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["app_user.id"], name=op.f("fk_saved_view_owner_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_view")),
    )
    op.create_index("ix_saved_view_instrument", "saved_view", ["instrument_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_saved_view_instrument", table_name="saved_view")
    op.drop_table("saved_view")
