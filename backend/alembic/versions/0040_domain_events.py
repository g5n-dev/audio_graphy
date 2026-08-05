"""Append-only domain event feed backing GET /api/v1/events/stream.

Rows are inserted in the same transaction as the state transition they
describe; the auto-increment id is the SSE cursor. No delivery state — that
belongs to integration_callbacks, which tracks one receiver per row. This
table is a log many readers walk with their own cursors.

Revision ID: 0040_domain_events
Revises: 0039_integration_open_api
Create Date: 2026-08-05 16:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0040_domain_events"
down_revision: str | None = "0039_integration_open_api"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "domain_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_domain_events"),
    )
    op.create_index("ix_domain_events_tenant_id", "domain_events", ["tenant_id"])
    op.create_index("ix_domain_events_cursor", "domain_events", ["tenant_id", "id"])


def downgrade() -> None:
    op.drop_table("domain_events")
