"""Graph router — explore / entity / subgraph / path.

See: docs/m3-prd.md §4.5.
"""

from __future__ import annotations

import logging
from typing import Any

import networkx as nx
from fastapi import APIRouter, Depends, Query, Request

from audio_graphy.api.deps import get_graph_store
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_any_authenticated
from audio_graphy.errors import EntityNotFoundError, PathNotFoundError
from audio_graphy.schemas.graph import (
    EntityDetailResponse,
    ExploreResponse,
    GraphEdgeResponse,
    GraphNodeResponse,
    NeighborResponse,
    PathResponse,
)
from audio_graphy.storage.graph_networkx import NetworkXGraphStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/graph", tags=["graph"])


def _node_to_response(node_id: str, attrs: dict[str, Any]) -> GraphNodeResponse:
    """Convert NetworkX node attrs to response schema."""
    from audio_graphy.core.types import _str_to_list

    source_ids = [str(s) for s in _str_to_list(attrs.get("source_ids", "[]"))]
    recording_ids_raw = _str_to_list(attrs.get("recording_ids", "[]"))
    recording_ids = [int(r) for r in recording_ids_raw if r is not None]

    return GraphNodeResponse(
        id=str(node_id),
        label=attrs.get("name", str(node_id)),
        type=attrs.get("type", "未知"),
        description=attrs.get("description", ""),
        degree=int(attrs.get("degree", 0)),
        source_ids=source_ids,
        recording_ids=recording_ids,
    )


def _edge_to_response(
    source: str, target: str, key: str, attrs: dict[str, Any]
) -> GraphEdgeResponse:
    """Convert NetworkX edge attrs to response schema."""
    from audio_graphy.core.types import _str_to_list

    confidence = attrs.get("confidence", "AMBIGUOUS")
    if confidence not in ("EXTRACTED", "INFERRED", "AMBIGUOUS"):
        confidence = "AMBIGUOUS"

    score = attrs.get("confidence_score")
    if score is not None:
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = None

    source_ids = [str(s) for s in _str_to_list(attrs.get("source_ids", "[]"))]

    return GraphEdgeResponse(
        source=str(source),
        target=str(target),
        relation=str(key),
        weight=float(attrs.get("weight", 1.0)),
        confidence=confidence,
        confidence_score=score,
        source_ids=source_ids,
    )


@router.get("/explore", response_model=ExploreResponse, summary="Browse full graph")
async def explore(
    request: Request,
    node_type: str | None = Query(default=None),
    min_degree: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=2000),
    graph_store: NetworkXGraphStore = Depends(get_graph_store),
    _user: AuthUser = Depends(require_any_authenticated()),
) -> ExploreResponse:
    """Browse the full knowledge graph (nodes + edges)."""
    await graph_store._ensure_loaded()
    g = graph_store.graph

    # Collect nodes
    node_data = []
    for node_id, attrs in g.nodes(data=True):
        if node_type is not None and attrs.get("type") != node_type:
            continue
        degree = int(attrs.get("degree", g.degree(node_id)))
        if degree < min_degree:
            continue
        node_data.append((node_id, attrs))

    # Apply limit
    node_data = node_data[:limit]
    node_ids = {n_id for n_id, _ in node_data}

    nodes = [_node_to_response(n_id, attrs) for n_id, attrs in node_data]

    # Collect edges between visible nodes
    edges = []
    for source, target, key, attrs in g.edges(data=True, keys=True):
        if source in node_ids or target in node_ids:
            edges.append(_edge_to_response(source, target, key, attrs))

    return ExploreResponse(
        nodes=nodes,
        edges=edges,
        total_nodes=g.number_of_nodes(),
        total_edges=g.number_of_edges(),
    )


