"""M9 T1 — edge_events append-only bi-temporal log (architecture §21.1).

Revision ID: 0010_m9_bitemp
Revises: 0009_m8_streaming_init
Create Date: 2026-07-22 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_m9_bitemp"
down_revision: str | None = "0009_m8_streaming_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create edge_events table for bi-temporal audit log."""
    op.create_table(
        "edge_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("edge_key", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("relation", sa.String(length=128), nullable=False),
        sa.Column(
            "valid_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "invalid_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("superseded_by", sa.String(length=255), nullable=True),
        sa.Column(
            "actor",
            sa.String(length=64),
            nullable=False,
            server_default="system",
        ),
        sa.Column("payload", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edge_events")),
        sa.CheckConstraint(
            "event_type IN "
            "('insert', 'merge', 'supersede', 'soft_delete', 'restore')",
            name="ck_edge_events_event_type",
        ),
    )
    op.create_index(
        "ix_edge_events_tenant_valid",
        "edge_events",
        ["tenant_id", "valid_at"],
        unique=False,
    )
    op.create_index(
        "ix_edge_events_edge_key",
        "edge_events",
        ["edge_key"],
        unique=False,
    )
    op.create_index(
        "ix_edge_events_event_type",
        "edge_events",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_edge_events_tenant_id"),
        "edge_events",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop edge_events.

    Per architecture §4 lesson: use ``op.drop_table`` directly so MySQL 8
    cascade-cleans FK + index. NEVER explicit drop_index before
    drop_constraint (causes ER_CANT_DROP_FIELD_OR_KEY).
    """
    op.drop_table("edge_events")
