"""Record how confidently each voiceprint was attached to its speaker.

SpeakerLinker refuses to use a tentatively-attached vector (an AMBIGUOUS
merge) as a speaker's representative template — otherwise one uncertain
merge redefines that speaker for every later comparison (ADR-0001).

Existing rows default to 1.0. That was described here as "their attachment
confidence is unknown", which is wrong for the rows that matter: the same merge
also wrote the cosine to ``speaker_links``. Migration 0038 backfills from there;
this one stays a pure schema change so the two can be reasoned about separately.

Revision ID: 0034_voiceprint_attach_cos
Revises: 0033_prompt_lab
Create Date: 2026-07-31 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034_voiceprint_attach_cos"
down_revision: str | None = "0033_prompt_lab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "vectors_voiceprint"
_COLUMN = "attach_cosine"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Float(),
            nullable=False,
            server_default="1.0",
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
