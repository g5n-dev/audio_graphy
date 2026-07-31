"""Add alert-only baseline provenance for automatic tag-job budgets.

Revision ID: 0028_job_budget_baseline
Revises: 0027_tag_job_budget
Create Date: 2026-07-27 19:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_job_budget_baseline"
down_revision: str | None = "0027_tag_job_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tag_extraction_jobs",
        sa.Column(
            "budget_source",
            sa.String(length=16),
            server_default="alert_only",
            nullable=False,
        ),
    )
    op.add_column(
        "tag_extraction_jobs",
        sa.Column(
            "budget_purpose",
            sa.String(length=64),
            server_default="extract",
            nullable=False,
        ),
    )
    for column in (
        sa.Column(
            "budget_baseline_sample_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "budget_accounted_items",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "budget_usage_complete",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    ):
        op.add_column("tag_extraction_jobs", column)
    op.create_check_constraint(
        "ck_tag_extraction_jobs_budget_source",
        "tag_extraction_jobs",
        "budget_source IN ('alert_only', 'explicit', 'default_p99')",
    )
    op.create_check_constraint(
        "ck_tag_extraction_jobs_budget_baseline",
        "tag_extraction_jobs",
        "budget_baseline_sample_count >= 0 AND budget_accounted_items >= 0",
    )
    op.create_index(
        "ix_tag_jobs_budget_baseline",
        "tag_extraction_jobs",
        [
            "tenant_id",
            "job_type",
            "budget_purpose",
            "budget_usage_complete",
            "finished_at",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tag_jobs_budget_baseline",
        table_name="tag_extraction_jobs",
    )
    for constraint in (
        "ck_tag_extraction_jobs_budget_baseline",
        "ck_tag_extraction_jobs_budget_source",
    ):
        op.drop_constraint(constraint, "tag_extraction_jobs", type_="check")
    for column in (
        "budget_usage_complete",
        "budget_accounted_items",
        "budget_baseline_sample_count",
        "budget_purpose",
        "budget_source",
    ):
        op.drop_column("tag_extraction_jobs", column)
