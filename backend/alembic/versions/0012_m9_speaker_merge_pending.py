"""M9 T1 — speaker_merge_pending L8 fuzzy reconfirm queue (architecture §21.3).

Revision ID: 0012_m9_speaker_mp
Revises: 0011_m9_leiden_cs
Create Date: 2026-07-22 15:02:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_m9_speaker_mp"
down_revision: str | None = "0011_m9_leiden_cs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create speaker_merge_pending table."""
    op.create_table(
        "speaker_merge_pending",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "recording_id",
            sa.BigInteger(),
            sa.ForeignKey("recordings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("candidate_name", sa.String(length=128), nullable=False),
        sa.Column(
            "matched_speaker_node_id",
            sa.BigInteger(),
            sa.ForeignKey("speaker_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fuzzy_score", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "voiceprint_score", sa.Numeric(precision=5, scale=4), nullable=True
        ),
        sa.Column("resolved_by", sa.String(length=24), nullable=True),
        sa.Column(
            "resolved_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_speaker_merge_pending")),
        sa.CheckConstraint(
            "status IN ('pending', 'resolved_inferred', 'resolved_rejected')",
            name="ck_speaker_merge_pending_status",
        ),
        sa.CheckConstraint(
            "resolved_by IS NULL OR resolved_by IN "
            "('voiceprint', 'human', 'timeout')",
            name="ck_speaker_merge_pending_resolved_by",
        ),
    )
    op.create_index(
        "ix_speaker_merge_pending_tenant_status",
        "speaker_merge_pending",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_speaker_merge_pending_tenant_id"),
        "speaker_merge_pending",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop speaker_merge_pending.

    Per architecture §4 lesson: use ``op.drop_table`` directly so MySQL 8
    cascade-cleans both FK + index. NEVER explicit drop_index before
    drop_constraint (ER_CANT_DROP_FIELD_OR_KEY).
    """
    op.drop_table("speaker_merge_pending")
