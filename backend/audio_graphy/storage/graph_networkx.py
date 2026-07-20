"""NetworkX knowledge graph store — MultiDiGraph CRUD + GraphML persistence.

Each tenant has one GraphML file: ``working_dir/{tenant_id}/graph_chunk_entity_relation.graphml``.

Uses ``nx.MultiDiGraph`` so that the same entity pair can have multiple
relation types (e.g. ``(客户)-[询问]→(CS75 Plus)`` and ``(客户)-[对比]→(CS75 Plus)``).
Edge key = relation string.

GraphML serialisation: list attributes (source_ids, recording_ids) are stored
as JSON strings because GraphML does not support native list types.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, cast

import networkx as nx

from audio_graphy.adapters.protocols import EdgeConfidence
from audio_graphy.core.types import (
    GraphEdge,
    GraphNode,
    _list_to_str,
    _str_to_list,
    upgrade_confidence,
)

logger = logging.getLogger(__name__)

GRAPHML_FILENAME = "graph_chunk_entity_relation.graphml"


def cast_edge_confidence(value: str) -> EdgeConfidence:
    """Safely cast a string to EdgeConfidence, defaulting to AMBIGUOUS."""
    if value in ("EXTRACTED", "INFERRED", "AMBIGUOUS"):
        return cast(EdgeConfidence, value)
    return "AMBIGUOUS"


class NetworkXGraphStore:
    """NetworkX MultiDiGraph knowledge graph with GraphML file persistence.

    Args:
        working_dir: Root working_dir path.
        tenant_id: Tenant ID (determines sub-directory + GraphML file path).
    """

    def __init__(self, working_dir: Path, *, tenant_id: str = "default") -> None:
        self._working_dir = Path(working_dir)
        self._tenant_id = tenant_id
        self._graph: nx.MultiDiGraph = nx.MultiDiGraph()
        self._loaded = False

    @property
    def graphml_path(self) -> Path:
        """Full path to the tenant's GraphML file."""
        return self._working_dir / self._tenant_id / GRAPHML_FILENAME

    @property
    def graph(self) -> nx.MultiDiGraph:
        """Direct access to the underlying NetworkX graph (for advanced queries)."""
        return self._graph

    # ------------------------------------------------------------------
    # Node CRUD
    # ------------------------------------------------------------------

    async def upsert_node(self, node: GraphNode) -> None:
        """Insert or update a node in the graph.

        If a node with the same entity_id already exists, its attributes are
        merged: source_ids and recording_ids are extended, degree is updated.

        Args:
            node: GraphNode to upsert.
        """
        await self._ensure_loaded()
        if self._graph.has_node(node.entity_id):
            existing = self._graph.nodes[node.entity_id]
            # Merge source_ids (union, preserve order)
            existing_ids = set(_str_to_list(existing.get("source_ids", "[]")))
            new_ids = existing_ids | set(node.source_ids)
            existing["source_ids"] = _list_to_str(list(new_ids))

            # Merge recording_ids (union)
            existing_rec = set(_str_to_list(existing.get("recording_ids", "[]")))
            new_rec = existing_rec | set(node.recording_ids)
            existing["recording_ids"] = _list_to_str(list(new_rec))

            # Update other attributes (take the new values)
            existing["name"] = node.name
            existing["type"] = node.type
            existing["description"] = node.description
            existing["degree"] = node.degree
        else:
            self._graph.add_node(
                node.entity_id,
                name=node.name,
                type=node.type,
                description=node.description,
                source_ids=_list_to_str(node.source_ids),
                recording_ids=_list_to_str(node.recording_ids),
                degree=node.degree,
            )

    async def get_node(self, entity_id: str) -> GraphNode | None:
        """Retrieve a node by entity_id.

        Args:
            entity_id: The normalised entity name (node key).

        Returns:
            GraphNode if found, None otherwise.
        """
        await self._ensure_loaded()
        if not self._graph.has_node(entity_id):
            return None
        attrs = self._graph.nodes[entity_id]
        return self._attrs_to_node(entity_id, attrs)

    async def get_all_nodes(self) -> list[GraphNode]:
        """Return all nodes in the graph.

        Returns:
            List of all GraphNode objects.
        """
        await self._ensure_loaded()
        return [
            self._attrs_to_node(node_id, attrs) for node_id, attrs in self._graph.nodes(data=True)
        ]

    # ------------------------------------------------------------------
    # Edge CRUD
    # ------------------------------------------------------------------

    async def upsert_edge(self, edge: GraphEdge) -> None:
        """Insert or update an edge in the graph.

        If an edge with the same (source, target, relation) already exists,
        its weight is accumulated and source_ids are extended. Confidence is
        upgraded per the EXTRACTED > INFERRED > AMBIGUOUS rule.

        Args:
            edge: GraphEdge to upsert.
        """
        await self._ensure_loaded()
        # Ensure nodes exist
        for node_id in (edge.source, edge.target):
            if not self._graph.has_node(node_id):
                self._graph.add_node(
                    node_id,
                    name=node_id,
                    type="未知",
                    description="",
                    source_ids="[]",
                    recording_ids="[]",
                    degree=0,
                )

        if self._graph.has_edge(edge.source, edge.target, key=edge.relation):
            existing = self._graph[edge.source][edge.target][edge.relation]
            existing["weight"] = float(existing.get("weight", 0.0)) + edge.weight
            # Merge source_ids
            existing_ids = set(_str_to_list(existing.get("source_ids", "[]")))
            existing_ids.update(edge.source_ids)
            existing["source_ids"] = _list_to_str(list(existing_ids))
            # Upgrade confidence

            old_conf = existing.get("confidence", "AMBIGUOUS")
            new_conf = upgrade_confidence(old_conf, edge.confidence)
            existing["confidence"] = new_conf
            # Recompute confidence_score
            existing["confidence_score"] = self._compute_score(new_conf, existing["weight"])
        else:
            self._graph.add_edge(
                edge.source,
                edge.target,
                key=edge.relation,
                relation=edge.relation,
                weight=edge.weight,
                confidence=edge.confidence,
                confidence_score=edge.confidence_score,
                source_ids=_list_to_str(edge.source_ids),
            )

    async def get_edges(self, entity_id: str) -> list[GraphEdge]:
        """Get all edges connected to an entity (both as source and target).

        Args:
            entity_id: The entity to look up.

        Returns:
            List of GraphEdge objects.
        """
        await self._ensure_loaded()
        edges: list[GraphEdge] = []
        # Outgoing edges
        for source, target, key, attrs in self._graph.out_edges(entity_id, data=True, keys=True):
            edges.append(self._attrs_to_edge(source, target, key, attrs))
        # Incoming edges
        for source, target, key, attrs in self._graph.in_edges(entity_id, data=True, keys=True):
            edge = self._attrs_to_edge(source, target, key, attrs)
            if edge not in edges:
                edges.append(edge)
        return edges

    # ------------------------------------------------------------------
    # Graph queries
    # ------------------------------------------------------------------

    async def get_neighbors(self, entity_id: str, *, max_hops: int = 1) -> list[GraphNode]:
        """Get neighbor nodes within ``max_hops`` of the given entity.

        Args:
            entity_id: Starting entity.
            max_hops: Maximum hop distance (default 1 = direct neighbors).

        Returns:
            List of neighbor GraphNode objects (excluding the start node).
        """
        await self._ensure_loaded()
        if not self._graph.has_node(entity_id):
            return []

        if max_hops <= 0:
            return []

        # Use BFS to find neighbors within max_hops
        visited: set[str] = {entity_id}
        frontier: set[str] = {entity_id}

        for _hop in range(max_hops):
            next_frontier: set[str] = set()
            for node in frontier:
                # Outgoing neighbors
                for target in self._graph.successors(node):
                    if target not in visited:
                        next_frontier.add(target)
                # Incoming neighbors
                for source in self._graph.predecessors(node):
                    if source not in visited:
                        next_frontier.add(source)
            visited.update(next_frontier)
            frontier = next_frontier

        visited.discard(entity_id)
        return [
            self._attrs_to_node(nid, self._graph.nodes[nid])
            for nid in visited
            if self._graph.has_node(nid)
        ]

    async def get_relation_counts(self, entity_id: str) -> dict[str, int]:
        """Count edges by relation type for an entity.

        Used by the graph retrieval channel to rank neighbors by
        graph-structure signal (not pure vector similarity).

        Args:
            entity_id: The entity to analyse.

        Returns:
            Dict mapping relation type → count.
        """
        await self._ensure_loaded()
        counts: dict[str, int] = {}
        for _, _, key in self._graph.out_edges(entity_id, keys=True):
            counts[key] = counts.get(key, 0) + 1
        for _, _, key in self._graph.in_edges(entity_id, keys=True):
            counts[key] = counts.get(key, 0) + 1
        return counts

    async def get_node_degree(self, entity_id: str) -> int:
        """Get the degree (total in + out edges) of a node.

        Args:
            entity_id: The entity to look up.

        Returns:
            Total degree, or 0 if node doesn't exist.
        """
        await self._ensure_loaded()
        if not self._graph.has_node(entity_id):
            return 0
        return int(self._graph.degree(entity_id))

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def save(self) -> None:
        """Flush the in-memory graph to GraphML file.

        Raises:
            StorageError: If write fails.
        """
        from audio_graphy.core.types import StorageError

        self.graphml_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(self._sync_save)
        except Exception as exc:
            raise StorageError(f"Failed to save GraphML to {self.graphml_path}: {exc}") from exc
        logger.debug(
            "Saved graph (%d nodes, %d edges) to %s",
            self._graph.number_of_nodes(),
            self._graph.number_of_edges(),
            self.graphml_path,
        )

    def _sync_save(self) -> None:
        """Synchronous GraphML write — called via asyncio.to_thread."""
        nx.write_graphml(self._graph, self.graphml_path, encoding="utf-8")

    async def load(self) -> None:
        """Load the graph from GraphML file into memory.

        If the file doesn't exist or is corrupted, initialises an empty graph
        (per PRD §4.7 error handling).
        """
        await asyncio.to_thread(self._sync_load)
        self._loaded = True

    def _sync_load(self) -> None:
        """Synchronous GraphML read — called via asyncio.to_thread."""
        if not self.graphml_path.exists():
            self._graph = nx.MultiDiGraph()
            return
        try:
            loaded = nx.read_graphml(self.graphml_path)
            # read_graphml returns DiGraph or MultiDiGraph depending on edge keys
            if not isinstance(loaded, nx.MultiDiGraph):
                # Convert DiGraph to MultiDiGraph
                multi = nx.MultiDiGraph()
                multi.add_nodes_from(loaded.nodes(data=True))
                for u, v, data in loaded.edges(data=True):
                    key = data.get("relation", "relation")
                    multi.add_edge(u, v, key=key, **data)
                self._graph = multi
            else:
                self._graph = loaded
        except Exception as exc:
            logger.warning(
                "Corrupted GraphML %s, initialising empty graph: %s", self.graphml_path, exc
            )
            self._graph = nx.MultiDiGraph()

    async def has_graph(self) -> bool:
        """Check whether the GraphML file exists and is non-empty.

        Returns:
            True if a valid graph file exists.
        """
        return self.graphml_path.exists() and self.graphml_path.stat().st_size > 0

    # ------------------------------------------------------------------
    # Attribute conversion helpers
    # ------------------------------------------------------------------

    def _attrs_to_node(self, node_id: str, attrs: dict[str, Any]) -> GraphNode:
        """Convert NetworkX node attributes to a GraphNode dataclass."""
        source_ids = _str_to_list(attrs.get("source_ids", "[]"))
        recording_ids_raw = _str_to_list(attrs.get("recording_ids", "[]"))
        recording_ids = [int(r) for r in recording_ids_raw if r is not None]

        return GraphNode(
            entity_id=str(node_id),
            name=attrs.get("name", str(node_id)),
            type=attrs.get("type", "未知"),
            description=attrs.get("description", ""),
            source_ids=[str(s) for s in source_ids],
            recording_ids=recording_ids,
            degree=int(attrs.get("degree", 0)),
        )

    def _attrs_to_edge(
        self, source: str, target: str, key: str, attrs: dict[str, Any]
    ) -> GraphEdge:
        """Convert NetworkX edge attributes to a GraphEdge dataclass."""
        confidence_str = attrs.get("confidence", "AMBIGUOUS")
        # Ensure valid EdgeConfidence value
        if confidence_str not in ("EXTRACTED", "INFERRED", "AMBIGUOUS"):
            confidence_str = "AMBIGUOUS"
        confidence = cast_edge_confidence(confidence_str)

        score = attrs.get("confidence_score")
        if score is not None:
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = None

        source_ids = _str_to_list(attrs.get("source_ids", "[]"))

        return GraphEdge(
            source=str(source),
            target=str(target),
            relation=str(key),
            weight=float(attrs.get("weight", 1.0)),
            confidence=confidence,
            confidence_score=score,
            source_ids=[str(s) for s in source_ids],
        )

    @staticmethod
    def _compute_score(confidence: str, weight: float) -> float | None:
        """Compute confidence_score from confidence tag and weight."""
        if confidence == "EXTRACTED":
            return 1.0
        if confidence == "INFERRED":
            return round(weight / (weight + 1.0), 4) if weight > 0 else 0.5
        return None

    async def _ensure_loaded(self) -> None:
        """Lazy-load from disk on first access."""
        if not self._loaded:
            await self.load()


__all__ = ["NetworkXGraphStore"]
