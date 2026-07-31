"""Add immutable reception audio and durable streaming consistency state.

Revision ID: 0030_audio_stream_consistency
Revises: 0029_audio_consistency_runs
Create Date: 2026-07-29 14:00:00.000000

This is the expand/backfill migration for the runtime models introduced by the
audio consistency hardening.  Media probing is intentionally not performed
here: verified source facts are populated by the resumable management worker.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

# Alembic's default version table stores at most 32 characters.
revision: str = "0030_audio_stream_consistency"
down_revision: str | None = "0029_audio_consistency_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
    ]


def _assert_reception_seconds_are_backfillable() -> None:
    """Reject corrupt legacy geometry before any non-transactional MySQL DDL."""

    bind = op.get_bind()
    invalid = list(
        bind.execute(
            sa.text(
                """
                SELECT id
                FROM reception_recordings
                WHERE timeline_start_sec IS NULL
                   OR timeline_end_sec IS NULL
                   OR source_start_sec IS NULL
                   OR gap_before_sec IS NULL
                   OR timeline_start_sec < 0
                   OR timeline_end_sec <= timeline_start_sec
                   OR source_start_sec < 0
                   OR (
                       source_end_sec IS NOT NULL
                       AND source_end_sec <= source_start_sec
                   )
                   OR gap_before_sec < 0
                ORDER BY id
                LIMIT 20
                """
            )
        ).scalars()
    )
    if invalid:
        rendered = ", ".join(str(value) for value in invalid)
        raise RuntimeError(
            "0030 cannot backfill invalid reception timeline rows; "
            f"mapping ids: {rendered}"
        )


def _backfill_integer_timeline_coordinates() -> None:
    """Deterministically populate the new millisecond coordinate projection."""

    bind = op.get_bind()
    while True:
        result = bind.execute(
            sa.text(
                """
                UPDATE reception_recordings
                SET source_start_ms = CAST(ROUND(source_start_sec * 1000) AS SIGNED),
                    source_end_ms = CASE
                        WHEN source_end_sec IS NULL THEN NULL
                        ELSE CAST(ROUND(source_end_sec * 1000) AS SIGNED)
                    END,
                    timeline_start_ms =
                        CAST(ROUND(timeline_start_sec * 1000) AS SIGNED),
                    timeline_end_ms =
                        CAST(ROUND(timeline_end_sec * 1000) AS SIGNED),
                    gap_before_ms =
                        CAST(ROUND(gap_before_sec * 1000) AS SIGNED)
                WHERE source_start_ms IS NULL
                   OR (
                       source_end_sec IS NOT NULL
                       AND source_end_ms IS NULL
                   )
                   OR timeline_start_ms IS NULL
                   OR timeline_end_ms IS NULL
                   OR gap_before_ms IS NULL
                ORDER BY id
                LIMIT 5000
                """
            )
        )
        if not result.rowcount:
            break


def _assert_integer_timeline_backfill_complete() -> None:
    bind = op.get_bind()
    incomplete = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM reception_recordings
            WHERE source_start_ms IS NULL
               OR (
                   source_end_sec IS NOT NULL
                   AND source_end_ms IS NULL
               )
               OR timeline_start_ms IS NULL
               OR timeline_end_ms IS NULL
               OR gap_before_ms IS NULL
            """
        )
    ).scalar_one()
    if int(incomplete):
        raise RuntimeError(
            "0030 millisecond coordinate backfill is incomplete; "
            f"remaining rows: {incomplete}"
        )


def _assert_legacy_streaming_session_uniqueness() -> None:
    """Ensure downgrade can restore the old global session-id uniqueness."""

    bind = op.get_bind()
    duplicates = list(
        bind.execute(
            sa.text(
                """
                SELECT session_id, COUNT(*) AS row_count
                FROM streaming_sessions
                GROUP BY session_id
                HAVING COUNT(*) > 1
                ORDER BY session_id
                LIMIT 20
                """
            )
        ).mappings()
    )
    if duplicates:
        rendered = ", ".join(
            f"{row['session_id']} ({row['row_count']} rows)" for row in duplicates
        )
        raise RuntimeError(
            "0030 downgrade blocked: duplicate session_id values cannot satisfy "
            f"legacy UNIQUE(session_id): {rendered}"
        )


