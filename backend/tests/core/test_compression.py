"""T9 — CompressionService tests (architecture §9, Q3 SOFT-only).

Verifies:
  - Phase 1 scoring: god_node / stale / redundant / low_degree
  - Phase 2 Q3 SOFT-only mutations (nodes get expired_at, edges get invalid_at)
  - Phase 2 NEVER hard-deletes (policy_check refuses forbidden methods)
  - Phase 3 rollback on simulated failure
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from audio_graphy.core.bi_temporal import BiTemporalEdgeService
from audio_graphy.core.compression import (
    CompressionCandidate,
    CompressionReport,
    CompressionService,
    InMemoryCompressionSink,
)
from audio_graphy.core.types import (
    CompressionPolicyViolationError,
    CompressionRollbackError,
    GraphEdge,
    GraphNode,
)

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture()
def bt() -> BiTemporalEdgeService:
    return BiTemporalEdgeService(tenant_id="t1")


@pytest.fixture()
def sink() -> InMemoryCompressionSink:
    return InMemoryCompressionSink()


@pytest.fixture()
def svc(bt: BiTemporalEdgeService, sink: InMemoryCompressionSink) -> CompressionService:
    return CompressionService(
        sink=sink,
        bt_service=bt,
        god_node_degree_threshold=10,
        stale_days=180,
        tenant_id="t1",
    )


def _node(
    eid: str,
    *,
    degree: int = 1,
    description: str = "d",
    recording_ids: list[int] | None = None,
    expired_at: datetime | None = None,
) -> GraphNode:
    return GraphNode(
        entity_id=eid,
        name=eid,
        type="车型",
        description=description,
        source_ids=[],
        recording_ids=recording_ids if recording_ids is not None else [1, 2],
        degree=degree,
        expired_at=expired_at,
    )


def _edge(s: str, t: str) -> GraphEdge:
    return GraphEdge(
        source=s,
        target=t,
        relation="r",
        weight=1.0,
        confidence="EXTRACTED",
        confidence_score=1.0,
    )


# ============================================================
# Phase 1: scoring
# ============================================================


def test_god_node_scored_highest(svc: CompressionService) -> None:
    nodes = [
        _node("Normal", degree=2),
        _node("God", degree=100),  # god_node
        _node("Leaf", degree=0),  # low_degree
    ]
    cands = svc.select_candidates(nodes)
    assert cands[0].entity_id == "God"
    assert cands[0].reason == "god_node"
    assert cands[0].score == 0.9


def test_stale_scored_via_single_recording(svc: CompressionService) -> None:
    """Stale heuristic: only one recording_id → stale."""
    nodes = [_node("Stale", recording_ids=[1])]
    cands = svc.select_candidates(nodes)
    found = [c for c in cands if c.entity_id == "Stale"]
    assert found and found[0].reason == "stale"


def test_redundant_node_no_description(svc: CompressionService) -> None:
    nodes = [_node("Redundant", description="  ")]
    cands = svc.select_candidates(nodes)
    found = [c for c in cands if c.entity_id == "Redundant"]
    assert found and found[0].reason == "redundant"


def test_already_expired_node_skipped(svc: CompressionService) -> None:
    nodes = [_node("Old", expired_at=datetime.now(UTC))]
    cands = svc.select_candidates(nodes)
    assert all(c.entity_id != "Old" for c in cands)


def test_low_degree_leaf_picked_last(svc: CompressionService) -> None:
    nodes = [_node("Leaf", degree=0, recording_ids=[1, 2])]
    cands = svc.select_candidates(nodes)
    found = [c for c in cands if c.entity_id == "Leaf"]
    assert found and found[0].reason == "low_degree"


# ============================================================
# Phase 2: Q3 SOFT-only application
# ============================================================


def test_apply_sets_expired_at_on_nodes(
    svc: CompressionService,
    sink: InMemoryCompressionSink,
) -> None:
    sink.seed(
        nodes=[_node("A"), _node("B")],
        edges=[_edge("A", "B")],
    )
    cands = [CompressionCandidate("A", 0.9, "god_node")]
    report = svc.apply(cands)

    assert report.rolled_back is False
    assert "A" in report.soft_deleted_nodes
    assert sink.nodes["A"].expired_at is not None
    # B untouched.
    assert sink.nodes["B"].expired_at is None


def test_apply_sets_invalid_at_on_incident_edges(
    svc: CompressionService,
    sink: InMemoryCompressionSink,
) -> None:
    sink.seed(
        nodes=[_node("A"), _node("B"), _node("C")],
        edges=[_edge("A", "B"), _edge("B", "C"), _edge("A", "C")],
    )
    cands = [CompressionCandidate("A", 0.9, "god_node")]
    report = svc.apply(cands)

    # Two edges touch A: A->B and A->C. Both must be invalidated.
    assert any("A" in key for key in report.soft_deleted_edges)
    ab = sink.edges["A|r|B"]
    ac = sink.edges["A|r|C"]
    assert ab.invalid_at is not None
    assert ac.invalid_at is not None
    # B-C untouched.
    bc = sink.edges["B|r|C"]
    assert bc.invalid_at is None


def test_apply_idempotent_on_already_expired(
    svc: CompressionService,
    sink: InMemoryCompressionSink,
) -> None:
    sink.seed(nodes=[_node("A", expired_at=datetime.now(UTC))], edges=[])
    cands = [CompressionCandidate("A", 0.9, "god_node")]
    report = svc.apply(cands)
    assert report.soft_deleted_nodes == []
    assert report.rolled_back is False


def test_apply_q3_policy_check_rejects_hard_delete_method(
    bt: BiTemporalEdgeService,
) -> None:
    """A sink exposing ``delete_node`` triggers policy violation."""

    class BadSink:
        def delete_node(self, eid: str) -> None: ...
        def fetch_node(self, entity_id: str) -> GraphNode | None:
            return None

        def fetch_edges_on_node(self, entity_id: str) -> list[GraphEdge]:
            return []

        def write_node(self, node: GraphNode) -> None: ...
        def write_edge(self, edge: GraphEdge) -> None: ...
        def commit(self) -> None: ...
        def rollback(self) -> None: ...

    svc = CompressionService(sink=BadSink(), bt_service=bt, tenant_id="t1")
    with pytest.raises(CompressionPolicyViolationError):
        svc.apply([CompressionCandidate("X", 0.9, "god_node")])


# =================================================-----------
# Phase 3: rollback
# ============================================================


def test_apply_rolls_back_on_failure(
    bt: BiTemporalEdgeService,
) -> None:
    """If sink.commit raises, partial mutations are undone and error returned."""

    class FailingSink(InMemoryCompressionSink):
        def commit(self) -> None:
            raise RuntimeError("simulated DB outage")

    sink = FailingSink()
    sink.seed(nodes=[_node("A")], edges=[])
    svc = CompressionService(sink=sink, bt_service=bt, tenant_id="t1")
    cands = [CompressionCandidate("A", 0.9, "god_node")]
    report = svc.apply(cands)

    assert report.rolled_back is True
    assert report.error is not None
    assert "simulated DB outage" in str(report.error)
    assert report.soft_deleted_nodes == []
    # The post-rollback commit restores expired_at=None on the node.
    assert sink.nodes["A"].expired_at is None


def test_apply_raises_rollback_error_when_rollback_also_fails(
    bt: BiTemporalEdgeService,
) -> None:
    class DoubleFailingSink(InMemoryCompressionSink):
        commit_calls: int = 0

        def commit(self) -> None:
            self.commit_calls += 1
            raise RuntimeError("commit always fails")

        def rollback(self) -> None:
            raise RuntimeError("rollback also fails")

    sink = DoubleFailingSink()
    sink.seed(nodes=[_node("A")], edges=[])
    svc = CompressionService(sink=sink, bt_service=bt, tenant_id="t1")
    with pytest.raises(CompressionRollbackError):
        svc.apply([CompressionCandidate("A", 0.9, "god_node")])


# ============================================================
# Top-level run()
# ============================================================


def test_run_combines_phases(
    svc: CompressionService,
    sink: InMemoryCompressionSink,
) -> None:
    sink.seed(
        nodes=[_node("God", degree=100), _node("Normal", degree=2)],
        edges=[_edge("God", "Normal")],
    )
    report = svc.run([sink.nodes["God"], sink.nodes["Normal"]], max_candidates=10)
    assert isinstance(report, CompressionReport)
    assert "God" in report.soft_deleted_nodes
    assert sink.nodes["God"].expired_at is not None


def test_run_caps_candidates(
    svc: CompressionService,
    sink: InMemoryCompressionSink,
) -> None:
    sink.seed(
        nodes=[_node(f"N{i}", degree=100) for i in range(5)],
        edges=[],
    )
    report = svc.run(list(sink.nodes.values()), max_candidates=2)
    assert len(report.candidates) <= 5
    assert len(report.soft_deleted_nodes) <= 2


# ============================================================
# InMemoryCompressionSink sanity
# ============================================================


def test_in_memory_sink_seed_and_fetch() -> None:
    sink = InMemoryCompressionSink()
    sink.seed(
        nodes=[_node("A"), _node("B")],
        edges=[_edge("A", "B")],
    )
    assert sink.fetch_node("A") is not None
    edges = sink.fetch_edges_on_node("A")
    assert len(edges) == 1
    assert edges[0].source == "A"
