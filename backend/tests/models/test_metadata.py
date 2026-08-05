"""Metadata introspection tests for the models package.

These tests verify that Base.metadata contains all registered tables and that
the models package exports the corresponding model classes.
"""

from __future__ import annotations

import pytest

import audio_graphy.models.community_summary

# M9 R1 T1: register the four new M9 tables on metadata.
import audio_graphy.models.edge_event
import audio_graphy.models.leiden_job
import audio_graphy.models.speaker_merge_pending

# M8: streaming_sessions registers its table on import; import it explicitly so
# Base.metadata is deterministic regardless of test execution order.
import audio_graphy.models.streaming_session  # noqa: F401
from audio_graphy.models import (
    AuditLog,
    Base,
    Chunk,
    ChunkSegment,
    DialogueStateTransition,
    DialogueTagAssignment,
    DialogueUnit,
    EntityAlias,
    ErasureOutbox,
    EvalRunORM,
    LLMCacheEntry,
    LLMCacheRef,
    LLMCallLog,
    PipelineState,
    ProjectionOutbox,
    Prompt,
    ProvenanceEvent,
    Reception,
    ReceptionAudioArtifact,
    ReceptionAudioOperation,
    ReceptionAutomationRun,
    ReceptionRecording,
    ReceptionTimelineRevision,
    RecomputeTask,
    Recording,
    RecordingPipelineRun,
    RecordingStatus,
    Segment,
    SpeakerLink,
    SpeakerNode,
    StreamingPCMFrame,
    StreamingSegmentReceipt,
    StreamingWSTicket,
    TagBadcase,
    TagCurrent,
    TagDeploymentAuditSubject,
    TagEvaluationItem,
    TagExperienceCase,
    TagFact,
    TagFeedbackEvent,
    TagFeedbackLaneAssignment,
    TagHarnessExecution,
    TagHarnessStageTrace,
    TagOptimizationRun,
    TagOptimizationTrial,
    TagSource,
    TagStat,
    Tenant,
    TenantScopedBase,
    User,
    UserRole,
    VectorAudio,
    VectorChunk,
    VectorEntity,
    VoiceprintVector,
)

EXPECTED_TABLES = {
    "tenants",
    "users",
    "recordings",
    "segments",
    "chunks",
    "tag_facts",
    "tag_current",
    "tag_stats",
    "prompts",
    "vectors_entity",
    "vectors_chunk",
    "api_keys",
    "audit_logs",
    "llm_call_logs",
    "recompute_tasks",
    "eval_runs",
    "entity_aliases",
    # M7
    "speaker_nodes",
    "speaker_links",
    "vectors_voiceprint",
    "vectors_audio",
    # M8
    "streaming_sessions",
    "streaming_ws_tickets",
    "streaming_pcm_frames",
    "streaming_segment_receipts",
    # Immutable audio generation / recovery
    "chunk_segments",
    "recording_pipeline_runs",
    "projection_outbox",
    "reception_timeline_revisions",
    "reception_audio_operations",
    "reception_audio_artifacts",
    "erasure_outbox",
    # M9
    "edge_events",
    "community_summaries",
    "leiden_jobs",
    "speaker_merge_pending",
    # Reception/dialogue workspace
    "receptions",
    "reception_recordings",
    "dialogue_units",
    "dialogue_state_transitions",
    "dialogue_tag_assignments",
    "provenance_events",
    "reception_automation_runs",
    # Tag governance closed loop
    "integration_callbacks",
    "integration_uploads",
    "legacy_tag_mappings",
    "tag_assignment_current",
    "tag_assignment_facts",
    "tag_deployment_observation_samples",
    "tag_deployment_observations",
    "tag_deployment_audit_subjects",
    "tag_deployments",
    "tag_evaluation_metrics",
    "tag_evaluation_runs",
    "tag_extraction_jobs",
    "tag_extraction_runs",
    "tag_gate_results",
    "tag_gold_labels",
    "tag_gold_set_versions",
    "tag_gold_sets",
    "tag_governance_audit_events",
    "tag_review_decisions",
    "tag_review_tasks",
    "tag_schema_versions",
    "tag_schemas",
    "tagger_versions",
    # Semantic-tag Harness evolution
    "tag_harness_executions",
    "tag_harness_stage_traces",
    "tag_feedback_events",
    "tag_feedback_lane_assignments",
    "tag_evaluation_items",
    "tag_badcases",
    "tag_experience_cases",
    "tag_optimization_runs",
    "tag_optimization_trials",
    # Offline prompt compilation
    "tag_prompt_artifacts",
    "tag_prompt_gradients",
    "tag_prompt_demo_sources",
    "tag_silver_labels",
    # Centralized encrypted LLM cache
    "llm_cache_entries",
    "llm_cache_refs",
    "llm_cache_source_guards",
    "llm_cache_purges",
}