def _create_reception_audio_schema() -> None:
    op.create_table(
        "reception_timeline_revisions",
        *_base_columns(),
        sa.Column("reception_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("expected_reception_version", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default="STAGING",
            nullable=False,
        ),
        sa.Column("plan_signature", sa.String(length=64), nullable=False),
        sa.Column("plan_token_hash", sa.String(length=64), nullable=False),
        sa.Column("source_manifest", sa.JSON(), nullable=False),
        sa.Column("total_duration_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "physical_eligible",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('STAGING', 'ACTIVE', 'SUPERSEDED', 'CANCELLED', 'FAILED')",
            name="ck_reception_timeline_revisions_state",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND expected_reception_version >= 1 "
            "AND total_duration_ms > 0",
            name="ck_reception_timeline_revisions_values",
        ),
        sa.ForeignKeyConstraint(
            ["reception_id"],
            ["receptions.id"],
            name="fk_reception_timeline_revisions_reception_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_reception_timeline_revisions",
        ),
    )
    op.create_index(
        "ix_reception_timeline_revisions_tenant_id",
        "reception_timeline_revisions",
        ["tenant_id"],
    )
    op.create_index(
        "ux_reception_timeline_revisions_revision",
        "reception_timeline_revisions",
        ["reception_id", "revision"],
        unique=True,
    )
    op.create_index(
        "ux_reception_timeline_revisions_token",
        "reception_timeline_revisions",
        ["plan_token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_reception_timeline_revisions_active",
        "reception_timeline_revisions",
        ["tenant_id", "reception_id", "state"],
    )

    op.add_column(
        "receptions",
        sa.Column("active_timeline_revision_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_receptions_active_timeline_revision_id",
        "receptions",
        "reception_timeline_revisions",
        ["active_timeline_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "reception_audio_operations",
        *_base_columns(),
        sa.Column("reception_id", sa.BigInteger(), nullable=False),
        sa.Column("timeline_revision_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("expected_reception_version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="queued",
            nullable=False,
        ),
        sa.Column(
            "progress",
            sa.Float(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "mode IN ('logical', 'physical', 'both')",
            name="ck_reception_audio_operations_mode",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'claimed', 'probing', 'slicing', "
            "'assembling', 'encrypting', 'verifying', 'committing', "
            "'succeeded', 'failed', 'cancelled')",
            name="ck_reception_audio_operations_status",
        ),
        sa.CheckConstraint(
            "progress >= 0 AND progress <= 1 AND attempt_count >= 0",
            name="ck_reception_audio_operations_progress",
        ),
        sa.ForeignKeyConstraint(
            ["reception_id"],
            ["receptions.id"],
            name="fk_reception_audio_operations_reception_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["timeline_revision_id"],
            ["reception_timeline_revisions.id"],
            name="fk_reception_audio_operations_timeline_revision_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reception_audio_operations"),
    )
    op.create_index(
        "ix_reception_audio_operations_tenant_id",
        "reception_audio_operations",
        ["tenant_id"],
    )
    op.create_index(
        "ux_reception_audio_operations_idempotency",
        "reception_audio_operations",
        ["tenant_id", "reception_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_reception_audio_operations_claim",
        "reception_audio_operations",
        ["tenant_id", "status", "lease_expires_at"],
    )

    op.create_table(
        "reception_audio_artifacts",
        *_base_columns(),
        sa.Column("reception_id", sa.BigInteger(), nullable=False),
        sa.Column("timeline_revision_id", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default="PREPARING",
            nullable=False,
        ),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("sample_rate", sa.Integer(), nullable=True),
        sa.Column("channels", sa.Integer(), nullable=True),
        sa.Column("attached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('PREPARING', 'READY', 'ATTACHED', 'RETIRED', "
            "'DELETED', 'FAILED', 'ORPHANED')",
            name="ck_reception_audio_artifacts_state",
        ),
        sa.CheckConstraint(
            "(size_bytes IS NULL OR size_bytes > 0) AND "
            "(duration_ms IS NULL OR duration_ms > 0) AND "
            "(sample_rate IS NULL OR sample_rate > 0) AND "
            "(channels IS NULL OR channels > 0)",
            name="ck_reception_audio_artifacts_media",
        ),
        sa.ForeignKeyConstraint(
            ["reception_id"],
            ["receptions.id"],
            name="fk_reception_audio_artifacts_reception_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["timeline_revision_id"],
            ["reception_timeline_revisions.id"],
            name="fk_reception_audio_artifacts_timeline_revision_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["reception_audio_operations.id"],
            name="fk_reception_audio_artifacts_operation_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reception_audio_artifacts"),
    )
    op.create_index(
        "ix_reception_audio_artifacts_tenant_id",
        "reception_audio_artifacts",
        ["tenant_id"],
    )
    op.create_index(
        "ix_reception_audio_artifacts_active",
        "reception_audio_artifacts",
        ["tenant_id", "reception_id", "state"],
    )
    op.create_index(
        "ux_reception_audio_artifacts_path",
        "reception_audio_artifacts",
        ["path"],
        unique=True,
    )


def _expand_reception_generation_columns() -> None:
    for column in (
        sa.Column("timeline_revision_id", sa.BigInteger(), nullable=True),
        sa.Column("source_start_ms", sa.BigInteger(), nullable=True),
        sa.Column("source_end_ms", sa.BigInteger(), nullable=True),
        sa.Column("timeline_start_ms", sa.BigInteger(), nullable=True),
        sa.Column("timeline_end_ms", sa.BigInteger(), nullable=True),
        sa.Column("gap_before_ms", sa.BigInteger(), nullable=True),
    ):
        op.add_column("reception_recordings", column)

    _backfill_integer_timeline_coordinates()
    _assert_integer_timeline_backfill_complete()
    for column_name in (
        "source_start_ms",
        "timeline_start_ms",
        "timeline_end_ms",
        "gap_before_ms",
    ):
        op.alter_column(
            "reception_recordings",
            column_name,
            existing_type=sa.BigInteger(),
            nullable=False,
        )
    op.create_foreign_key(
        "fk_reception_recordings_timeline_revision_id",
        "reception_recordings",
        "reception_timeline_revisions",
        ["timeline_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_reception_recordings_integer_timeline",
        "reception_recordings",
        "source_start_ms >= 0 AND timeline_start_ms >= 0 AND "
        "timeline_end_ms >= timeline_start_ms AND gap_before_ms >= 0 AND "
        "(source_end_ms IS NULL OR source_end_ms > source_start_ms)",
    )

    op.add_column(
        "dialogue_units",
        sa.Column("stage_confidence", sa.Float(), nullable=True),
    )
    op.add_column(
        "dialogue_units",
        sa.Column("timeline_revision_id", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_dialogue_units_stage_confidence",
        "dialogue_units",
        "stage_confidence IS NULL OR "
        "(stage_confidence >= 0 AND stage_confidence <= 1)",
    )
    op.create_foreign_key(
        "fk_dialogue_units_timeline_revision_id",
        "dialogue_units",
        "reception_timeline_revisions",
        ["timeline_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )

    for table_name in (
        "dialogue_state_transitions",
        "dialogue_tag_assignments",
    ):
        op.add_column(
            table_name,
            sa.Column("timeline_revision_id", sa.BigInteger(), nullable=True),
        )
        op.create_foreign_key(
            f"fk_{table_name}_timeline_revision_id",
            table_name,
            "reception_timeline_revisions",
            ["timeline_revision_id"],
            ["id"],
            ondelete="SET NULL",
        )


def _expand_streaming_sessions() -> None:
    for column in (
        sa.Column(
            "epoch",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="RESERVING",
            nullable=False,
        ),
        sa.Column(
            "generation",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("pipeline_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "ack_seq_high_watermark",
            sa.Integer(),
            server_default=sa.text("-1"),
            nullable=False,
        ),
        sa.Column(
            "durable_segment_high_watermark",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
    ):
        op.add_column("streaming_sessions", column)

    op.execute(
        sa.text(
            """
            UPDATE streaming_sessions
            SET status = CASE
                WHEN ended_at IS NOT NULL AND end_reason = 'normal' THEN 'CLOSED'
                WHEN ended_at IS NULL THEN 'INCOMPLETE'
                ELSE 'INCOMPLETE'
            END
            """
        )
    )
    op.drop_constraint(
        "ux_streaming_sessions_session_id",
        "streaming_sessions",
        type_="unique",
    )
    op.create_index(
        "ux_streaming_sessions_tenant_session_epoch",
        "streaming_sessions",
        ["tenant_id", "session_id", "epoch"],
        unique=True,
    )
    op.create_foreign_key(
        "fk_streaming_sessions_pipeline_run_id",
        "streaming_sessions",
        "recording_pipeline_runs",
        ["pipeline_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_streaming_sessions_pipeline_run",
        "streaming_sessions",
        ["pipeline_run_id"],
    )
    op.create_check_constraint(
        "ck_streaming_sessions_status",
        "streaming_sessions",
        "status IN ('RESERVING', 'ACTIVE', 'DRAINING', 'COMMITTING', "
        "'CLOSED', 'INCOMPLETE', 'FAILED')",
    )
    op.create_check_constraint(
        "ck_streaming_sessions_watermarks",
        "streaming_sessions",
        "epoch >= 1 AND generation >= 0 AND ack_seq_high_watermark >= -1",
    )


def _create_streaming_durability_schema() -> None:
    op.create_table(
        "streaming_ws_tickets",
        *_base_columns(),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("recording_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("consent_token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default="ISSUED",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('ISSUED', 'CONSUMED', 'EXPIRED', 'REVOKED')",
            name="ck_streaming_ws_tickets_state",
        ),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["recordings.id"],
            name="fk_streaming_ws_tickets_recording_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_streaming_ws_tickets"),
    )
    op.create_index(
        "ix_streaming_ws_tickets_tenant_id",
        "streaming_ws_tickets",
        ["tenant_id"],
    )
    op.create_index(
        "ux_streaming_ws_tickets_hash",
        "streaming_ws_tickets",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_streaming_ws_tickets_tenant_expiry",
        "streaming_ws_tickets",
        ["tenant_id", "state", "expires_at"],
    )

    op.create_table(
        "streaming_pcm_frames",
        *_base_columns(),
        sa.Column("session_key", sa.String(length=128), nullable=False),
        sa.Column("recording_id", sa.BigInteger(), nullable=False),
        sa.Column("source_seq", sa.BigInteger(), nullable=False),
        sa.Column("pcm", sa.LargeBinary(length=65_536), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default="ACCEPTED",
            nullable=False,
        ),
        sa.Column("consumed_segment_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "source_seq >= 0",
            name="ck_stream_pcm_frames_source_seq",
        ),
        sa.CheckConstraint(
            "state IN ('ACCEPTED', 'CONSUMED', 'ORPHANED')",
            name="ck_stream_pcm_frames_state",
        ),
        sa.CheckConstraint(
            "length(pcm) > 0 AND length(pcm) <= 65536",
            name="ck_stream_pcm_frames_size",
        ),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["recordings.id"],
            name="fk_streaming_pcm_frames_recording_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["consumed_segment_id"],
            ["segments.id"],
            name="fk_streaming_pcm_frames_consumed_segment_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_streaming_pcm_frames"),
    )
    op.create_index(
        "ix_streaming_pcm_frames_tenant_id",
        "streaming_pcm_frames",
        ["tenant_id"],
    )
    op.create_index(
        "ux_stream_pcm_frames_source",
        "streaming_pcm_frames",
        ["tenant_id", "session_key", "source_seq"],
        unique=True,
    )
    op.create_index(
        "ix_stream_pcm_frames_replay",
        "streaming_pcm_frames",
        ["tenant_id", "session_key", "recording_id", "state", "source_seq"],
    )

    op.create_table(
        "streaming_segment_receipts",
        *_base_columns(),
        sa.Column("streaming_session_id", sa.BigInteger(), nullable=False),
        sa.Column("session_key", sa.String(length=128), nullable=False),
        sa.Column("source_event_key", sa.String(length=128), nullable=False),
        sa.Column("source_seq", sa.BigInteger(), nullable=False),
        sa.Column("recording_id", sa.BigInteger(), nullable=False),
        sa.Column("pipeline_run_id", sa.BigInteger(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("segment_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "source_seq >= 0",
            name="ck_stream_receipts_source_seq",
        ),
        sa.CheckConstraint(
            "generation >= 1",
            name="ck_stream_receipts_generation",
        ),
        sa.ForeignKeyConstraint(
            ["streaming_session_id"],
            ["streaming_sessions.id"],
            name="fk_streaming_segment_receipts_streaming_session_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["recordings.id"],
            name="fk_streaming_segment_receipts_recording_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["recording_pipeline_runs.id"],
            name="fk_streaming_segment_receipts_pipeline_run_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"],
            ["segments.id"],
            name="fk_streaming_segment_receipts_segment_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["chunks.id"],
            name="fk_streaming_segment_receipts_chunk_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_streaming_segment_receipts"),
    )
    op.create_index(
        "ix_streaming_segment_receipts_tenant_id",
        "streaming_segment_receipts",
        ["tenant_id"],
    )
    op.create_index(
        "ux_stream_receipts_source_event",
        "streaming_segment_receipts",
        ["tenant_id", "session_key", "source_event_key"],
        unique=True,
    )
    op.create_index(
        "ix_stream_receipts_recording_generation",
        "streaming_segment_receipts",
        ["recording_id", "generation"],
    )
    op.create_index(
        "ix_stream_receipts_segment_id",
        "streaming_segment_receipts",
        ["segment_id"],
    )
    op.create_index(
        "ix_stream_receipts_chunk_id",
        "streaming_segment_receipts",
        ["chunk_id"],
    )


def _expand_speaker_review_state() -> None:
    for column in (
        sa.Column(
            "observation_state",
            sa.String(length=24),
            server_default="PENDING_REVIEW",
            nullable=False,
        ),
        sa.Column(
            "state_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("candidate_speaker_id", sa.String(length=64), nullable=True),
        sa.Column("candidate_voiceprint_id", sa.String(length=64), nullable=True),
        sa.Column(
            "candidate_vector_encrypted",
            sa.LargeBinary(length=8192),
            nullable=True,
        ),
        sa.Column("candidate_encryption_meta", sa.JSON(), nullable=True),
        sa.Column("candidate_speech_sec", sa.Float(), nullable=True),
        sa.Column(
            "candidate_first_seen",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("candidate_role_hint", sa.String(length=32), nullable=True),
        sa.Column(
            "generation",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    ):
        op.add_column("speaker_merge_pending", column)

    op.execute(
        sa.text(
            """
            UPDATE speaker_merge_pending
            SET observation_state = CASE status
                WHEN 'resolved_inferred' THEN 'APPLIED'
                WHEN 'resolved_rejected' THEN 'REJECTED'
                ELSE 'PENDING_REVIEW'
            END
            """
        )
    )
    op.create_check_constraint(
        "ck_speaker_merge_pending_observation_state",
        "speaker_merge_pending",
        "observation_state IN "
        "('OBSERVED', 'PENDING_REVIEW', 'APPLIED', 'REJECTED')",
    )
    op.create_check_constraint(
        "ck_speaker_merge_pending_version_generation",
        "speaker_merge_pending",
        "state_version >= 1 AND generation >= 0",
    )
    op.create_index(
        "ux_speaker_merge_pending_observation",
        "speaker_merge_pending",
        [
            "tenant_id",
            "recording_id",
            "candidate_speaker_id",
            "matched_speaker_node_id",
        ],
        unique=True,
    )


def _create_erasure_outbox() -> None:
    """Create a deletion queue that survives removal of its source aggregate."""

    op.create_table(
        "erasure_outbox",
        *_base_columns(),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN "
            "('pending', 'processing', 'succeeded', 'failed', 'dead_letter')",
            name="ck_erasure_outbox_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_erasure_outbox_attempts",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_erasure_outbox"),
    )
    op.create_index(
        "ix_erasure_outbox_tenant_id",
        "erasure_outbox",
        ["tenant_id"],
    )
    op.create_index(
        "ux_erasure_outbox_subject",
        "erasure_outbox",
        ["tenant_id", "subject_type", "subject_id"],
        unique=True,
    )
    op.create_index(
        "ix_erasure_outbox_claim",
        "erasure_outbox",
        ["status", "available_at", "lease_expires_at"],
    )


def upgrade() -> None:
    """Expand, backfill deterministic projections, and then constrain."""

    _assert_reception_seconds_are_backfillable()
    _create_reception_audio_schema()
    _expand_reception_generation_columns()
    _expand_streaming_sessions()
    _create_streaming_durability_schema()
    _expand_speaker_review_state()
    _create_erasure_outbox()


def _downgrade_speaker_review_state() -> None:
    op.drop_index(
        "ux_speaker_merge_pending_observation",
        table_name="speaker_merge_pending",
    )
    op.drop_constraint(
        "ck_speaker_merge_pending_version_generation",
        "speaker_merge_pending",
        type_="check",
    )
    op.drop_constraint(
        "ck_speaker_merge_pending_observation_state",
        "speaker_merge_pending",
        type_="check",
    )
    for column_name in (
        "generation",
        "candidate_role_hint",
        "candidate_first_seen",
        "candidate_speech_sec",
        "candidate_encryption_meta",
        "candidate_vector_encrypted",
        "candidate_voiceprint_id",
        "candidate_speaker_id",
        "state_version",
        "observation_state",
    ):
        op.drop_column("speaker_merge_pending", column_name)


def _downgrade_streaming_sessions() -> None:
    op.drop_constraint(
        "ck_streaming_sessions_watermarks",
        "streaming_sessions",
        type_="check",
    )
    op.drop_constraint(
        "ck_streaming_sessions_status",
        "streaming_sessions",
        type_="check",
    )
    op.drop_constraint(
        "fk_streaming_sessions_pipeline_run_id",
        "streaming_sessions",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_streaming_sessions_pipeline_run",
        table_name="streaming_sessions",
    )
    op.drop_index(
        "ux_streaming_sessions_tenant_session_epoch",
        table_name="streaming_sessions",
    )
    op.create_unique_constraint(
        "ux_streaming_sessions_session_id",
        "streaming_sessions",
        ["session_id"],
    )
    for column_name in (
        "lease_token",
        "lease_expires_at",
        "durable_segment_high_watermark",
        "ack_seq_high_watermark",
        "pipeline_run_id",
        "generation",
        "status",
        "epoch",
    ):
        op.drop_column("streaming_sessions", column_name)


def _downgrade_reception_generation_columns() -> None:
    op.drop_constraint(
        "fk_dialogue_tag_assignments_timeline_revision_id",
        "dialogue_tag_assignments",
        type_="foreignkey",
    )
    op.drop_column("dialogue_tag_assignments", "timeline_revision_id")
    op.drop_constraint(
        "fk_dialogue_state_transitions_timeline_revision_id",
        "dialogue_state_transitions",
        type_="foreignkey",
    )
    op.drop_column("dialogue_state_transitions", "timeline_revision_id")
    op.drop_constraint(
        "fk_dialogue_units_timeline_revision_id",
        "dialogue_units",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_dialogue_units_stage_confidence",
        "dialogue_units",
        type_="check",
    )
    op.drop_column("dialogue_units", "timeline_revision_id")
    op.drop_column("dialogue_units", "stage_confidence")

    op.drop_constraint(
        "fk_reception_recordings_timeline_revision_id",
        "reception_recordings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_reception_recordings_integer_timeline",
        "reception_recordings",
        type_="check",
    )
    for column_name in (
        "gap_before_ms",
        "timeline_end_ms",
        "timeline_start_ms",
        "source_end_ms",
        "source_start_ms",
        "timeline_revision_id",
    ):
        op.drop_column("reception_recordings", column_name)


def downgrade() -> None:
    """Restore 0029 only when its global session-id invariant is satisfiable."""

    # This must remain the first operation: MySQL DDL auto-commits, so detecting
    # the incompatible reconnect history after dropping tables would leave a
    # partially downgraded database.
    _assert_legacy_streaming_session_uniqueness()

    op.drop_table("erasure_outbox")
    _downgrade_speaker_review_state()
    op.drop_table("streaming_segment_receipts")
    op.drop_table("streaming_pcm_frames")
    op.drop_table("streaming_ws_tickets")
    _downgrade_streaming_sessions()

    op.drop_table("reception_audio_artifacts")
    op.drop_table("reception_audio_operations")
    _downgrade_reception_generation_columns()
    op.drop_constraint(
        "fk_receptions_active_timeline_revision_id",
        "receptions",
        type_="foreignkey",
    )
    op.drop_column("receptions", "active_timeline_revision_id")
    op.drop_table("reception_timeline_revisions")