@router.get("/entity/{name}", response_model=EntityDetailResponse, summary="Get entity detail")
async def get_entity(
    name: str,
    request: Request,
    graph_store: NetworkXGraphStore = Depends(get_graph_store),
    _user: AuthUser = Depends(require_any_authenticated()),
) -> EntityDetailResponse:
    """Get entity details with 1-hop neighbors and relation counts."""
    node = await graph_store.get_node(name)
    if node is None:
        raise EntityNotFoundError(detail={"entity_name": name})

    neighbors_data = await graph_store.get_neighbors(name, max_hops=1)
    edges = await graph_store.get_edges(name)
    relation_counts = await graph_store.get_relation_counts(name)

    # Build neighbor list with edge info
    neighbors: list[NeighborResponse] = []
    neighbor_ids = {n.entity_id for n in neighbors_data}
    for edge in edges:
        other_id = edge.target if edge.source == name else edge.source
        if other_id in neighbor_ids:
            neighbor_node = await graph_store.get_node(other_id)
            if neighbor_node is not None:
                neighbors.append(
                    NeighborResponse(
                        id=neighbor_node.entity_id,
                        label=neighbor_node.name,
                        type=neighbor_node.type,
                        relation=edge.relation,
                        weight=edge.weight,
                        confidence=edge.confidence,
                    )
                )

    node_resp = GraphNodeResponse(
        id=node.entity_id,
        label=node.name,
        type=node.type,
        description=node.description,
        degree=node.degree,
        source_ids=node.source_ids,
        recording_ids=node.recording_ids,
    )

    return EntityDetailResponse(
        node=node_resp,
        neighbors=neighbors,
        relation_counts=relation_counts,
    )


@router.get("/subgraph", response_model=ExploreResponse, summary="Get N-hop subgraph")
async def get_subgraph(
    request: Request,
    entity: str = Query(description="Center entity name"),
    max_hops: int = Query(default=1, ge=1, le=3),
    limit: int = Query(default=50, ge=1, le=500),
    graph_store: NetworkXGraphStore = Depends(get_graph_store),
    _user: AuthUser = Depends(require_any_authenticated()),
) -> ExploreResponse:
    """Extract an N-hop subgraph centered on an entity."""
    await graph_store._ensure_loaded()
    g = graph_store.graph

    if not g.has_node(entity):
        raise EntityNotFoundError(detail={"entity_name": entity})

    # BFS to find nodes within max_hops
    visited: set[str] = {entity}
    frontier: set[str] = {entity}

    for _ in range(max_hops):
        next_frontier: set[str] = set()
        for node in frontier:
            for target in g.successors(node):
                if target not in visited:
                    next_frontier.add(target)
            for source in g.predecessors(node):
                if source not in visited:
                    next_frontier.add(source)
        visited.update(next_frontier)
        frontier = next_frontier

    # Apply limit
    node_list = list(visited)[:limit]
    node_set = set(node_list)

    nodes = []
    for node_id in node_list:
        if g.has_node(node_id):
            nodes.append(_node_to_response(node_id, g.nodes[node_id]))

    # Collect edges within the subgraph
    edges = []
    for source, target, key, attrs in g.edges(data=True, keys=True):
        if source in node_set and target in node_set:
            edges.append(_edge_to_response(source, target, key, attrs))

    return ExploreResponse(
        nodes=nodes,
        edges=edges,
        total_nodes=len(nodes),
        total_edges=len(edges),
    )


@router.get("/path", response_model=PathResponse, summary="Shortest path between entities")
async def get_path(
    request: Request,
    source: str = Query(description="Source entity name"),
    target: str = Query(description="Target entity name"),
    graph_store: NetworkXGraphStore = Depends(get_graph_store),
    _user: AuthUser = Depends(require_any_authenticated()),
) -> PathResponse:
    """Find the shortest path between two entities."""
    await graph_store._ensure_loaded()
    g = graph_store.graph

    if not g.has_node(source):
        raise EntityNotFoundError(detail={"entity_name": source})
    if not g.has_node(target):
        raise EntityNotFoundError(detail={"entity_name": target})

    try:
        # Convert MultiDiGraph to undirected for path finding
        undirected = nx.Graph()
        for u, v, data in g.edges(data=True):
            if not undirected.has_edge(u, v):
                undirected.add_edge(u, v, **data)

        path = nx.shortest_path(undirected, source=source, target=target)
    except nx.NetworkXNoPath:
        raise PathNotFoundError(detail={"source": source, "target": target}) from None
    except nx.NodeNotFound:
        raise PathNotFoundError(detail={"source": source, "target": target}) from None

    # Collect edges along the path
    edges: list[GraphEdgeResponse] = []
    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        if g.has_edge(u, v):
            for key, attrs in g[u][v].items():
                edges.append(_edge_to_response(u, v, key, attrs))
        elif g.has_edge(v, u):
            for key, attrs in g[v][u].items():
                edges.append(_edge_to_response(v, u, key, attrs))

    return PathResponse(
        path=path,
        length=len(path) - 1,
        edges=edges,
    )
