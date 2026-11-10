"""commissioning drafts

Expand-only: a new ``commissioning_draft`` table for the commissioning wizard (ONB-01). Drafts
are resumable and never change a profile until published; publishing writes an immutable
``profile_revision`` as before. No telemetry is touched.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-10 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "commissioning_draft",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("lab_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("preview", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_revision_id", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('draft','published','abandoned')", name=op.f("ck_commissioning_draft_state")
        ),
        sa.CheckConstraint("step BETWEEN 1 AND 6", name=op.f("ck_commissioning_draft_step")),
        sa.ForeignKeyConstraint(
            ["lab_id"], ["lab.id"], name=op.f("fk_commissioning_draft_lab_id_lab")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["app_user.id"], name=op.f("fk_commissioning_draft_created_by_app_user")
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["app_user.id"], name=op.f("fk_commissioning_draft_updated_by_app_user")
        ),
        sa.ForeignKeyConstraint(
            ["published_revision_id"],
            ["profile_revision.id"],
            name=op.f("fk_commissioning_draft_published_revision_id_profile_revision"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commissioning_draft")),
    )
    op.create_index("ix_commissioning_draft_lab", "commissioning_draft", ["lab_id"], unique=False)
    # at most one open draft per instrument
    op.create_index(
        "uq_commissioning_draft_open",
        "commissioning_draft",
        ["instrument_id"],
        unique=True,
        postgresql_where=sa.text("state = 'draft'"),
    )


def downgrade() -> None:
    op.drop_index("uq_commissioning_draft_open", table_name="commissioning_draft")
    op.drop_index("ix_commissioning_draft_lab", table_name="commissioning_draft")
    op.drop_table("commissioning_draft")
