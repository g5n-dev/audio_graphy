"""Add durable reception automation checkpoints and leases.

Revision ID: 0015_reception_automation
Revises: 0014_reception_dialogue
Create Date: 2026-07-23 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_reception_automation"
down_revision: str | None = "0014_reception_dialogue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the resumable, one-run-per-reception workflow table."""

    op.create_table(
        "reception_automation_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
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
        sa.Column(
            "reception_id",
            sa.BigInteger(),
            sa.ForeignKey("receptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "stage",
            sa.String(length=24),
            server_default="merge",
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("checkpoints", sa.JSON(), nullable=False),
        sa.Column(
            "segmentation_algorithm",
            sa.String(length=64),
            server_default="dialogue-hybrid-v1",
            nullable=False,
        ),
        sa.Column(
            "tag_group_key",
            sa.String(length=64),
            server_default="reception-rules",
            nullable=False,
        ),
        sa.Column(
            "tag_group_version",
            sa.String(length=64),
            server_default="rules-v1",
            nullable=False,
        ),
        sa.Column("target_labels", sa.JSON(), nullable=False),
        sa.Column(
            "tag_priority",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reception_automation_runs")),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'failed', 'ready')",
            name="ck_reception_automation_runs_status",
        ),
        sa.CheckConstraint(
            "stage IN ('merge', 'segmentation', 'tagging', 'ready')",
            name="ck_reception_automation_runs_stage",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_reception_automation_runs_attempt_count",
        ),
        sa.CheckConstraint(
            "tag_priority >= -1000 AND tag_priority <= 1000",
            name="ck_reception_automation_runs_tag_priority",
        ),
    )
    op.create_index(
        op.f("ix_reception_automation_runs_tenant_id"),
        "reception_automation_runs",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ux_reception_automation_runs_reception",
        "reception_automation_runs",
        ["reception_id"],
        unique=True,
    )
    op.create_index(
        "ix_reception_automation_runs_tenant_status",
        "reception_automation_runs",
        ["tenant_id", "status", "stage"],
        unique=False,
    )


def downgrade() -> None:
    """Remove reception automation checkpoints."""

    op.drop_table("reception_automation_runs")
