"""Bi-temporal NetworkX adapters shared by the API and retention layers.

Everything here is pure graph-attribute manipulation — no FastAPI, no DB
session. It lives in ``storage/`` so ``core/retention.py`` can build a
compression sink without importing the API package at all: ``core`` sits below
``api`` in the layering, and these helpers previously lived in
``api/compression_admin.py``, which meant retention reached upwards for two
private symbols.

Provides:
    - ``edge_from_graph_attrs``: GraphML attr dict → GraphEdge.
    - ``all_graph_nodes``: materialise every live GraphNode in a store.
    - ``GraphCompressionSink``: ``CompressionSink`` adapter over NetworkX.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from audio_graphy.core.types import GraphEdge, GraphNode, _list_to_str, _str_to_list

logger = logging.getLogger(__name__)


def edge_from_graph_attrs(
    source: str,
    target: str,
    relation: str,
    attrs: dict[str, Any],
) -> GraphEdge:
    """Build a GraphEdge from a NetworkX edge attribute dict.

    The graph store persists bi-temporal timestamps as ISO strings inside
    the attribute dict (per architecture §6.6); we deserialise them here.
    """

    def _dt(key: str) -> datetime | None:
        v = attrs.get(key)
        if not v or not isinstance(v, str):
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None

    confidence = attrs.get("confidence", "AMBIGUOUS")
    if confidence not in ("EXTRACTED", "INFERRED", "AMBIGUOUS"):
        confidence = "AMBIGUOUS"

    return GraphEdge(
        source=source,
        target=target,
        relation=relation,
        weight=float(attrs.get("weight", 1.0)),
        confidence=confidence,
        confidence_score=(
            float(attrs["confidence_score"]) if attrs.get("confidence_score") is not None else None
        ),
        source_ids=_str_to_list(attrs.get("source_ids", "[]")),
        valid_at=_dt("valid_at"),
        invalid_at=_dt("invalid_at"),
        created_at=_dt("created_at"),
        expired_at=_dt("expired_at"),
        superseded_by=attrs.get("superseded_by"),
    )


def all_graph_nodes(graph_store: Any) -> list[GraphNode]:
    """Materialise GraphNode objects for every live node in the graph."""
    g = graph_store.graph
    out: list[GraphNode] = []
    for nid, attrs in g.nodes(data=True):
        # Skip already-soft-deleted nodes.
        if attrs.get("expired_at"):
            continue
        out.append(
            GraphNode(
                entity_id=str(nid),
                name=str(attrs.get("name", nid)),
                type=str(attrs.get("type", "")),
                description=str(attrs.get("description", "")),
                source_ids=_str_to_list(attrs.get("source_ids", "[]")),
                recording_ids=[int(x) for x in _str_to_list(attrs.get("recording_ids", "[]"))],
                degree=int(attrs.get("degree", 0)),
                expired_at=None,
            )
        )
    return out


class GraphCompressionSink:
    """Adapter that lets CompressionService mutate a NetworkX graph store.

    Implements the ``CompressionSink`` protocol from core/compression.py:
        - fetch_node / fetch_edges_on_node  → read from NetworkX
        - write_node / write_edge           → mutate NetworkX attrs
        - commit / rollback                 → no-op (graph is in-memory)

    This is the simplest viable production sink. A transactional MySQL
    sink would commit/rollback a DB session; here we keep the graph +
    bi-temporal event buffer in memory.
    """

    def __init__(self, graph_store: Any, tenant_id: str) -> None:
        self._gs = graph_store
        self._tenant_id = tenant_id

    def fetch_node(self, entity_id: str) -> GraphNode | None:
        g = self._gs.graph
        if not g.has_node(entity_id):
            return None
        attrs = g.nodes[entity_id]
        return GraphNode(
            entity_id=entity_id,
            name=str(attrs.get("name", entity_id)),
            type=str(attrs.get("type", "")),
            description=str(attrs.get("description", "")),
            source_ids=_str_to_list(attrs.get("source_ids", "[]")),
            recording_ids=[int(x) for x in _str_to_list(attrs.get("recording_ids", "[]"))],
            degree=int(attrs.get("degree", 0)),
            expired_at=None,  # live nodes only; expired_at tracked in attrs
        )

    def fetch_edges_on_node(self, entity_id: str) -> list[GraphEdge]:
        g = self._gs.graph
        out: list[GraphEdge] = []
        for source, target, attrs in g.edges(data=True):
            if entity_id not in (source, target):
                continue
            rel = attrs.get("relation") or attrs.get("key") or ""
            out.append(edge_from_graph_attrs(source, target, rel, attrs))
        return out

    def write_node(self, node: GraphNode) -> None:
        g = self._gs.graph
        if not g.has_node(node.entity_id):
            g.add_node(node.entity_id)
        attrs = g.nodes[node.entity_id]
        attrs["name"] = node.name
        attrs["type"] = node.type
        attrs["description"] = node.description
        attrs["source_ids"] = _list_to_str(node.source_ids)
        attrs["recording_ids"] = _list_to_str([str(r) for r in node.recording_ids])
        attrs["degree"] = node.degree
        if node.expired_at is not None:
            attrs["expired_at"] = node.expired_at.isoformat()
        else:
            attrs.pop("expired_at", None)

    def write_edge(self, edge: GraphEdge) -> None:
        g = self._gs.graph
        key = edge.relation
        if g.has_edge(edge.source, edge.target, key=key):
            attrs = g[edge.source][edge.target][key]
        else:
            g.add_edge(edge.source, edge.target, key=key)
            attrs = g[edge.source][edge.target][key]
        attrs["relation"] = edge.relation
        attrs["weight"] = edge.weight
        attrs["confidence"] = str(edge.confidence)
        attrs["confidence_score"] = edge.confidence_score
        attrs["source_ids"] = _list_to_str(edge.source_ids)
        if edge.valid_at is not None:
            attrs["valid_at"] = edge.valid_at.isoformat()
        if edge.invalid_at is not None:
            attrs["invalid_at"] = edge.invalid_at.isoformat()
        if edge.created_at is not None:
            attrs["created_at"] = edge.created_at.isoformat()
        if edge.expired_at is not None:
            attrs["expired_at"] = edge.expired_at.isoformat()
        if edge.superseded_by is not None:
            attrs["superseded_by"] = edge.superseded_by

    def commit(self) -> None:
        # No-op: NetworkX mutations are immediate. We rely on the caller
        # to persist via GraphML on the next retention sweep.
        invalidate_projection = getattr(self._gs, "invalidate_path_projection", None)
        if callable(invalidate_projection):
            invalidate_projection()
        return None

    def rollback(self) -> None:
        # Best-effort: no snapshot/restore at this layer.
        invalidate_projection = getattr(self._gs, "invalidate_path_projection", None)
        if callable(invalidate_projection):
            invalidate_projection()
        logger.warning("GraphCompressionSink.rollback() called — no-op for NetworkX sink")


__all__ = ["GraphCompressionSink", "all_graph_nodes", "edge_from_graph_attrs"]
