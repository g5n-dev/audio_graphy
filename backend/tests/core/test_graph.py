"""Unit tests for GraphBuilder — cross-chunk merge + edge confidence.

Tests cover:
    - Entity type majority vote
    - Description dedup + concatenation + truncation
    - Edge weight accumulation
    - Confidence upgrade (EXTRACTED > INFERRED > AMBIGUOUS)
    - AMBIGUOUS detection (same name, different types)
    - Cross-recording entity merge
    - Node degree computation
    - GraphML persistence
"""

from __future__ import annotations

from typing import Any

import pytest

from audio_graphy.core.extractor import ExtractedEntity, ExtractedRelation, ExtractionResult
from audio_graphy.core.graph import GraphBuilder


def _make_extraction(
    chunk_id: int,
    recording_id: int,
    entities: list[tuple[str, str, str]] | None = None,
    relations: list[tuple[str, str, str, str, str]] | None = None,
    confidence: str = "EXTRACTED",
) -> ExtractionResult:
    """Helper to create an ExtractionResult.

    Entity tuple: (name, type, description)
    Relation tuple: (source, relation, target, description, confidence)
    """
    ents = [
        ExtractedEntity(name=n, type=t, description=d, chunk_id=chunk_id, recording_id=recording_id)
        for n, t, d in (entities or [])
    ]
    rels = [
        ExtractedRelation(
            source_name=s,
            target_name=t,
            relation=r,
            description=d,
            weight=1.0,
            confidence=conf,  # type: ignore[arg-type]
            chunk_id=chunk_id,
            recording_id=recording_id,
        )
        for s, r, t, d, conf in (relations or [(s, r, t, d, confidence) for s, r, t, d in []])
    ]
    # Handle relations properly
    rels = []
    for item in relations or []:
        if len(item) == 5:
            s, r, t, d, c = item
        else:
            s, r, t, d = item
            c = confidence
        rels.append(
            ExtractedRelation(
                source_name=s,
                target_name=t,
                relation=r,
                description=d,
                weight=1.0,
                confidence=c,  # type: ignore[arg-type]
                chunk_id=chunk_id,
                recording_id=recording_id,
            )
        )
    return ExtractionResult(
        chunk_id=chunk_id,
        recording_id=recording_id,
        entities=ents,
        relations=rels,
        parse_success=True,
        gleaning_rounds=0,
    )


