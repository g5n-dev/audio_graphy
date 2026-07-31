"""Persist tag-job provider budgets and atomic publication state.

Revision ID: 0027_tag_job_budget
Revises: 0026_shadow_sampling_complete
Create Date: 2026-07-27 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_tag_job_budget"
down_revision: str | None = "0026_shadow_sampling_complete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in (
        sa.Column("budget_max_provider_tokens", sa.BigInteger(), nullable=True),
        sa.Column("budget_max_provider_calls", sa.Integer(), nullable=True),
        sa.Column("budget_max_cost_microunits", sa.BigInteger(), nullable=True),
        sa.Column("budget_max_wall_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "budget_reserved_provider_tokens",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "budget_reserved_provider_calls",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "budget_reserved_cost_microunits",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "budget_consumed_provider_tokens",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "budget_consumed_provider_calls",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "budget_consumed_cost_microunits",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("budget_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("budget_exhausted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_published_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("tag_extraction_jobs", column)
    op.create_check_constraint(
        "ck_tag_extraction_jobs_budget_limits",
        "tag_extraction_jobs",
        "(budget_max_provider_tokens IS NULL OR budget_max_provider_tokens > 0) "
        "AND (budget_max_provider_calls IS NULL OR budget_max_provider_calls > 0) "
        "AND (budget_max_cost_microunits IS NULL OR budget_max_cost_microunits > 0) "
        "AND (budget_max_wall_seconds IS NULL OR budget_max_wall_seconds > 0)",
    )
    op.create_check_constraint(
        "ck_tag_extraction_jobs_budget_usage",
        "tag_extraction_jobs",
        "budget_reserved_provider_tokens >= 0 "
        "AND budget_reserved_provider_calls >= 0 "
        "AND budget_reserved_cost_microunits >= 0 "
        "AND budget_consumed_provider_tokens >= 0 "
        "AND budget_consumed_provider_calls >= 0 "
        "AND budget_consumed_cost_microunits >= 0",
    )


def downgrade() -> None:
    for constraint in (
        "ck_tag_extraction_jobs_budget_usage",
        "ck_tag_extraction_jobs_budget_limits",
    ):
        op.drop_constraint(constraint, "tag_extraction_jobs", type_="check")
    for column in (
        "current_published_at",
        "budget_exhausted_at",
        "budget_started_at",
        "budget_consumed_cost_microunits",
        "budget_consumed_provider_calls",
        "budget_consumed_provider_tokens",
        "budget_reserved_cost_microunits",
        "budget_reserved_provider_calls",
        "budget_reserved_provider_tokens",
        "budget_max_wall_seconds",
        "budget_max_cost_microunits",
        "budget_max_provider_calls",
        "budget_max_provider_tokens",
    ):
        op.drop_column("tag_extraction_jobs", column)
