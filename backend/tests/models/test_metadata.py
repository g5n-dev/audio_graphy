"""Metadata introspection tests for the models package.

These tests verify that Base.metadata contains all 13 tables and that
the models package exports all 13 model classes.
"""

from __future__ import annotations

import pytest

from audio_graphy.models import (
    AuditLog,
    Base,
    Chunk,
    LLMCallLog,
    PipelineState,
    Prompt,
    RecomputeTask,
    Recording,
    RecordingStatus,
    Segment,
    TagCurrent,
    TagFact,
    TagSource,
    TagStat,
    Tenant,
    TenantScopedBase,
    User,
    UserRole,
    VectorChunk,
    VectorEntity,
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
}


@pytest.mark.integration
class TestMetadata:
    """Verify Base.metadata contains all 14 tables (13 original + recompute_tasks)."""

    def test_metadata_has_13_tables(self) -> None:
        assert len(Base.metadata.tables) == 14

    def test_metadata_table_names(self) -> None:
        assert set(Base.metadata.tables.keys()) == EXPECTED_TABLES

    def test_each_model_has_tablename(self) -> None:
        for model in EXPECTED_MODELS:
            assert hasattr(model, "__tablename__")
            assert model.__tablename__ in EXPECTED_TABLES


@pytest.mark.integration
class TestModelExports:
    """Verify all 13 model classes are exported from the package."""

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
        }
        for model in tsb_models:
            assert issubclass(model, TenantScopedBase)
            assert issubclass(model, Base)
