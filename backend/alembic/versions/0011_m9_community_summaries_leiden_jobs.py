"""M9 T1 — community_summaries + leiden_jobs (architecture §21.2).

Revision ID: 0011_m9_leiden_cs
Revises: 0010_m9_bitemp
Create Date: 2026-07-22 15:01:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_m9_leiden_cs"
down_revision: str | None = "0010_m9_bitemp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create leiden_jobs then community_summaries (FK child must follow parent)."""
    # --- leiden_jobs (parent) ---
    op.create_table(
        "leiden_jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("job_type", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("triggered_by", sa.String(length=32), nullable=False),
        sa.Column("node_count_snapshot", sa.Integer(), nullable=False),
        sa.Column("edge_count_snapshot", sa.Integer(), nullable=False),
        sa.Column("diff_percent", sa.Float(), nullable=True),
        sa.Column("modularity", sa.Float(), nullable=True),
        sa.Column(
            "levels",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("snapshot_path", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "finished_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leiden_jobs")),
        sa.CheckConstraint(
            "job_type IN ('full', 'incremental')",
            name="ck_leiden_jobs_job_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_leiden_jobs_status",
        ),
    )
    op.create_index(
        "ix_leiden_jobs_tenant_status",
        "leiden_jobs",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_leiden_jobs_finished",
        "leiden_jobs",
        ["finished_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_leiden_jobs_tenant_id"),
        "leiden_jobs",
        ["tenant_id"],
        unique=False,
    )

    # --- community_summaries (child) ---
    op.create_table(
        "community_summaries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "leiden_job_id",
            sa.BigInteger(),
            sa.ForeignKey("leiden_jobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("community_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=80), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column(
            "member_node_ids",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("strategy", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_community_summaries")),
        sa.UniqueConstraint(
            "leiden_job_id",
            "level",
            "community_id",
            name="ux_cs_job_level_comm",
        ),
    )
    op.create_index(
        "ix_community_summaries_tenant_level",
        "community_summaries",
        ["tenant_id", "level"],
        unique=False,
    )
    op.create_index(
        op.f("ix_community_summaries_tenant_id"),
        "community_summaries",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop child first then parent (FK order), both via drop_table cascade.

    Per architecture §4 lesson: NEVER explicit drop_index before
    drop_constraint on MySQL 8. ``op.drop_table`` cascade-cleans both.
    Order matters here: community_summaries holds FK to leiden_jobs, so
    we drop child first.
    """
    op.drop_table("community_summaries")
    op.drop_table("leiden_jobs")