@pytest.mark.unit
class TestEntityMerge:
    """Entity merging logic."""

    async def test_type_majority_vote(self, graph_store: Any) -> None:
        """Same entity with different types → majority vote."""
        extractions = [
            _make_extraction(1, 1, entities=[("A", "车型", "desc1")]),
            _make_extraction(2, 1, entities=[("A", "车型", "desc2")]),
            _make_extraction(3, 1, entities=[("A", "竞品", "desc3")]),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        node_a = next(n for n in snapshot.nodes if n.entity_id == "A")
        assert node_a.type == "车型"  # 2 votes vs 1

    async def test_description_dedup(self, graph_store: Any) -> None:
        """Same description appears once."""
        extractions = [
            _make_extraction(1, 1, entities=[("A", "车型", "相同描述")]),
            _make_extraction(2, 1, entities=[("A", "车型", "相同描述")]),
            _make_extraction(3, 1, entities=[("A", "车型", "不同描述")]),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        node_a = next(n for n in snapshot.nodes if n.entity_id == "A")
        assert "相同描述" in node_a.description
        assert node_a.description.count("相同描述") == 1  # Deduplicated
        assert "不同描述" in node_a.description

    async def test_description_truncation(self, graph_store: Any) -> None:
        """Description > 512 chars is truncated."""
        long_desc = "A" * 600
        extractions = [
            _make_extraction(1, 1, entities=[("A", "车型", long_desc)]),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        node_a = next(n for n in snapshot.nodes if n.entity_id == "A")
        assert len(node_a.description) <= 512

    async def test_source_ids_union(self, graph_store: Any) -> None:
        """source_ids is the union of all chunk references."""
        extractions = [
            _make_extraction(1, 1, entities=[("A", "车型", "d1")]),
            _make_extraction(2, 1, entities=[("A", "车型", "d2")]),
            _make_extraction(3, 2, entities=[("A", "车型", "d3")]),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        node_a = next(n for n in snapshot.nodes if n.entity_id == "A")
        assert set(node_a.source_ids) == {"1_1", "1_2", "2_3"}
        assert set(node_a.recording_ids) == {1, 2}


@pytest.mark.unit
class TestEdgeMerge:
    """Edge merging logic."""

    async def test_weight_accumulation(self, graph_store: Any) -> None:
        """Same (source, target, relation) accumulates weight."""
        extractions = [
            _make_extraction(
                1,
                1,
                entities=[("A", "坐席", "d"), ("B", "车型", "d")],
                relations=[("A", "推荐", "B", "d", "EXTRACTED")],
            ),
            _make_extraction(
                2,
                1,
                entities=[("A", "坐席", "d"), ("B", "车型", "d")],
                relations=[("A", "推荐", "B", "d", "EXTRACTED")],
            ),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        edge = next(e for e in snapshot.edges if e.relation == "推荐")
        assert edge.weight == 2.0

    async def test_confidence_upgrade(self, graph_store: Any) -> None:
        """INFERRED + EXTRACTED → EXTRACTED."""
        extractions = [
            _make_extraction(
                1,
                1,
                entities=[("A", "坐席", "d"), ("B", "车型", "d")],
                relations=[("A", "推荐", "B", "d", "INFERRED")],
            ),
            _make_extraction(
                2,
                1,
                entities=[("A", "坐席", "d"), ("B", "车型", "d")],
                relations=[("A", "推荐", "B", "d", "EXTRACTED")],
            ),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        edge = next(e for e in snapshot.edges if e.relation == "推荐")
        assert edge.confidence == "EXTRACTED"
        assert edge.confidence_score == 1.0

    async def test_multiple_relation_types(self, graph_store: Any) -> None:
        """Same entity pair with different relations → separate edges."""
        extractions = [
            _make_extraction(
                1,
                1,
                entities=[("客户", "客户", "d"), ("CS75 Plus", "车型", "d")],
                relations=[
                    ("客户", "询问", "CS75 Plus", "d", "EXTRACTED"),
                    ("客户", "对比", "CS75 Plus", "d", "EXTRACTED"),
                ],
            ),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        assert snapshot.total_relations == 2
        relations = {e.relation for e in snapshot.edges}
        assert relations == {"询问", "对比"}


@pytest.mark.unit
class TestAmbiguousDetection:
    """AMBIGUOUS entity detection."""

    async def test_ambiguous_different_types(self, graph_store: Any) -> None:
        """Same name, different types → AMBIGUOUS edges."""
        extractions = [
            _make_extraction(1, 1, entities=[("客户", "客户", "d1")]),
            _make_extraction(2, 1, entities=[("客户", "坐席", "d2")]),
            _make_extraction(
                3,
                1,
                entities=[("客户", "客户", "d3"), ("CS75 Plus", "车型", "d")],
                relations=[("客户", "推荐", "CS75 Plus", "d", "EXTRACTED")],
            ),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        # The edge should be AMBIGUOUS because "客户" has conflicting types
        edge = next(e for e in snapshot.edges if e.relation == "推荐")
        assert edge.confidence == "AMBIGUOUS"
        assert edge.confidence_score is None


@pytest.mark.unit
class TestCrossRecordingMerge:
    """Cross-recording entity merge."""

    async def test_cross_recording_entity(self, graph_store: Any) -> None:
        """Same entity in multiple recordings is merged."""
        extractions = [
            _make_extraction(1, 1, entities=[("CS75 Plus", "车型", "rec1 desc")]),
            _make_extraction(2, 2, entities=[("CS75 Plus", "车型", "rec2 desc")]),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        assert snapshot.total_entities == 1  # Merged into one
        node = snapshot.nodes[0]
        assert set(node.recording_ids) == {1, 2}
        assert snapshot.cross_recording_entities == 1

    async def test_no_cross_recording(self, graph_store: Any) -> None:
        """Entities in single recording → cross_recording_entities = 0."""
        extractions = [
            _make_extraction(1, 1, entities=[("A", "车型", "d"), ("B", "坐席", "d")]),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        assert snapshot.cross_recording_entities == 0


@pytest.mark.unit
class TestNodeDegree:
    """Node degree computation."""

    async def test_degree_count(self, graph_store: Any) -> None:
        """Node degree = in + out edges."""
        extractions = [
            _make_extraction(
                1,
                1,
                entities=[("A", "坐席", "d"), ("B", "车型", "d"), ("C", "竞品", "d")],
                relations=[
                    ("A", "推荐", "B", "d", "EXTRACTED"),
                    ("A", "对比", "C", "d", "EXTRACTED"),
                ],
            ),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        node_a = next(n for n in snapshot.nodes if n.entity_id == "A")
        assert node_a.degree == 2  # 2 outgoing edges

        node_b = next(n for n in snapshot.nodes if n.entity_id == "B")
        assert node_b.degree == 1  # 1 incoming edge


@pytest.mark.unit
class TestGraphBuilderEdgeCases:
    """Edge cases for GraphBuilder."""

    async def test_empty_extractions(self, graph_store: Any) -> None:
        """Empty extractions → empty snapshot."""
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions([])

        assert snapshot.total_entities == 0
        assert snapshot.total_relations == 0
        assert snapshot.nodes == []
        assert snapshot.edges == []

    async def test_graphml_persisted(self, graph_store: Any) -> None:
        """Graph is persisted to GraphML after build."""
        extractions = [
            _make_extraction(
                1,
                1,
                entities=[("A", "坐席", "d"), ("B", "车型", "d")],
                relations=[("A", "推荐", "B", "d", "EXTRACTED")],
            ),
        ]
        builder = GraphBuilder(graph_store)
        await builder.build_from_extractions(extractions)

        assert await graph_store.has_graph()

    async def test_round_trip_via_graph_store(self, graph_store: Any) -> None:
        """Nodes and edges survive save → load round-trip."""
        extractions = [
            _make_extraction(
                1,
                1,
                entities=[("A", "坐席", "desc A"), ("B", "车型", "desc B")],
                relations=[("A", "推荐", "B", "rec", "EXTRACTED")],
            ),
        ]
        builder = GraphBuilder(graph_store)
        await builder.build_from_extractions(extractions)

        # Reload from disk
        await graph_store.load()
        node_a = await graph_store.get_node("A")
        assert node_a is not None
        assert node_a.type == "坐席"

        edges = await graph_store.get_edges("A")
        assert len(edges) == 1
        assert edges[0].relation == "推荐"
        assert edges[0].confidence == "EXTRACTED"
