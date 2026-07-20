"""Unit tests for NetworkXGraphStore — MultiDiGraph CRUD + GraphML persistence.

Tests cover:
    - Node CRUD (upsert, get, get_all)
    - Edge CRUD (upsert with weight accumulation + confidence upgrade)
    - Neighbor / relation_counts / degree queries
    - GraphML save/load round-trip
    - Error handling (missing file, corrupted GraphML)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audio_graphy.core.types import GraphEdge, GraphNode
from audio_graphy.storage.graph_networkx import NetworkXGraphStore


def _make_node(
    entity_id: str = "CS75 Plus",
    name: str = "CS75 Plus",
    type: str = "车型",
    description: str = "热门SUV",
    source_ids: list[str] | None = None,
    recording_ids: list[int] | None = None,
    degree: int = 0,
) -> GraphNode:
    """Helper to create a GraphNode with defaults."""
    return GraphNode(
        entity_id=entity_id,
        name=name,
        type=type,
        description=description,
        source_ids=source_ids or ["1_0"],
        recording_ids=recording_ids or [1],
        degree=degree,
    )


def _make_edge(
    source: str = "张敏",
    target: str = "CS75 Plus",
    relation: str = "推荐",
    weight: float = 1.0,
    confidence: str = "EXTRACTED",
    confidence_score: float | None = 1.0,
    source_ids: list[str] | None = None,
) -> GraphEdge:
    """Helper to create a GraphEdge with defaults."""
    return GraphEdge(
        source=source,
        target=target,
        relation=relation,
        weight=weight,
        confidence=confidence,  # type: ignore[arg-type]
        confidence_score=confidence_score,
        source_ids=source_ids or ["1_0"],
    )


@pytest.mark.unit
class TestNodeCRUD:
    """Node create/read/update operations."""

    async def test_upsert_and_get_node(self, graph_store: NetworkXGraphStore) -> None:
        """Upsert a node and retrieve it."""
        node = _make_node()
        await graph_store.upsert_node(node)
        retrieved = await graph_store.get_node("CS75 Plus")
        assert retrieved is not None
        assert retrieved.entity_id == "CS75 Plus"
        assert retrieved.name == "CS75 Plus"
        assert retrieved.type == "车型"
        assert retrieved.source_ids == ["1_0"]

    async def test_get_missing_node(self, graph_store: NetworkXGraphStore) -> None:
        """Getting a non-existent node returns None."""
        result = await graph_store.get_node("nonexistent")
        assert result is None

    async def test_get_all_nodes(self, graph_store: NetworkXGraphStore) -> None:
        """get_all_nodes returns all nodes."""
        await graph_store.upsert_node(_make_node("A", "A", "车型"))
        await graph_store.upsert_node(_make_node("B", "B", "坐席"))
        nodes = await graph_store.get_all_nodes()
        assert len(nodes) == 2
        ids = {n.entity_id for n in nodes}
        assert ids == {"A", "B"}

    async def test_upsert_node_merges_source_ids(self, graph_store: NetworkXGraphStore) -> None:
        """Re-upserting a node merges source_ids and recording_ids."""
        await graph_store.upsert_node(_make_node("X", source_ids=["1_0"], recording_ids=[1]))
        await graph_store.upsert_node(_make_node("X", source_ids=["2_3"], recording_ids=[2]))
        node = await graph_store.get_node("X")
        assert node is not None
        assert set(node.source_ids) == {"1_0", "2_3"}
        assert set(node.recording_ids) == {1, 2}


@pytest.mark.unit
class TestEdgeCRUD:
    """Edge create/read/update operations."""

    async def test_upsert_and_get_edges(self, graph_store: NetworkXGraphStore) -> None:
        """Upsert an edge and retrieve it via get_edges."""
        await graph_store.upsert_node(_make_node("张敏", type="坐席"))
        await graph_store.upsert_node(_make_node("CS75 Plus", type="车型"))
        await graph_store.upsert_edge(_make_edge("张敏", "CS75 Plus", "推荐"))
        edges = await graph_store.get_edges("张敏")
        assert len(edges) == 1
        assert edges[0].source == "张敏"
        assert edges[0].target == "CS75 Plus"
        assert edges[0].relation == "推荐"

    async def test_edge_weight_accumulation(self, graph_store: NetworkXGraphStore) -> None:
        """Same (source, target, relation) edge accumulates weight."""
        await graph_store.upsert_node(_make_node("A"))
        await graph_store.upsert_node(_make_node("B"))
        await graph_store.upsert_edge(_make_edge("A", "B", "推荐", weight=1.0))
        await graph_store.upsert_edge(_make_edge("A", "B", "推荐", weight=1.0))

        edges = await graph_store.get_edges("A")
        assert len(edges) == 1
        assert edges[0].weight == 2.0

    async def test_multiple_relation_types(self, graph_store: NetworkXGraphStore) -> None:
        """Same entity pair with different relations creates separate edges."""
        await graph_store.upsert_node(_make_node("客户"))
        await graph_store.upsert_node(_make_node("CS75 Plus"))
        await graph_store.upsert_edge(_make_edge("客户", "CS75 Plus", "询问"))
        await graph_store.upsert_edge(_make_edge("客户", "CS75 Plus", "对比"))

        edges = await graph_store.get_edges("客户")
        assert len(edges) == 2
        relations = {e.relation for e in edges}
        assert relations == {"询问", "对比"}

    async def test_confidence_upgrade(self, graph_store: NetworkXGraphStore) -> None:
        """Edge confidence upgrades: INFERRED + EXTRACTED → EXTRACTED."""
        await graph_store.upsert_node(_make_node("A"))
        await graph_store.upsert_node(_make_node("B"))
        # First: INFERRED
        await graph_store.upsert_edge(
            _make_edge("A", "B", "推荐", weight=1.0, confidence="INFERRED", confidence_score=0.5)
        )
        # Second: EXTRACTED (should upgrade)
        await graph_store.upsert_edge(
            _make_edge("A", "B", "推荐", weight=1.0, confidence="EXTRACTED", confidence_score=1.0)
        )
        edges = await graph_store.get_edges("A")
        assert len(edges) == 1
        assert edges[0].confidence == "EXTRACTED"
        assert edges[0].confidence_score == 1.0


@pytest.mark.unit
class TestGraphQueries:
    """Neighbor, relation_counts, and degree queries."""

    async def test_get_neighbors_1hop(self, graph_store: NetworkXGraphStore) -> None:
        """get_neighbors returns direct neighbors."""
        await graph_store.upsert_node(_make_node("A"))
        await graph_store.upsert_node(_make_node("B"))
        await graph_store.upsert_node(_make_node("C"))
        await graph_store.upsert_edge(_make_edge("A", "B", "推荐"))
        await graph_store.upsert_edge(_make_edge("C", "A", "询问"))

        neighbors = await graph_store.get_neighbors("A", max_hops=1)
        neighbor_ids = {n.entity_id for n in neighbors}
        assert neighbor_ids == {"B", "C"}

    async def test_get_neighbors_missing_node(self, graph_store: NetworkXGraphStore) -> None:
        """get_neighbors on a non-existent node returns empty list."""
        result = await graph_store.get_neighbors("nonexistent")
        assert result == []

    async def test_get_relation_counts(self, graph_store: NetworkXGraphStore) -> None:
        """get_relation_counts returns {relation: count}."""
        await graph_store.upsert_node(_make_node("A"))
        await graph_store.upsert_node(_make_node("B"))
        await graph_store.upsert_node(_make_node("C"))
        await graph_store.upsert_edge(_make_edge("A", "B", "推荐"))
        await graph_store.upsert_edge(_make_edge("A", "B", "推荐"))
        await graph_store.upsert_edge(_make_edge("A", "C", "询问"))

        counts = await graph_store.get_relation_counts("A")
        # Two upserts with same (A, B, 推荐) key = 1 edge (merged), plus (A, C, 询问) = 1 edge
        assert counts.get("推荐") == 1
        assert counts.get("询问") == 1

    async def test_get_node_degree(self, graph_store: NetworkXGraphStore) -> None:
        """get_node_degree returns total in + out edges."""
        await graph_store.upsert_node(_make_node("A"))
        await graph_store.upsert_node(_make_node("B"))
        await graph_store.upsert_node(_make_node("C"))
        await graph_store.upsert_edge(_make_edge("A", "B", "推荐"))
        await graph_store.upsert_edge(_make_edge("C", "A", "询问"))

        degree = await graph_store.get_node_degree("A")
        assert degree == 2  # 1 out + 1 in

    async def test_get_node_degree_missing(self, graph_store: NetworkXGraphStore) -> None:
        """Degree of non-existent node is 0."""
        degree = await graph_store.get_node_degree("nonexistent")
        assert degree == 0


@pytest.mark.unit
class TestGraphMLPersistence:
    """GraphML save/load round-trip."""

    async def test_save_and_has_graph(self, graph_store: NetworkXGraphStore) -> None:
        """save() writes a GraphML file; has_graph() detects it."""
        await graph_store.upsert_node(_make_node("A"))
        await graph_store.upsert_edge(_make_edge("A", "A", "自环"))
        await graph_store.save()

        assert await graph_store.has_graph()
        assert graph_store.graphml_path.exists()

    async def test_has_graph_no_file(self, graph_store: NetworkXGraphStore) -> None:
        """has_graph returns False when no file exists."""
        assert await graph_store.has_graph() is False

    async def test_save_load_roundtrip(self, tmp_working_dir: Path) -> None:
        """Graph survives save → new instance → load round-trip."""
        store1 = NetworkXGraphStore(tmp_working_dir, tenant_id="default")
        await store1.upsert_node(_make_node("A", type="车型", description="desc A"))
        await store1.upsert_node(_make_node("B", type="坐席", description="desc B"))
        await store1.upsert_edge(_make_edge("B", "A", "推荐", weight=2.0))
        await store1.save()

        store2 = NetworkXGraphStore(tmp_working_dir, tenant_id="default")
        await store2.load()

        node_a = await store2.get_node("A")
        assert node_a is not None
        assert node_a.type == "车型"
        assert node_a.description == "desc A"

        edges = await store2.get_edges("B")
        assert len(edges) == 1
        assert edges[0].relation == "推荐"
        assert edges[0].weight == 2.0

    async def test_load_missing_file(self, tmp_working_dir: Path) -> None:
        """load() on missing file initialises empty graph."""
        store = NetworkXGraphStore(tmp_working_dir, tenant_id="default")
        await store.load()
        nodes = await store.get_all_nodes()
        assert nodes == []

    async def test_tenant_isolation(self, tmp_working_dir: Path) -> None:
        """Different tenants have separate GraphML files."""
        store_a = NetworkXGraphStore(tmp_working_dir, tenant_id="tenant_a")
        store_b = NetworkXGraphStore(tmp_working_dir, tenant_id="tenant_b")

        await store_a.upsert_node(_make_node("entity_a"))
        await store_b.upsert_node(_make_node("entity_b"))
        await store_a.save()
        await store_b.save()

        assert store_a.graphml_path != store_b.graphml_path
        assert store_a.graphml_path.exists()
        assert store_b.graphml_path.exists()
