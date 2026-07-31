"""Add generation-isolated recording runs and durable projection outbox.

Revision ID: 0029_audio_consistency_runs
Revises: 0028_job_budget_baseline
Create Date: 2026-07-29 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029_audio_consistency_runs"
down_revision: str | None = "0028_job_budget_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base_columns() -> list[sa.Column[object]]:
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


def upgrade() -> None:
    op.create_table(
        "recording_pipeline_runs",
        *_base_columns(),
        sa.Column("recording_id", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            sa.String(length=32),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("required_projections", sa.JSON(), nullable=False),
        sa.Column("completed_projections", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("generation >= 1", name="ck_pipeline_runs_generation"),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_pipeline_runs_attempt_count",
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'claimed', 'vad', 'asr', 'segments', 'chunks', "
            "'projections', 'verifying', 'ready', 'ready_no_speech', 'partial', "
            "'failed_retryable', 'failed_terminal', 'superseded')",
            name="ck_pipeline_runs_state",
        ),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["recordings.id"],
            name="fk_pipeline_runs_recording_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_recording_pipeline_runs"),
    )
    op.create_index(
        "ix_recording_pipeline_runs_tenant_id",
        "recording_pipeline_runs",
        ["tenant_id"],
    )
    op.create_index(
        "ix_pipeline_runs_tenant_recording",
        "recording_pipeline_runs",
        ["tenant_id", "recording_id"],
    )
    op.create_index(
        "ix_pipeline_runs_claim",
        "recording_pipeline_runs",
        ["state", "lease_expires_at", "created_at"],
    )
    op.create_index(
        "ux_pipeline_runs_recording_generation",
        "recording_pipeline_runs",
        ["recording_id", "generation"],
        unique=True,
    )
    op.create_index(
        "ux_pipeline_runs_tenant_idempotency",
        "recording_pipeline_runs",
        ["tenant_id", "idempotency_key"],
        unique=True,
    )

    for column in (
        sa.Column("audio_duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("audio_sha256", sa.String(length=64), nullable=True),
        sa.Column("audio_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("audio_sample_rate", sa.Integer(), nullable=True),
        sa.Column("audio_channels", sa.Integer(), nullable=True),
        sa.Column(
            "source_revision",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("active_pipeline_run_id", sa.BigInteger(), nullable=True),
    ):
        op.add_column("recordings", column)
    op.create_foreign_key(
        "fk_recordings_active_pipeline_run_id",
        "recordings",
        "recording_pipeline_runs",
        ["active_pipeline_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_recordings_audio_duration_ms",
        "recordings",
        "audio_duration_ms IS NULL OR audio_duration_ms >= 0",
    )
    op.create_check_constraint(
        "ck_recordings_audio_size_bytes",
        "recordings",
        "audio_size_bytes IS NULL OR audio_size_bytes > 0",
    )
    op.create_check_constraint(
        "ck_recordings_audio_sample_rate",
        "recordings",
        "audio_sample_rate IS NULL OR audio_sample_rate > 0",
    )
    op.create_check_constraint(
        "ck_recordings_audio_channels",
        "recordings",
        "audio_channels IS NULL OR audio_channels > 0",
    )
    op.create_check_constraint(
        "ck_recordings_source_revision",
        "recordings",
        "source_revision >= 1",
    )
    op.create_index(
        "ix_recordings_active_pipeline_run",
        "recordings",
        ["active_pipeline_run_id"],
    )
    op.create_index(
        "ix_recordings_tenant_audio_sha256",
        "recordings",
        ["tenant_id", "audio_sha256"],
    )

    op.add_column(
        "segments",
        sa.Column("pipeline_run_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "segments",
        sa.Column(
            "generation",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_segments_pipeline_run_id",
        "segments",
        "recording_pipeline_runs",
        ["pipeline_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_segments_generation",
        "segments",
        "generation >= 0",
    )
    op.drop_index("ux_segments_recording_idx", table_name="segments")
    op.create_index(
        "ux_segments_recording_generation_idx",
        "segments",
        ["recording_id", "generation", "idx"],
        unique=True,
    )
    op.create_index(
        "ix_segments_pipeline_run_id",
        "segments",
        ["pipeline_run_id"],
    )

    op.add_column(
        "chunks",
        sa.Column("pipeline_run_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "chunks",
        sa.Column(
            "generation",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column("chunks", sa.Column("ordinal", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_chunks_pipeline_run_id",
        "chunks",
        "recording_pipeline_runs",
        ["pipeline_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint("ck_chunks_generation", "chunks", "generation >= 0")
    op.create_check_constraint(
        "ck_chunks_ordinal",
        "chunks",
        "ordinal IS NULL OR ordinal >= 0",
    )
    op.drop_index("ux_chunks_content_hash", table_name="chunks")
    op.create_index(
        "ix_chunks_content_hash",
        "chunks",
        ["tenant_id", "content_hash"],
    )
    op.create_index(
        "ux_chunks_recording_generation_ordinal",
        "chunks",
        ["recording_id", "generation", "ordinal"],
        unique=True,
    )
    op.create_index("ix_chunks_pipeline_run_id", "chunks", ["pipeline_run_id"])

    op.create_table(
        "chunk_segments",
        *_base_columns(),
        sa.Column("recording_id", sa.BigInteger(), nullable=False),
        sa.Column("pipeline_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "generation",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("chunk_id", sa.BigInteger(), nullable=False),
        sa.Column("segment_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint("generation >= 0", name="ck_chunk_segments_generation"),
        sa.CheckConstraint("ordinal >= 0", name="ck_chunk_segments_ordinal"),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["recordings.id"],
            name="fk_chunk_segments_recording_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["recording_pipeline_runs.id"],
            name="fk_chunk_segments_pipeline_run_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["chunks.id"],
            name="fk_chunk_segments_chunk_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"],
            ["segments.id"],
            name="fk_chunk_segments_segment_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chunk_segments"),
    )
    op.create_index("ix_chunk_segments_tenant_id", "chunk_segments", ["tenant_id"])
    op.create_index(
        "ux_chunk_segments_chunk_ordinal",
        "chunk_segments",
        ["chunk_id", "ordinal"],
        unique=True,
    )
    op.create_index(
        "ux_chunk_segments_chunk_segment",
        "chunk_segments",
        ["chunk_id", "segment_id"],
        unique=True,
    )
    op.create_index(
        "ix_chunk_segments_recording_generation",
        "chunk_segments",
        ["recording_id", "generation"],
    )
    op.create_index(
        "ix_chunk_segments_segment_id",
        "chunk_segments",
        ["segment_id"],
    )
    op.create_index(
        "ix_chunk_segments_pipeline_run_id",
        "chunk_segments",
        ["pipeline_run_id"],
    )

    op.create_table(
        "projection_outbox",
        *_base_columns(),
        sa.Column("recording_id", sa.BigInteger(), nullable=False),
        sa.Column("pipeline_run_id", sa.BigInteger(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("projection_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
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
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint("generation >= 1", name="ck_projection_outbox_generation"),
        sa.CheckConstraint("attempts >= 0", name="ck_projection_outbox_attempts"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'failed', 'dead_letter')",
            name="ck_projection_outbox_status",
        ),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["recordings.id"],
            name="fk_projection_outbox_recording_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["recording_pipeline_runs.id"],
            name="fk_projection_outbox_pipeline_run_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_projection_outbox"),
    )
    op.create_index("ix_projection_outbox_tenant_id", "projection_outbox", ["tenant_id"])
    op.create_index(
        "ux_projection_outbox_tenant_idempotency",
        "projection_outbox",
        ["tenant_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_projection_outbox_claim",
        "projection_outbox",
        ["status", "available_at", "lease_expires_at", "created_at"],
    )
    op.create_index(
        "ix_projection_outbox_run",
        "projection_outbox",
        ["pipeline_run_id", "projection_type", "aggregate_type", "aggregate_id"],
    )
    op.create_index(
        "ix_projection_outbox_tenant_recording",
        "projection_outbox",
        ["tenant_id", "recording_id"],
    )


def downgrade() -> None:
    op.drop_table("projection_outbox")
    op.drop_table("chunk_segments")

    op.drop_constraint("fk_chunks_pipeline_run_id", "chunks", type_="foreignkey")
    op.drop_index("ix_chunks_pipeline_run_id", table_name="chunks")
    op.drop_index("ux_chunks_recording_generation_ordinal", table_name="chunks")
    op.drop_index("ix_chunks_content_hash", table_name="chunks")
    op.create_index(
        "ux_chunks_content_hash",
        "chunks",
        ["tenant_id", "content_hash"],
        unique=True,
    )
    op.drop_constraint("ck_chunks_ordinal", "chunks", type_="check")
    op.drop_constraint("ck_chunks_generation", "chunks", type_="check")
    op.drop_column("chunks", "ordinal")
    op.drop_column("chunks", "generation")
    op.drop_column("chunks", "pipeline_run_id")

    op.drop_constraint("fk_segments_pipeline_run_id", "segments", type_="foreignkey")
    op.drop_index("ix_segments_pipeline_run_id", table_name="segments")
    op.drop_index("ux_segments_recording_generation_idx", table_name="segments")
    op.create_index(
        "ux_segments_recording_idx",
        "segments",
        ["recording_id", "idx"],
        unique=True,
    )
    op.drop_constraint("ck_segments_generation", "segments", type_="check")
    op.drop_column("segments", "generation")
    op.drop_column("segments", "pipeline_run_id")

    op.drop_index("ix_recordings_tenant_audio_sha256", table_name="recordings")
    op.drop_constraint(
        "fk_recordings_active_pipeline_run_id",
        "recordings",
        type_="foreignkey",
    )
    op.drop_index("ix_recordings_active_pipeline_run", table_name="recordings")
    for constraint in (
        "ck_recordings_source_revision",
        "ck_recordings_audio_channels",
        "ck_recordings_audio_sample_rate",
        "ck_recordings_audio_size_bytes",
        "ck_recordings_audio_duration_ms",
    ):
        op.drop_constraint(constraint, "recordings", type_="check")
    for column in (
        "active_pipeline_run_id",
        "source_revision",
        "audio_channels",
        "audio_sample_rate",
        "audio_size_bytes",
        "audio_sha256",
        "audio_duration_ms",
    ):
        op.drop_column("recordings", column)

    op.drop_table("recording_pipeline_runs")
