"""Bounded graph-explorer projection tests."""

from __future__ import annotations

import networkx as nx
import pytest

from audio_graphy.services.graph_explorer import (
    ABSOLUTE_GRAPH_EDGE_RENDER_BUDGET,
    collect_bounded_induced_edges,
)


@pytest.mark.unit
def test_collect_bounded_induced_edges_counts_without_over_serializing() -> None:
    graph = nx.MultiDiGraph()
    graph.add_nodes_from(["a", "b", "c"])
    for index in range(12):
        graph.add_edge("a", "b", key=f"rel-{index}", weight=1.0)
    graph.add_edge("a", "c", key="outside-visible-set", weight=1.0)

    result = collect_bounded_induced_edges(
        graph,
        {"a", "b"},
        requested_budget=4,
        configured_budget=10,
    )

    assert result.total == 12
    assert result.returned == 4
    assert result.truncated is True
    assert result.render_budget == 4
    assert len(result.edges) == 4
    assert all(
        source in {"a", "b"} and target in {"a", "b"} for source, target, _, _ in result.edges
    )


@pytest.mark.unit
def test_collect_bounded_induced_edges_never_exceeds_absolute_cap() -> None:
    graph = nx.MultiDiGraph()
    graph.add_nodes_from(["a", "b"])
    for index in range(ABSOLUTE_GRAPH_EDGE_RENDER_BUDGET + 2):
        graph.add_edge("a", "b", key=f"rel-{index}")

    result = collect_bounded_induced_edges(
        graph,
        {"a", "b"},
        requested_budget=ABSOLUTE_GRAPH_EDGE_RENDER_BUDGET,
        configured_budget=ABSOLUTE_GRAPH_EDGE_RENDER_BUDGET,
    )

    assert result.returned == ABSOLUTE_GRAPH_EDGE_RENDER_BUDGET
    assert result.total == ABSOLUTE_GRAPH_EDGE_RENDER_BUDGET + 2
    assert result.truncated is True


@pytest.mark.unit
def test_configured_budget_can_only_lower_a_larger_request() -> None:
    graph = nx.MultiDiGraph()
    graph.add_nodes_from(["a", "b"])
    for index in range(5):
        graph.add_edge("a", "b", key=f"rel-{index}")

    result = collect_bounded_induced_edges(
        graph,
        {"a", "b"},
        requested_budget=5_000,
        configured_budget=2,
    )

    assert result.render_budget == 2
    assert result.returned == 2
    assert result.total == 5
    assert result.truncated is True
