"""Represent verified-silence recordings without claiming they are indexed.

Revision ID: 0031_recording_no_speech
Revises: 0030_audio_stream_consistency
Create Date: 2026-07-29 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_recording_no_speech"
down_revision: str | None = "0030_audio_stream_consistency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_STATUS_CHECK = (
    "status IN ('queued', 'processing', 'indexed', 'failed', 'archived')"
)
_NO_SPEECH_STATUS_CHECK = (
    "status IN ('queued', 'processing', 'indexed', 'ready_no_speech', "
    "'failed', 'archived')"
)


def upgrade() -> None:
    op.drop_constraint("ck_recordings_status", "recordings", type_="check")
    op.create_check_constraint(
        "ck_recordings_status",
        "recordings",
        _NO_SPEECH_STATUS_CHECK,
    )
    # Repair rows activated by the pre-0031 service, which represented
    # READY_NO_SPEECH pipeline runs as indexed recordings.
    op.execute(
        sa.text(
            """
            UPDATE recordings AS recording
            INNER JOIN recording_pipeline_runs AS pipeline_run
                ON pipeline_run.id = recording.active_pipeline_run_id
            SET recording.status = 'ready_no_speech',
                recording.indexed_at = NULL
            WHERE pipeline_run.state = 'ready_no_speech'
            """
        )
    )


def downgrade() -> None:
    # Older application versions cannot represent the terminal no-speech
    # result. Re-queue it instead of falsely translating it to ``indexed``.
    op.execute(
        sa.text(
            """
            UPDATE recordings
            SET status = 'queued',
                pipeline_state = 'pending',
                indexed_at = NULL
            WHERE status = 'ready_no_speech'
            """
        )
    )
    op.drop_constraint("ck_recordings_status", "recordings", type_="check")
    op.create_check_constraint(
        "ck_recordings_status",
        "recordings",
        _LEGACY_STATUS_CHECK,
    )
