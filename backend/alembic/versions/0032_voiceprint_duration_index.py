"""Index the per-speaker voiceprint ranking by duration (ADR-0001).

SpeakerLinker selects each speaker's representative template by longest
sample rather than newest, so ``ix_vp_tenant_speaker_created`` no longer
covers the ranking's leading sort key.

Scope note: this index narrows the rows MySQL reads for a tenant, but it
does NOT remove the sort. The ranking runs a window function inside a
derived table, which MySQL 8 always materializes, and the ORDER BY mixes
ascending partition keys with descending sort keys plus columns that are
not in the index — so a filesort over the tenant's rows remains. See
docs/adr/0001-voiceprint-sampling.md.

Revision ID: 0032_voiceprint_duration_idx
Revises: 0031_recording_no_speech
Create Date: 2026-07-31 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0032_voiceprint_duration_idx"
down_revision: str | None = "0031_recording_no_speech"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_vp_tenant_speaker_duration"
_TABLE = "vectors_voiceprint"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        _TABLE,
        ["tenant_id", "speaker_entity_id", "duration_sec"],
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name=_TABLE)
