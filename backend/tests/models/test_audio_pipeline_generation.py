"""Contract tests for generation-isolated recording pipeline state."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect

from audio_graphy.models import (
    Base,
    Chunk,
    ChunkSegment,
    ProjectionOutbox,
    Recording,
    RecordingPipelineRun,
    Segment,
)
from audio_graphy.models.pipeline import pipeline_run_transition_allowed


def test_recording_persists_source_facts_and_active_generation() -> None:
    columns = inspect(Recording).columns

    assert {
        "audio_duration_ms",
        "audio_sha256",
        "audio_size_bytes",
        "audio_sample_rate",
        "audio_channels",
        "source_revision",
        "active_pipeline_run_id",
    } <= set(columns.keys())
    assert columns.source_revision.default is not None
    assert columns.active_pipeline_run_id.nullable


def test_pipeline_models_are_registered_and_tenant_scoped() -> None:
    assert RecordingPipelineRun.__tablename__ == "recording_pipeline_runs"
    assert ProjectionOutbox.__tablename__ == "projection_outbox"
    assert "recording_pipeline_runs" in Base.metadata.tables
    assert "projection_outbox" in Base.metadata.tables

    run_columns = inspect(RecordingPipelineRun).columns
    assert {
        "tenant_id",
        "recording_id",
        "generation",
        "idempotency_key",
        "source_fingerprint",
        "config_fingerprint",
        "state",
        "lease_owner",
        "lease_expires_at",
        "attempt_count",
        "required_projections",
        "completed_projections",
        "error_code",
        "error_message",
        "started_at",
        "finished_at",
        "activated_at",
    } <= set(run_columns.keys())

    outbox_columns = inspect(ProjectionOutbox).columns
    assert {
        "tenant_id",
        "recording_id",
        "pipeline_run_id",
        "generation",
        "projection_type",
        "aggregate_type",
        "aggregate_id",
        "payload",
        "status",
        "attempts",
        "available_at",
        "lease_owner",
        "lease_expires_at",
        "idempotency_key",
        "error_message",
    } <= set(outbox_columns.keys())


def test_segments_and_chunks_are_generation_isolated() -> None:
    segment_columns = inspect(Segment).columns
    chunk_columns = inspect(Chunk).columns

    assert {"pipeline_run_id", "generation"} <= set(segment_columns.keys())
    assert {"pipeline_run_id", "generation", "ordinal"} <= set(chunk_columns.keys())

    segment_indexes = {index.name: index for index in Segment.__table__.indexes}
    assert list(
        segment_indexes["ux_segments_recording_generation_idx"].columns.keys()
    ) == ["recording_id", "generation", "idx"]
    assert segment_indexes["ux_segments_recording_generation_idx"].unique

    chunk_indexes = {index.name: index for index in Chunk.__table__.indexes}
    assert list(
        chunk_indexes["ux_chunks_recording_generation_ordinal"].columns.keys()
    ) == ["recording_id", "generation", "ordinal"]
    assert chunk_indexes["ux_chunks_recording_generation_ordinal"].unique
    assert not chunk_indexes["ix_chunks_content_hash"].unique


def test_chunk_segment_provenance_uses_real_database_ids() -> None:
    columns = inspect(ChunkSegment).columns
    assert {
        "tenant_id",
        "recording_id",
        "pipeline_run_id",
        "generation",
        "chunk_id",
        "segment_id",
        "ordinal",
    } <= set(columns.keys())

    indexes = {index.name: index for index in ChunkSegment.__table__.indexes}
    assert indexes["ux_chunk_segments_chunk_ordinal"].unique
    assert indexes["ux_chunk_segments_chunk_segment"].unique


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("queued", "claimed"),
        ("claimed", "vad"),
        ("vad", "asr"),
        ("asr", "segments"),
        ("asr", "verifying"),
        ("segments", "chunks"),
        ("chunks", "projections"),
        ("projections", "verifying"),
        ("verifying", "ready"),
        ("verifying", "ready_no_speech"),
        ("failed_retryable", "claimed"),
        ("asr", "claimed"),
        ("ready", "superseded"),
        ("chunks", "partial"),
        ("vad", "failed_retryable"),
    ],
)
def test_pipeline_state_model_accepts_only_declared_edges(
    current: str,
    target: str,
) -> None:
    assert pipeline_run_transition_allowed(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("queued", "ready"),
        ("claimed", "chunks"),
        ("vad", "projections"),
        ("asr", "ready"),
        ("segments", "verifying"),
        ("ready", "claimed"),
        ("ready_no_speech", "ready"),
        ("partial", "vad"),
        ("failed_terminal", "claimed"),
        ("superseded", "ready"),
        ("unknown", "claimed"),
    ],
)
def test_pipeline_state_model_rejects_illegal_jumps(
    current: str,
    target: str,
) -> None:
    assert not pipeline_run_transition_allowed(current, target)
