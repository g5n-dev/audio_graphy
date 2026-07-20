"""Unit tests for core/types.py — shared types, constants, exceptions, helpers.

Tests cover:
    - GraphRAG delimiter constants
    - DEFAULT_ENTITY_TYPES
    - GraphNode / GraphEdge / GraphSnapshot frozen + slots
    - VectorSearchHit
    - Exception hierarchy
    - upgrade_confidence function
    - normalize_confidence_score function
"""

from __future__ import annotations

import dataclasses

import pytest

from audio_graphy.core.types import (
    COMPLETION_DELIMITER,
    DEFAULT_ENTITY_TYPES,
    RECORD_DELIMITER,
    TUPLE_DELIMITER,
    AudioGraphyError,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    ParseError,
    PipelineError,
    StorageError,
    VectorSearchHit,
    normalize_confidence_score,
    upgrade_confidence,
)


@pytest.mark.unit
class TestGraphRAGDelimiters:
    """GraphRAG delimiter protocol constants."""

    def test_tuple_delimiter(self) -> None:
        assert TUPLE_DELIMITER == "<|>"

    def test_record_delimiter(self) -> None:
        assert RECORD_DELIMITER == "##"

    def test_completion_delimiter(self) -> None:
        assert COMPLETION_DELIMITER == "<|COMPLETE|>"


@pytest.mark.unit
class TestDefaultEntityTypes:
    """Default entity types for car-sales domain."""

    def test_contains_8_types(self) -> None:
        assert len(DEFAULT_ENTITY_TYPES) == 8

    def test_contains_car_sales_types(self) -> None:
        assert "客户" in DEFAULT_ENTITY_TYPES
        assert "坐席" in DEFAULT_ENTITY_TYPES
        assert "车型" in DEFAULT_ENTITY_TYPES
        assert "价格方案" in DEFAULT_ENTITY_TYPES
        assert "金融政策" in DEFAULT_ENTITY_TYPES
        assert "优惠权益" in DEFAULT_ENTITY_TYPES
        assert "竞品" in DEFAULT_ENTITY_TYPES
        assert "预约事件" in DEFAULT_ENTITY_TYPES


@pytest.mark.unit
class TestDataclassProperties:
    """Verify frozen + slots on all shared dataclasses."""

    def test_graph_node_frozen(self) -> None:
        """GraphNode is frozen (immutable)."""
        node = GraphNode(
            entity_id="X",
            name="X",
            type="车型",
            description="desc",
            source_ids=["1_0"],
            recording_ids=[1],
            degree=2,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            node.name = "Y"  # type: ignore[misc]

    def test_graph_edge_frozen(self) -> None:
        """GraphEdge is frozen."""
        edge = GraphEdge(
            source="A",
            target="B",
            relation="推荐",
            weight=1.0,
            confidence="EXTRACTED",
            confidence_score=1.0,
            source_ids=["1_0"],
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            edge.weight = 2.0  # type: ignore[misc]

    def test_graph_snapshot_frozen(self) -> None:
        """GraphSnapshot is frozen."""
        snap = GraphSnapshot(
            nodes=[],
            edges=[],
            total_entities=0,
            total_relations=0,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.total_entities = 5  # type: ignore[misc]

    def test_vector_search_hit_frozen(self) -> None:
        """VectorSearchHit is frozen."""
        hit = VectorSearchHit(id="entity_1", score=0.95)
        with pytest.raises(dataclasses.FrozenInstanceError):
            hit.score = 1.0  # type: ignore[misc]

    def test_graph_node_slots(self) -> None:
        """GraphNode uses __slots__."""
        assert hasattr(GraphNode, "__slots__")

    def test_graph_edge_slots(self) -> None:
        """GraphEdge uses __slots__."""
        assert hasattr(GraphEdge, "__slots__")


@pytest.mark.unit
class TestExceptionHierarchy:
    """Exception hierarchy: AudioGraphyError → sub-classes."""

    def test_base_exception(self) -> None:
        assert issubclass(AudioGraphyError, Exception)

    def test_parse_error(self) -> None:
        assert issubclass(ParseError, AudioGraphyError)

    def test_storage_error(self) -> None:
        assert issubclass(StorageError, AudioGraphyError)

    def test_pipeline_error(self) -> None:
        assert issubclass(PipelineError, AudioGraphyError)

    def test_catch_base(self) -> None:
        """Catching AudioGraphyError catches all sub-classes."""
        with pytest.raises(AudioGraphyError):
            raise ParseError("test")
        with pytest.raises(AudioGraphyError):
            raise StorageError("test")
        with pytest.raises(AudioGraphyError):
            raise PipelineError("test")


@pytest.mark.unit
class TestUpgradeConfidence:
    """Confidence upgrade rule: EXTRACTED > INFERRED > AMBIGUOUS."""

    def test_extracted_stays_extracted(self) -> None:
        assert upgrade_confidence("EXTRACTED", "INFERRED") == "EXTRACTED"
        assert upgrade_confidence("EXTRACTED", "AMBIGUOUS") == "EXTRACTED"
        assert upgrade_confidence("EXTRACTED", "EXTRACTED") == "EXTRACTED"

    def test_inferred_upgrades_to_extracted(self) -> None:
        assert upgrade_confidence("INFERRED", "EXTRACTED") == "EXTRACTED"

    def test_inferred_stays_inferred(self) -> None:
        assert upgrade_confidence("INFERRED", "INFERRED") == "INFERRED"

    def test_ambiguous_upgrades_to_inferred(self) -> None:
        assert upgrade_confidence("AMBIGUOUS", "INFERRED") == "INFERRED"

    def test_ambiguous_upgrades_to_extracted(self) -> None:
        assert upgrade_confidence("AMBIGUOUS", "EXTRACTED") == "EXTRACTED"

    def test_ambiguous_stays_ambiguous(self) -> None:
        assert upgrade_confidence("AMBIGUOUS", "AMBIGUOUS") == "AMBIGUOUS"


@pytest.mark.unit
class TestNormalizeConfidenceScore:
    """Confidence score computation."""

    def test_extracted_score(self) -> None:
        assert normalize_confidence_score("EXTRACTED", 1.0) == 1.0
        assert normalize_confidence_score("EXTRACTED", 5.0) == 1.0

    def test_inferred_score_in_range(self) -> None:
        score = normalize_confidence_score("INFERRED", 1.0)
        assert score is not None
        assert 0.0 < score < 1.0

    def test_inferred_score_increases_with_weight(self) -> None:
        s1 = normalize_confidence_score("INFERRED", 1.0)
        s5 = normalize_confidence_score("INFERRED", 5.0)
        assert s5 is not None and s1 is not None
        assert s5 > s1

    def test_ambiguous_score_none(self) -> None:
        assert normalize_confidence_score("AMBIGUOUS", 1.0) is None
