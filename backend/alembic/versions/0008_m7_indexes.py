"""M7 WS-2 — vectors_audio table + performance indexes.

Creates ``vectors_audio`` for CLAP 512-d segment embeddings (M7 P0-13).
CLAP vectors are NOT biometric (PIPL §14.3 does not apply), so the column
stores plaintext bytes — retrieval avoids per-decrypt overhead. Q-后续-4
(open question) leaves future encryption to M8.

Also adds a composite index on ``speaker_nodes`` for cosine-threshold
queries (Layer-1 voiceprint matching against same-tenant speakers).

Revision ID: 0008_m7_indexes
Revises: 0007_m7_voiceprint
Create Date: 2026-07-21 14:32:11.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_m7_indexes"
down_revision: str | None = "0007_m7_voiceprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create vectors_audio + composite speaker index."""
    op.create_table(
        "vectors_audio",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "recording_id",
            sa.BigInteger(),
            sa.ForeignKey("recordings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("segment_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_id", sa.BigInteger(), nullable=True),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False, server_default="512"),
        sa.Column(
            "model",
            sa.String(length=64),
            nullable=False,
            server_default="clap-htsat-base-2022",
        ),
        sa.Column("duration_sec", sa.Float(), server_default="0.0"),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vectors_audio")),
    )
    op.create_index(
        "ix_va_tenant_recording",
        "vectors_audio",
        ["tenant_id", "recording_id"],
        unique=False,
    )
    op.create_index(
        "ix_va_segment",
        "vectors_audio",
        ["segment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vectors_audio_tenant_id"),
        "vectors_audio",
        ["tenant_id"],
        unique=False,
    )

    # Composite: tenant + role + first_seen — supports SpeakerLinker's
    # "find existing agents/customers newer than X" queries.
    op.create_index(
        "ix_speaker_nodes_tenant_role_first_seen",
        "speaker_nodes",
        ["tenant_id", "speaker_role", "first_seen"],
        unique=False,
    )


def downgrade() -> None:
    """Drop vectors_audio + composite speaker index."""
    op.drop_index(
        "ix_speaker_nodes_tenant_role_first_seen",
        table_name="speaker_nodes",
    )
    op.drop_index(op.f("ix_vectors_audio_tenant_id"), table_name="vectors_audio")
    op.drop_index("ix_va_segment", table_name="vectors_audio")
    op.drop_index("ix_va_tenant_recording", table_name="vectors_audio")
    op.drop_table("vectors_audio")
