"""M6 PIPL §14.3 — audio encryption + segment scrubbed text.

Adds:
    1. recordings.audio_encrypted_path VARCHAR(512) NULL — encrypted file path.
    2. recordings.audio_encryption_meta JSON NULL — envelope metadata.
    3. segments.text_scrubbed TEXT NULL — PII-scrubbed transcript (forward-fill).

Backward compatibility: NULL is treated as "legacy / unencrypted" by the
DSAR + query layers; runtime PIIScrubber.scrub is applied lazily when a
row is read.

Revision ID: 0003_m6_pipl
Revises: 0002_add_password_and_recompute
Create Date: 2026-07-21 14:32:11.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_m6_pipl"
down_revision: str | None = "0002_add_password_and_recompute"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add PIPL §14.3 columns."""
    op.add_column(
        "recordings",
        sa.Column("audio_encrypted_path", sa.String(512), nullable=True),
    )
    op.add_column(
        "recordings",
        sa.Column("audio_encryption_meta", sa.JSON(), nullable=True),
    )
    op.add_column(
        "segments",
        sa.Column("text_scrubbed", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Drop PIPL §14.3 columns."""
    op.drop_column("segments", "text_scrubbed")
    op.drop_column("recordings", "audio_encryption_meta")
    op.drop_column("recordings", "audio_encrypted_path")
