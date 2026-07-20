"""Add users.password_hash + recompute_tasks table.

Adds:
    1. users.password_hash VARCHAR(128) NULL — bcrypt hash column.
    2. recompute_tasks table — tracks prompt recompute job progress.

Revision ID: 0002_add_password_and_recompute
Revises: 0001_init
Create Date: 2026-07-22 10:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_add_password_and_recompute"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add password_hash column + recompute_tasks table."""

    # ── 1. users.password_hash ───────────────────────────────────
    op.add_column(
        "users",
        sa.Column("password_hash", sa.String(128), nullable=True),
    )

    # ── 2. recompute_tasks ───────────────────────────────────────
    op.create_table(
        "recompute_tasks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("processed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("changed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cached_hits", sa.Integer, nullable=False, server_default="0"),
        sa.Column("llm_calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("task_id", name="ux_recompute_tasks_task_id"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')",
            name="ck_recompute_tasks_status",
        ),
    )
    op.create_index("ix_recompute_tasks_tenant_id", "recompute_tasks", ["tenant_id"])
    op.create_index(
        "ix_recompute_tasks_tenant_status", "recompute_tasks", ["tenant_id", "status"]
    )
    op.create_index(
        "ix_recompute_tasks_prompt_version", "recompute_tasks", ["prompt_version"]
    )


def downgrade() -> None:
    """Drop recompute_tasks table + users.password_hash column."""
    op.drop_index("ix_recompute_tasks_prompt_version", table_name="recompute_tasks")
    op.drop_index("ix_recompute_tasks_tenant_status", table_name="recompute_tasks")
    op.drop_index("ix_recompute_tasks_tenant_id", table_name="recompute_tasks")
    op.drop_table("recompute_tasks")
    op.drop_column("users", "password_hash")
