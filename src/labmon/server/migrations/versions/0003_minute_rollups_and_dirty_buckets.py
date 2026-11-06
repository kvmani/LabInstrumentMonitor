"""minute rollups and dirty buckets

Expand-only: two new tables. ``sample_rollup_1m`` is a rebuildable cache of ``sample`` and
``rollup_dirty`` a work queue, so the downgrade loses no scientific record.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06 19:18:56.178792
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rollup_dirty",
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("marked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("channel_id", "bucket", name=op.f("pk_rollup_dirty")),
    )
    op.create_table(
        "sample_rollup_1m",
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("n_valid", sa.Integer(), nullable=False),
        sa.Column("n_uncertain", sa.Integer(), nullable=False),
        sa.Column("v_sum", sa.Double(), nullable=True),
        sa.Column("v_min", sa.Double(), nullable=True),
        sa.Column("t_min", sa.DateTime(timezone=True), nullable=True),
        sa.Column("v_max", sa.Double(), nullable=True),
        sa.Column("t_max", sa.DateTime(timezone=True), nullable=True),
        sa.Column("v_first", sa.Double(), nullable=True),
        sa.Column("v_last", sa.Double(), nullable=True),
        sa.Column("t_first", sa.DateTime(timezone=True), nullable=False),
        sa.Column("t_last", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gap_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gap_s", sa.Double(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channel.id"], name=op.f("fk_sample_rollup_1m_channel_id_channel")
        ),
        sa.PrimaryKeyConstraint("channel_id", "bucket", name=op.f("pk_sample_rollup_1m")),
    )
    # existing samples have no rollups yet: queue every touched minute for the worker
    op.execute(
        "INSERT INTO rollup_dirty (channel_id, bucket, revision, marked_at) "
        "SELECT channel_id, date_bin(interval '1 minute', event_time, "
        "timestamptz '2000-01-01 00:00:00+00'), 1, now() FROM sample GROUP BY 1, 2"
    )


def downgrade() -> None:
    op.drop_table("sample_rollup_1m")
    op.drop_table("rollup_dirty")
