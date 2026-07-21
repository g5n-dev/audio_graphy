"""M7 WS-2 — vectors_voiceprint table (encrypted speaker voiceprints).

PIPL §14.3 compliance: voiceprints are stored as AES-256-GCM envelope
ciphertext. The ``voiceprint_id`` column holds a sha256 hash of the
decrypted vector (Q3 locked: same master key as M6 audio encryption).

Table: ``vectors_voiceprint``. Inherits ``tenant_id`` from TenantScopedBase.
FK targets: ``recordings.id`` (CASCADE) + ``speaker_nodes.id`` (CASCADE).

Revision ID: 0007_m7_voiceprint
Revises: 0006_m7_speaker
Create Date: 2026-07-21 14:32:11.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_m7_voiceprint"
down_revision: str | None = "0006_m7_speaker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create vectors_voiceprint table."""
    op.create_table(
        "vectors_voiceprint",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "recording_id",
            sa.BigInteger(),
            sa.ForeignKey("recordings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("segment_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "speaker_entity_id",
            sa.BigInteger(),
            sa.ForeignKey("speaker_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("voiceprint_id", sa.String(length=64), nullable=False),
        sa.Column(
            "vector_encrypted",
            sa.LargeBinary(length=8192),
            nullable=False,
        ),
        sa.Column("encryption_meta", sa.JSON(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vectors_voiceprint")),
    )
    op.create_index(
        "ix_vp_tenant_recording",
        "vectors_voiceprint",
        ["tenant_id", "recording_id"],
        unique=False,
    )
    op.create_index(
        "ix_vp_speaker",
        "vectors_voiceprint",
        ["speaker_entity_id"],
        unique=False,
    )
    op.create_index(
        "ux_vp_voiceprint_id",
        "vectors_voiceprint",
        ["tenant_id", "voiceprint_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_vectors_voiceprint_tenant_id"),
        "vectors_voiceprint",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop vectors_voiceprint table."""
    # MySQL 8: DROP TABLE auto-cascades FK constraints and indexes, so we
    # skip explicit drop_index calls here. ix_vp_speaker backs the FK
    # vectors_voiceprint.speaker_entity_id → speaker_nodes.id and cannot
    # be dropped while the FK exists — MySQL would error with
    # "Cannot drop index 'ix_vp_speaker': needed in a foreign key constraint".
    op.drop_table("vectors_voiceprint")
