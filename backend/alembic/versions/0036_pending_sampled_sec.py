"""Stage the sampled duration alongside a pending speaker merge.

``VoiceprintVector.duration_sec`` ranks representative templates
(ADR-0001), and the confirm endpoint copies it from the pending row. That
row only carried the speaker's *total* speech, so a human-confirmed merge
claimed a longer sample than actually backed its vector.

Revision ID: 0036_pending_sampled_sec
Revises: 0035_speaker_link_src_label
Create Date: 2026-08-03 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036_pending_sampled_sec"
down_revision: str | None = "0035_speaker_link_src_label"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "speaker_merge_pending"
_COLUMN = "candidate_sampled_sec"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
