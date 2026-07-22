"""R3 — L6 + L7 spec compliance tests (PRD §8 locked decisions).

These tests verify the R3 fixes for the M9 QA deviations documented in
``docs/m9-qa-report.md`` §10:

    * L6 — Low-degree node merge = ``degree ≤ 1`` + ``rapidfuzz
      fuzz.token_ratio ≥ 85`` (same community) → source node soft-deleted
      (Q3 expired_at=now()), canonical kept.
    * L7 — AMBIGUOUS edges older than 30 days without re-encounter →
      demoted to ``confidence='DEPRECATED'`` + ``expired_at=now()``.
      Re-encounter events within the window keep the edge AMBIGUOUS.
    * DEPRECATED edges are excluded from streaming retrieval (multiplier
      × 0; not even emitted as a candidate).

The original M9 R1 heuristic scoring path is preserved (back-compat)
and exercised in ``test_compression.py`` — these tests focus on the
newly-implemented spec-compliant paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.core.bi_temporal import BiTemporalEdgeService
from audio_graphy.core.compression import (
    CompressionService,
    InMemoryCompressionSink,
    LowDegreeMergeCandidate,
)
from audio_graphy.core.streaming_retrieval import StreamingRetriever
from audio_graphy.core.streaming_rwlock import StreamingRWLock
from audio_graphy.core.types import GraphEdge, GraphNode
from audio_graphy.storage.graph_networkx import NetworkXGraphStore


# ============================================================
# Helpers
# ============================================================


def _node(
    eid: str,
    *,
    name: str | None = None,
    degree: int = 1,
    type_: str = "车型",
    description: str = "d",
    recording_ids: list[int] | None = None,
    expired_at: datetime | None = None,
) -> GraphNode:
    return GraphNode(
        entity_id=eid,
        name=name if name is not None else eid,
        type=type_,
        description=description,
        source_ids=[],
        recording_ids=recording_ids if recording_ids is not None else [1, 2],
        degree=degree,
        expired_at=expired_at,
    )


def _edge(
    s: str,
    t: str,
    *,
    confidence: str = "EXTRACTED",
    score: float | None = 1.0,
    created_at: datetime | None = None,
    expired_at: datetime | None = None,
    relation: str = "r",
) -> GraphEdge:
    return GraphEdge(
        source=s,
        target=t,
        relation=relation,
        weight=1.0,
        confidence=confidence,  # type: ignore[arg-type]
        confidence_score=score,
        created_at=created_at,
        expired_at=expired_at,
    )


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
        tenant_id="t1",
        degree_threshold=1,
        fuzzy_token_ratio=85,
        ambiguous_deprecate_days=30,
    )


# ============================================================
# L6 — degree ≤ 1 + token_ratio ≥ 85 → merge
# ============================================================


class TestL6LowDegreeMerge:
    """L6 locked decision (PRD §8 / architecture §9.3)."""

    def test_l6_merges_when_degree_le_1_and_token_ratio_high(self, svc: CompressionService) -> None:
        """Two nodes, degree=1, fuzz.token_ratio ≥ 85 → 1 merge candidate."""
        nodes = [
            _node("CS75_1", name="CS75 Plus", degree=1),
            _node("CS75_2", name="CS75Plus", degree=1),
        ]
        pairs = svc.select_low_degree_merge_candidates(nodes)
        assert len(pairs) == 1
        pair = pairs[0]
        assert isinstance(pair, LowDegreeMergeCandidate)
        # Deterministic canonical = smaller entity_id.
        assert pair.canonical_entity_id == "CS75_1"
        assert pair.source_entity_id == "CS75_2"
        assert pair.score >= 85

    def test_l6_no_merge_when_degree_exceeds_threshold(self, svc: CompressionService) -> None:
        """degree=2 → excluded from low-degree candidacy even with identical names."""
        nodes = [
            _node("A1", name="长安CS75", degree=2),
            _node("A2", name="长安CS75", degree=0),
        ]
        pairs = svc.select_low_degree_merge_candidates(nodes)
        # Only A2 is eligible (degree ≤ 1) — needs ≥ 2 eligible nodes for a pair.
        assert pairs == []

    def test_l6_no_merge_when_token_ratio_below_threshold(self, svc: CompressionService) -> None:
        """token_ratio < 85 → no merge even at degree=1."""
        nodes = [
            _node("X", name="长安CS75", degree=1),
            _node("Y", name="本田CRV", degree=1),
        ]
        pairs = svc.select_low_degree_merge_candidates(nodes)
        assert pairs == []

    def test_l6_select_candidates_strategy_l6_merge_emits_source_only(
        self, svc: CompressionService,
    ) -> None:
        """The flat ``select_candidates(strategy="l6_merge")`` adapter emits
        the SOURCE side only (canonicals are not soft-delete targets)."""
        nodes = [
            _node("Alpha", name="CS75 Plus", degree=1),
            _node("Beta", name="CS75Plus", degree=1),
        ]
        cands = svc.select_candidates(nodes, strategy="l6_merge")
        assert len(cands) == 1
        # Source side = Beta (canonical is Alpha because "Alpha" < "Beta").
        assert cands[0].entity_id == "Beta"
        assert cands[0].reason == "l6_merge"
        assert 0.0 < cands[0].score <= 1.0

    def test_l6_apply_soft_deletes_source_node(
        self,
        bt: BiTemporalEdgeService,
        sink: InMemoryCompressionSink,
    ) -> None:
        """End-to-end: select L6 pairs → apply → source gets expired_at."""
        nodes = [
            _node("Alpha", name="CS75 Plus", degree=1),
            _node("Beta", name="CS75Plus", degree=1),
        ]
        sink.seed(nodes=nodes, edges=[_edge("Alpha", "Beta")])
        svc_local = CompressionService(
            sink=sink, bt_service=bt, tenant_id="t1",
            degree_threshold=1, fuzzy_token_ratio=85,
        )
        cands = svc_local.select_candidates(nodes, strategy="l6_merge")
        assert len(cands) == 1
        report = svc_local.apply(cands)
        assert report.rolled_back is False
        assert "Beta" in report.soft_deleted_nodes
        # Canonical untouched.
        assert sink.nodes["Alpha"].expired_at is None
        # Source soft-deleted (Q3 SOFT only — no hard delete).
        assert sink.nodes["Beta"].expired_at is not None

    def test_l6_skips_already_expired_nodes(self, svc: CompressionService) -> None:
        """A node with expired_at set is not considered for merging."""
        nodes = [
            _node("Old", name="长安CS75", degree=1, expired_at=datetime.now(UTC)),
            _node("New", name="长安CS75", degree=1),
        ]
        pairs = svc.select_low_degree_merge_candidates(nodes)
        assert pairs == []

    def test_l6_respects_custom_degree_threshold(
        self,
        bt: BiTemporalEdgeService,
        sink: InMemoryCompressionSink,
    ) -> None:
        """degree_threshold=3 catches degree-2 nodes too."""
        nodes = [
            _node("A", name="长安CS75", degree=2),
            _node("B", name="长安CS75", degree=2),
        ]
        svc_local = CompressionService(
            sink=sink, bt_service=bt, tenant_id="t1",
            degree_threshold=3, fuzzy_token_ratio=85,
        )
        pairs = svc_local.select_low_degree_merge_candidates(nodes)
        assert len(pairs) == 1


# ============================================================
# L7 — AMBIGUOUS 30-day deprecation
# ============================================================


class TestL7AmbiguousDeprecation:
    """L7 locked decision (PRD §8 / architecture §9.4)."""

    def test_deprecated_after_30_days_no_re_encounter(
        self,
        bt: BiTemporalEdgeService,
        sink: InMemoryCompressionSink,
    ) -> None:
        """AMBIGUOUS edge older than 30 days with no re-encounter → DEPRECATED."""
        old_created = datetime.now(UTC) - timedelta(days=45)
        edge = _edge("A", "B", confidence="AMBIGUOUS", score=None, created_at=old_created)
        svc_local = CompressionService(
            sink=sink, bt_service=bt, tenant_id="t1",
            ambiguous_deprecate_days=30,
        )
        deprecated_keys, _ = svc_local.deprecate_ambiguous_edges(edges=[edge])
        assert len(deprecated_keys) == 1
        # Sink now has the demoted edge.
        demoted = sink.edges["A|r|B"]
        assert demoted.confidence == "DEPRECATED"
        assert demoted.expired_at is not None

    def test_keeps_ambiguous_when_recent_re_encounter(
        self,
        bt: BiTemporalEdgeService,
        sink: InMemoryCompressionSink,
    ) -> None:
        """A re-encounter event within the 30-day window blocks deprecation."""

        def has_recent_event(_edge: GraphEdge, threshold_dt: datetime) -> bool:
            # Pretend an edge_event row was written 5 days ago.
            return datetime.now(UTC) - timedelta(days=5) >= threshold_dt

        old_created = datetime.now(UTC) - timedelta(days=45)
        edge = _edge("A", "B", confidence="AMBIGUOUS", score=None, created_at=old_created)
        svc_local = CompressionService(
            sink=sink, bt_service=bt, tenant_id="t1",
            ambiguous_deprecate_days=30,
        )
        deprecated_keys, _ = svc_local.deprecate_ambiguous_edges(
            edges=[edge], re_encounter_provider=has_recent_event,
        )
        assert deprecated_keys == []
        # Sink is unchanged.
        assert sink.edges == {}

    def test_keeps_ambiguous_when_within_window(
        self,
        bt: BiTemporalEdgeService,
        sink: InMemoryCompressionSink,
    ) -> None:
        """Edge created 10 days ago is within the 30-day window — not deprecated."""
        recent_created = datetime.now(UTC) - timedelta(days=10)
        edge = _edge("A", "B", confidence="AMBIGUOUS", score=None, created_at=recent_created)
        svc_local = CompressionService(
            sink=sink, bt_service=bt, tenant_id="t1",
            ambiguous_deprecate_days=30,
        )
        deprecated_keys, _ = svc_local.deprecate_ambiguous_edges(edges=[edge])
        assert deprecated_keys == []

    def test_skips_extracted_and_inferred_edges(
        self,
        bt: BiTemporalEdgeService,
        sink: InMemoryCompressionSink,
    ) -> None:
        """Non-AMBIGUOUS confidence tags are never deprecated by L7."""
        old = datetime.now(UTC) - timedelta(days=45)
        edges = [
            _edge("A", "B", confidence="EXTRACTED", score=1.0, created_at=old),
            _edge("C", "D", confidence="INFERRED", score=0.5, created_at=old),
            _edge("E", "F", confidence="AMBIGUOUS", score=None, created_at=old),
        ]
        svc_local = CompressionService(
            sink=sink, bt_service=bt, tenant_id="t1",
            ambiguous_deprecate_days=30,
        )
        deprecated_keys, _ = svc_local.deprecate_ambiguous_edges(edges=edges)
        assert len(deprecated_keys) == 1
        # Only the AMBIGUOUS edge was demoted.
        assert sink.edges["E|r|F"].confidence == "DEPRECATED"
        # EXTRACTED + INFERRED edges were NOT written (no deprecation).
        assert "A|r|B" not in sink.edges
        assert "C|r|D" not in sink.edges

    def test_skips_already_expired_ambiguous(
        self,
        bt: BiTemporalEdgeService,
        sink: InMemoryCompressionSink,
    ) -> None:
        """Already-soft-deleted AMBIGUOUS edges are idempotently skipped."""
        old = datetime.now(UTC) - timedelta(days=45)
        edge = _edge(
            "A", "B", confidence="AMBIGUOUS", score=None,
            created_at=old, expired_at=old,
        )
        svc_local = CompressionService(
            sink=sink, bt_service=bt, tenant_id="t1",
            ambiguous_deprecate_days=30,
        )
        deprecated_keys, _ = svc_local.deprecate_ambiguous_edges(edges=[edge])
        assert deprecated_keys == []


# ============================================================
# DEPRECATED exclusion from retrieval
# ============================================================


class _KeywordLLM:
    """Fake weak_llm that echoes a fixed keyword set."""

    model = "fake-weak"

    def __init__(self, keywords: str = "客户A") -> None:
        self._keywords = keywords

    async def complete(self, messages, *, temperature: float = 0.0, max_tokens=None, cache_key=None) -> LLMResponse:
        return LLMResponse(text=self._keywords, model=self.model, prompt_hash="x")


class _FakeBundle:
    def __init__(self) -> None:
        self.weak_llm = _KeywordLLM()


class TestDeprecatedExcludedFromRetrieval:
    """DEPRECATED edges never appear in streaming retrieval candidates."""

    @pytest.mark.asyncio
    async def test_deprecated_edge_excluded(self, tmp_path: Path) -> None:
        store = NetworkXGraphStore(tmp_path, tenant_id="t1")
        rwlock = StreamingRWLock()
        bundle = _FakeBundle()
        retriever = StreamingRetriever(
            lambda _t: store, rwlock, bundle,  # type: ignore[arg-type]
        )

        await store.upsert_node(GraphNode(
            entity_id="客户A", name="客户A", type="客户",
            description="", source_ids=["1_1"], recording_ids=[1],
        ))
        await store.upsert_node(GraphNode(
            entity_id="GhostCar", name="GhostCar", type="车型",
            description="", source_ids=["1_2"], recording_ids=[1],
        ))
        await store.upsert_node(GraphNode(
            entity_id="LiveCar", name="LiveCar", type="车型",
            description="", source_ids=["1_3"], recording_ids=[1],
        ))
        # DEPRECATED edge — should be filtered out.
        await store.upsert_edge(GraphEdge(
            source="客户A", target="GhostCar", relation="听说",
            weight=2.0, confidence="DEPRECATED", confidence_score=None,
            source_ids=["1_2"],
        ))
        # AMBIGUOUS edge — included (× 0.5 weight).
        await store.upsert_edge(GraphEdge(
            source="客户A", target="LiveCar", relation="听说",
            weight=2.0, confidence="AMBIGUOUS", confidence_score=None,
            source_ids=["1_3"],
        ))

        result = await retriever.retrieve("客户A", tenant_id="t1")
        neighbor_ids = {c.entity_id for c in result.candidates if c.depth == 1}
        assert "GhostCar" not in neighbor_ids, "DEPRECATED edge leaked into retrieval"
        assert "LiveCar" in neighbor_ids, "AMBIGUOUS edge should still appear"
        # Filter counter increments once for the DEPRECATED drop.
        assert result.filtered_by_confidence >= 1
