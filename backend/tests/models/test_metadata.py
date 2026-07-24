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
    DialogueStateTransition,
    DialogueTagAssignment,
    DialogueUnit,
    EntityAlias,
    EvalRunORM,
    LLMCallLog,
    PipelineState,
    Prompt,
    ProvenanceEvent,
    Reception,
    ReceptionAutomationRun,
    ReceptionRecording,
    RecomputeTask,
    Recording,
    RecordingStatus,
    Segment,
    SpeakerLink,
    SpeakerNode,
    TagCurrent,
    TagFact,
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
}

EXPECTED_MODELS = {
    Tenant,
    User,
    Recording,
    Segment,
    Chunk,
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
    # M7
    SpeakerNode,
    SpeakerLink,
    VoiceprintVector,
    VectorAudio,
    Reception,
    ReceptionRecording,
    DialogueUnit,
    DialogueStateTransition,
    DialogueTagAssignment,
    ProvenanceEvent,
    ReceptionAutomationRun,
}


@pytest.mark.integration
class TestMetadata:
    """Verify metadata includes the reception/dialogue workspace tables."""

    def test_metadata_has_all_tables(self) -> None:
        assert len(Base.metadata.tables) == 32

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
        assert values == {"queued", "processing", "indexed", "failed", "archived"}

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
            TagFact,
            TagCurrent,
            TagStat,
            VectorEntity,
            VectorChunk,
            AuditLog,
            LLMCallLog,
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
