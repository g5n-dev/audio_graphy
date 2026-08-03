"""Keep the diarization-local speaker label on each speaker link.

Segments record only the per-file label ("spk_0"), and speaker_links
recorded only canonical node ids, so there was no way to map a transcript
line back to the speaker it belongs to — every per-segment speaker display
was stuck showing the raw label with no identity or quality attached.

Nullable: links written before this column exists cannot be reconstructed,
and guessing would misattribute speech.

Revision ID: 0035_speaker_link_src_label
Revises: 0034_voiceprint_attach_cos
Create Date: 2026-08-03 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_speaker_link_src_label"
down_revision: str | None = "0034_voiceprint_attach_cos"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "speaker_links"
_COLUMN = "source_speaker_label"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
