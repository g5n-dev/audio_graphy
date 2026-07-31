"""Persist completion of the bounded shadow-generation sample.

Revision ID: 0026_shadow_sampling_complete
Revises: 0025_llm_usage_ledger
Create Date: 2026-07-27 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_shadow_sampling_complete"
down_revision: str | None = "0025_llm_usage_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tag_deployments",
        sa.Column(
            "sampling_complete_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("tag_deployments", "sampling_complete_at")