EXPECTED_MODELS = {
    Tenant,
    User,
    Recording,
    Segment,
    Chunk,
    ChunkSegment,
    TagFact,
    TagCurrent,
    TagStat,
    Prompt,
    VectorEntity,
    VectorChunk,
    AuditLog,
    LLMCallLog,
    RecomputeTask,
    EvalRunORM,
    EntityAlias,
    ErasureOutbox,
    # M7
    SpeakerNode,
    SpeakerLink,
    VoiceprintVector,
    VectorAudio,
    Reception,
    ReceptionRecording,
    ReceptionTimelineRevision,
    ReceptionAudioOperation,
    ReceptionAudioArtifact,
    DialogueUnit,
    DialogueStateTransition,
    DialogueTagAssignment,
    ProvenanceEvent,
    ReceptionAutomationRun,
    RecordingPipelineRun,
    ProjectionOutbox,
    StreamingWSTicket,
    StreamingPCMFrame,
    StreamingSegmentReceipt,
    LLMCacheEntry,
    LLMCacheRef,
    TagHarnessExecution,
    TagHarnessStageTrace,
    TagFeedbackEvent,
    TagFeedbackLaneAssignment,
    TagDeploymentAuditSubject,
    TagEvaluationItem,
    TagBadcase,
    TagExperienceCase,
    TagOptimizationRun,
    TagOptimizationTrial,
}


@pytest.mark.integration
class TestMetadata:
    """Verify metadata includes the reception/dialogue workspace tables."""

    def test_metadata_has_all_tables(self) -> None:
        assert len(Base.metadata.tables) == len(EXPECTED_TABLES)

    def test_metadata_table_names(self) -> None:
        assert set(Base.metadata.tables.keys()) == EXPECTED_TABLES

    def test_each_model_has_tablename(self) -> None:
        for model in EXPECTED_MODELS:
            assert hasattr(model, "__tablename__")
            assert model.__tablename__ in EXPECTED_TABLES


@pytest.mark.integration
class TestModelExports:
    """Verify all registered model classes are exported from the package."""

    def test_all_models_importable(self) -> None:
        for model in EXPECTED_MODELS:
            assert model.__name__ is not None

    def test_base_classes_exported(self) -> None:
        assert Base is not None
        assert TenantScopedBase is not None

    def test_enums_exported(self) -> None:
        assert UserRole is not None
        assert RecordingStatus is not None
        assert PipelineState is not None
        assert TagSource is not None


@pytest.mark.integration
class TestEnumValues:
    """Verify enum values match PRD CHECK constraints."""

    def test_user_role_values(self) -> None:
        values = {e.value for e in UserRole}
        assert values == {"admin", "inspector", "agent", "viewer"}

    def test_recording_status_values(self) -> None:
        values = {e.value for e in RecordingStatus}
        assert values == {
            "queued",
            "processing",
            "indexed",
            "ready_no_speech",
            "failed",
            "archived",
        }

    def test_pipeline_state_values(self) -> None:
        values = {e.value for e in PipelineState}
        assert values == {
            "pending",
            "vad",
            "asr",
            "chunking",
            "embedding",
            "extraction",
            "graph_merge",
            "tagging",
            "done",
            "error",
        }

    def test_tag_source_values(self) -> None:
        values = {e.value for e in TagSource}
        assert values == {"llm", "manual"}


@pytest.mark.integration
class TestInheritanceHierarchy:
    """Verify the inheritance hierarchy is correct."""

    def test_tenant_inherits_base(self) -> None:
        assert issubclass(Tenant, Base)
        assert not issubclass(Tenant, TenantScopedBase)

    def test_prompt_inherits_base(self) -> None:
        assert issubclass(Prompt, Base)
        assert not issubclass(Prompt, TenantScopedBase)

    def test_business_tables_inherit_tsb(self) -> None:
        tsb_models = {
            User,
            Recording,
            Segment,
            Chunk,
            ChunkSegment,
            RecordingPipelineRun,
            ProjectionOutbox,
            TagFact,
            TagCurrent,
            TagStat,
            VectorEntity,
            VectorChunk,
            AuditLog,
            LLMCallLog,
            LLMCacheEntry,
            LLMCacheRef,
            TagHarnessExecution,
            TagHarnessStageTrace,
            TagFeedbackEvent,
            TagEvaluationItem,
            TagBadcase,
            TagExperienceCase,
            TagOptimizationRun,
            TagOptimizationTrial,
            EntityAlias,
            Reception,
            ReceptionRecording,
            DialogueUnit,
            DialogueStateTransition,
            DialogueTagAssignment,
            ProvenanceEvent,
        }
        for model in tsb_models:
            assert issubclass(model, TenantScopedBase)
            assert issubclass(model, Base)
