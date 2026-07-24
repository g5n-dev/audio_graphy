"""Add the covering prefix used by batched latest-voiceprint lookup.

Revision ID: 0013_vp_batch_lookup
Revises: 0012_m9_speaker_mp
Create Date: 2026-07-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_vp_batch_lookup"
down_revision: str | None = "0012_m9_speaker_mp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "vectors_voiceprint"
_INDEX = "ix_vp_tenant_speaker_created"


def upgrade() -> None:
    """Add the online index used by tenant/speaker/latest window queries."""
    with op.get_context().autocommit_block():
        op.execute(
            f"ALTER TABLE {_TABLE} "
            f"ADD INDEX {_INDEX} (tenant_id, speaker_entity_id, created_at), "
            "ALGORITHM=INPLACE, LOCK=NONE"
        )


def downgrade() -> None:
    """Remove the lookup index without blocking concurrent table writes."""
    with op.get_context().autocommit_block():
        op.execute(
            f"ALTER TABLE {_TABLE} DROP INDEX {_INDEX}, "
            "ALGORITHM=INPLACE, LOCK=NONE"
        )
