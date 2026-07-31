"""Bounded projections for graph-explorer API responses.

The graph store may contain a dense multigraph.  These helpers keep induced
edge enumeration linear in the graph view and enforce a hard serialization
budget before response models are allocated.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Any

import networkx as nx

ABSOLUTE_GRAPH_EDGE_RENDER_BUDGET = 5_000

GraphEdgeTuple = tuple[Any, Any, Any, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class BoundedInducedEdges:
    """One explainable, bounded induced-edge projection."""

    edges: list[GraphEdgeTuple]
    total: int
    returned: int
    truncated: bool
    render_budget: int


def collect_bounded_induced_edges(
    graph: nx.MultiDiGraph,
    node_ids: set[Any],
    *,
    requested_budget: int | None,
    configured_budget: int,
) -> BoundedInducedEdges:
    """Collect at most the effective budget of edges induced by ``node_ids``.

    ``MultiDiGraph.subgraph`` returns a view, so this avoids constructing an
    O(V²) candidate-pair matrix.  Counting still reflects the complete induced
    edge set while response materialization is strictly bounded.
    """
    if configured_budget < 1:
        raise ValueError("configured graph edge render budget must be positive")
    if requested_budget is not None and requested_budget < 1:
        raise ValueError("requested graph edge render budget must be positive")

    render_budget = min(
        requested_budget if requested_budget is not None else configured_budget,
        configured_budget,
        ABSOLUTE_GRAPH_EDGE_RENDER_BUDGET,
    )
    induced = graph.subgraph(node_ids)
    total = induced.number_of_edges()
    edges = list(
        islice(
            induced.edges(data=True, keys=True),
            render_budget,
        )
    )
    returned = len(edges)
    return BoundedInducedEdges(
        edges=edges,
        total=total,
        returned=returned,
        truncated=returned < total,
        render_budget=render_budget,
    )
