"""M7 WS-2 — speaker_nodes + speaker_links tables.

Creates two tables backing the cross-recording speaker linking pipeline
(M7 architecture §7 + §8):

    speaker_nodes : per-tenant speaker entity with voiceprint_id, role,
                    recordings_list, merge_confidence, ambiguity_tag.
    speaker_links : append-only audit trail of every merge decision
                    (canonical/source/recording/strategy/confidence).

Both inherit ``tenant_id`` from TenantScopedBase. speaker_links references
speaker_nodes via ON DELETE CASCADE so deleting a speaker node cleans up
its audit trail automatically.

Revision ID: 0006_m7_speaker
Revises: 0005_m6_rapidfuzz
Create Date: 2026-07-21 14:32:11.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_m7_speaker"
down_revision: str | None = "0005_m6_rapidfuzz"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VALID_ROLES = ("agent", "customer", "unknown")
_VALID_STRATEGIES = ("voiceprint", "fuzzy", "manual", "single_recording")


def upgrade() -> None:
    """Create speaker_nodes + speaker_links tables."""
    op.create_table(
        "speaker_nodes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("voiceprint_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column(
            "speaker_role",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("recordings_list", sa.JSON(), nullable=False),
        sa.Column(
            "recordings_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "total_speech_sec", sa.Float(), nullable=False, server_default="0.0"
        ),
        sa.Column(
            "merge_confidence", sa.Float(), nullable=False, server_default="0.0"
        ),
        sa.Column(
            "merge_strategy",
            sa.String(length=32),
            nullable=False,
            server_default="single_recording",
        ),
        sa.Column("ambiguity_tag", sa.String(length=32), nullable=True),
        sa.Column("attrs", sa.JSON(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_speaker_nodes")),
        sa.CheckConstraint(
            f"speaker_role IN {_VALID_ROLES}",
            name="ck_speaker_nodes_role",
        ),
        sa.CheckConstraint(
            f"merge_strategy IN {_VALID_STRATEGIES}",
            name="ck_speaker_nodes_strategy",
        ),
        sa.CheckConstraint(
            "ambiguity_tag IS NULL OR ambiguity_tag IN ('AMBIGUOUS', 'PENDING_REVIEW')",
            name="ck_speaker_nodes_ambiguity",
        ),
    )
    op.create_index(
        "ux_speaker_nodes_vp",
        "speaker_nodes",
        ["tenant_id", "voiceprint_id"],
        unique=True,
    )
    op.create_index(
        "ix_speaker_nodes_role",
        "speaker_nodes",
        ["tenant_id", "speaker_role"],
        unique=False,
    )
    op.create_index(
        op.f("ix_speaker_nodes_tenant_id"),
        "speaker_nodes",
        ["tenant_id"],
        unique=False,
    )

    op.create_table(
        "speaker_links",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "canonical_speaker_id",
            sa.BigInteger(),
            sa.ForeignKey("speaker_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_speaker_id",
            sa.BigInteger(),
            sa.ForeignKey("speaker_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "recording_id",
            sa.BigInteger(),
            sa.ForeignKey("recordings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cosine_similarity", sa.Float(), nullable=True),
        sa.Column("merge_confidence", sa.Float(), nullable=False),
        sa.Column("strategy", sa.String(length=32), nullable=False),
        sa.Column("ambiguity_tag", sa.String(length=32), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_speaker_links")),
        sa.CheckConstraint(
            f"strategy IN {_VALID_STRATEGIES}",
            name="ck_speaker_links_strategy",
        ),
        sa.CheckConstraint(
            "ambiguity_tag IS NULL OR ambiguity_tag IN ('AMBIGUOUS', 'PENDING_REVIEW')",
            name="ck_speaker_links_ambiguity",
        ),
    )
    op.create_index(
        "ix_speaker_links_canonical",
        "speaker_links",
        ["canonical_speaker_id"],
        unique=False,
    )
    op.create_index(
        "ix_speaker_links_source",
        "speaker_links",
        ["source_speaker_id"],
        unique=False,
    )
    op.create_index(
        "ix_speaker_links_recording",
        "speaker_links",
        ["recording_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_speaker_links_tenant_id"),
        "speaker_links",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop speaker_links + speaker_nodes tables."""
    # MySQL 8: DROP TABLE auto-cascades FK constraints and indexes, so we
    # skip explicit drop_index calls here. Several speaker_links indexes
    # (ix_speaker_links_canonical/source/recording) back FK columns and
    # cannot be dropped while the FKs exist — MySQL would error with
    # "Cannot drop index 'X': needed in a foreign key constraint".
    op.drop_table("speaker_links")
    op.drop_table("speaker_nodes")
