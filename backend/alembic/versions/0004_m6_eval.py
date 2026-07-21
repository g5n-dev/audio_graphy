"""M6 WS-2 — eval_runs table for async Eval REST API.

Creates the ``eval_runs`` table that backs the ``EvalRunORM`` model and the
``/api/v1/eval/runs`` endpoints. APScheduler polls rows with
``status='pending'`` and transitions them through
``running`` → ``completed`` | ``failed``.

Revision ID: 0004_m6_eval
Revises: 0003_m6_pipl
Create Date: 2026-07-21 14:32:11.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_m6_eval"
down_revision: str | None = "0003_m6_pipl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create eval_runs table."""
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.String(length=36), nullable=False, comment="UUID4 hex"),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("gold_set_path", sa.String(length=512), nullable=False),
        sa.Column("pipeline", sa.String(length=32), nullable=False, comment="mock|rag"),
        sa.Column(
            "judge_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("k_value", sa.Integer(), nullable=False, server_default="5"),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
            comment="pending|running|completed|failed",
        ),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("aggregate_metrics", sa.JSON(), nullable=True),
        sa.Column("report_markdown_path", sa.String(length=512), nullable=True),
        sa.Column("report_json_path", sa.String(length=512), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_runs")),
        sa.CheckConstraint(
            "pipeline IN ('mock', 'rag')", name="ck_eval_runs_pipeline"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_eval_runs_status",
        ),
    )
    op.create_index(
        "ix_eval_runs_tenant_status", "eval_runs", ["tenant_id", "status"], unique=False
    )
    op.create_index(
        "ix_eval_runs_started_at", "eval_runs", ["started_at"], unique=False
    )
    op.create_index(
        op.f("ix_eval_runs_tenant_id"), "eval_runs", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    """Drop eval_runs table."""
    op.drop_index(op.f("ix_eval_runs_tenant_id"), table_name="eval_runs")
    op.drop_index("ix_eval_runs_started_at", table_name="eval_runs")
    op.drop_index("ix_eval_runs_tenant_status", table_name="eval_runs")
    op.drop_table("eval_runs")
