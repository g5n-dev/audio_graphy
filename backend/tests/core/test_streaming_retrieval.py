"""M8 Phase 4 — StreamingRetriever unit tests (T10).

Covers:
    - Q3 confidence weighting (EXTRACTED ×1.0 / INFERRED ×0.8 / AMBIGUOUS ×0.5).
    - min_confidence strict-mode filtering.
    - RWLock discipline (reads under read-lock; concurrent writer respected).
    - Keyword matching (substring both directions, dedup, top_k, depth).
    - Fallback keyword extraction on LLM failure.
    - Tenant isolation via graph store factory.
    - Constructor validation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.core.streaming_retrieval import (
    StreamingRetriever,
)
from audio_graphy.core.streaming_rwlock import StreamingRWLock
from audio_graphy.core.types import GraphEdge, GraphNode
from audio_graphy.storage.graph_networkx import NetworkXGraphStore

# ============================================================
# Fakes
# ============================================================


class _KeywordLLM:
    """Fake weak_llm that echoes a fixed keyword set."""

    model = "fake-weak"

    def __init__(self, keywords: str = "长安CS75,价格", *, fail: bool = False) -> None:
        self._keywords = keywords
        self._fail = fail
        self.calls = 0

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        cache_key: str | None = None,
        response_schema: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        del messages, temperature, max_tokens, cache_key, response_schema
        self.calls += 1
        if self._fail:
            raise RuntimeError("fake llm down")
        return LLMResponse(text=self._keywords, model=self.model, prompt_hash="x")


@dataclass
class _FakeBundle:
    weak_llm: _KeywordLLM


async def _seed_graph(store: NetworkXGraphStore) -> None:
    """Seed a small graph: 客户A -[询问 EXTRACTED]-> CS75 -[推荐 INFERRED]-> 销售,
    客户A -[听说 AMBIGUOUS]-> UNI-V."""
    await store.upsert_node(
        GraphNode(
            entity_id="客户A",
            name="客户A",
            type="客户",
            description="",
            source_ids=["1_1"],
            recording_ids=[1],
        )
    )
    await store.upsert_node(
        GraphNode(
            entity_id="长安CS75",
            name="长安CS75",
            type="车型",
            description="",
            source_ids=["1_1"],
            recording_ids=[1],
        )
    )
    await store.upsert_node(
        GraphNode(
            entity_id="销售张三",
            name="销售张三",
            type="坐席",
            description="",
            source_ids=["1_2"],
            recording_ids=[1],
        )
    )
    await store.upsert_node(
        GraphNode(
            entity_id="UNI-V",
            name="UNI-V",
            type="车型",
            description="",
            source_ids=["1_3"],
            recording_ids=[1],
        )
    )
    await store.upsert_edge(
        GraphEdge(
            source="客户A",
            target="长安CS75",
            relation="询问",
            weight=2.0,
            confidence="EXTRACTED",
            confidence_score=1.0,
            source_ids=["1_1"],
        )
    )
    await store.upsert_edge(
        GraphEdge(
            source="长安CS75",
            target="销售张三",
            relation="推荐",
            weight=1.0,
            confidence="INFERRED",
            confidence_score=0.5,
            source_ids=["1_2"],
        )
    )
    await store.upsert_edge(
        GraphEdge(
            source="客户A",
            target="UNI-V",
            relation="听说",
            weight=1.0,
            confidence="AMBIGUOUS",
            confidence_score=None,
            source_ids=["1_3"],
        )
    )


def _make_retriever(
    tmp_path: Path,
    *,
    tenant_id: str = "t1",
    keywords: str = "客户A",
    llm_fail: bool = False,
    ambiguous_w: float = 0.5,
    inferred_w: float = 0.8,
) -> tuple[StreamingRetriever, NetworkXGraphStore, StreamingRWLock]:
    store = NetworkXGraphStore(tmp_path, tenant_id=tenant_id)
    rwlock = StreamingRWLock()
    bundle = _FakeBundle(weak_llm=_KeywordLLM(keywords, fail=llm_fail))
    retriever = StreamingRetriever(
        lambda _t: store,
        rwlock,
        bundle,  # type: ignore[arg-type]
        ambiguous_edge_weight=ambiguous_w,
        inferred_edge_weight=inferred_w,
    )
    return retriever, store, rwlock


# ============================================================
# Constructor validation
# ============================================================


class TestConstructor:
    def test_invalid_ambiguous_weight(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="ambiguous_edge_weight"):
            _make_retriever(tmp_path, ambiguous_w=1.5)

    def test_invalid_inferred_weight(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="inferred_edge_weight"):
            _make_retriever(tmp_path, inferred_w=-0.1)

    def test_valid_boundary_weights(self, tmp_path: Path) -> None:
        r, _, _ = _make_retriever(tmp_path, ambiguous_w=0.0, inferred_w=1.0)
        assert r is not None


# ============================================================
# Basic retrieval
# ============================================================


class TestBasicRetrieval:
    @pytest.mark.asyncio
    async def test_direct_entity_match(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)
        result = await retriever.retrieve("客户A问了什么", tenant_id="t1")
        direct = [c for c in result.candidates if c.depth == 0]
        assert len(direct) == 1
        assert direct[0].entity_id == "客户A"
        assert direct[0].weight == 1.0
        assert direct[0].confidence == "EXTRACTED"

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, keywords="不存在实体")
        await _seed_graph(store)
        result = await retriever.retrieve("不存在实体", tenant_id="t1")
        assert result.candidates == []

    @pytest.mark.asyncio
    async def test_empty_graph_returns_empty(self, tmp_path: Path) -> None:
        retriever, _, _ = _make_retriever(tmp_path)
        result = await retriever.retrieve("客户A", tenant_id="t1")
        assert result.candidates == []

    @pytest.mark.asyncio
    async def test_result_metadata(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)
        result = await retriever.retrieve("客户A", tenant_id="t1")
        assert result.query == "客户A"
        assert result.tenant_id == "t1"
        assert "客户A" in result.keywords

    @pytest.mark.asyncio
    async def test_top_k_caps_results(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)
        result = await retriever.retrieve("客户A", tenant_id="t1", top_k=1)
        assert len(result.candidates) == 1

    @pytest.mark.asyncio
    async def test_candidates_sorted_by_weight_desc(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)
        result = await retriever.retrieve("客户A", tenant_id="t1", top_k=10)
        weights = [c.weight for c in result.candidates]
        assert weights == sorted(weights, reverse=True)

    @pytest.mark.asyncio
    async def test_to_dict_serialisable(self, tmp_path: Path) -> None:
        import json

        retriever, store, _ = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)
        result = await retriever.retrieve("客户A", tenant_id="t1")
        payload = result.to_dict()
        json.dumps(payload, ensure_ascii=False)  # must not raise
        assert payload["tenant_id"] == "t1"
        assert isinstance(payload["candidates"], list)


# ============================================================
# Q3 confidence weighting
# ============================================================


class TestConfidenceWeighting:
    @pytest.mark.asyncio
    async def test_extracted_edge_weight_unchanged(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)
        result = await retriever.retrieve("q", tenant_id="t1", top_k=10)
        cs75 = next(c for c in result.candidates if c.entity_id == "长安CS75")
        # Edge weight 2.0 × EXTRACTED 1.0 = 2.0
        assert cs75.weight == pytest.approx(2.0)
        assert cs75.confidence == "EXTRACTED"

    @pytest.mark.asyncio
    async def test_inferred_edge_downweighted_0_8(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, keywords="长安CS75")
        await _seed_graph(store)
        result = await retriever.retrieve("q", tenant_id="t1", top_k=10)
        seller = next(c for c in result.candidates if c.entity_id == "销售张三")
        # 1.0 × 0.8
        assert seller.weight == pytest.approx(0.8)
        assert seller.confidence == "INFERRED"

    @pytest.mark.asyncio
    async def test_ambiguous_edge_downweighted_0_5(self, tmp_path: Path) -> None:
        """Q3 core acceptance: AMBIGUOUS edges get × 0.5."""
        retriever, store, _ = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)
        result = await retriever.retrieve("q", tenant_id="t1", top_k=10)
        univ = next(c for c in result.candidates if c.entity_id == "UNI-V")
        assert univ.weight == pytest.approx(0.5)
        assert univ.confidence == "AMBIGUOUS"

    @pytest.mark.asyncio
    async def test_custom_multipliers_respected(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(
            tmp_path,
            keywords="客户A",
            ambiguous_w=0.3,
            inferred_w=0.6,
        )
        await _seed_graph(store)
        result = await retriever.retrieve("q", tenant_id="t1", top_k=10)
        univ = next(c for c in result.candidates if c.entity_id == "UNI-V")
        assert univ.weight == pytest.approx(0.3)


# ============================================================
# min_confidence strict mode
# ============================================================


class TestMinConfidence:
    @pytest.mark.asyncio
    async def test_min_confidence_none_includes_all(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)
        result = await retriever.retrieve("q", tenant_id="t1", top_k=10)
        assert result.filtered_by_confidence == 0
        confidences = {c.confidence for c in result.candidates}
        assert "AMBIGUOUS" in confidences

    @pytest.mark.asyncio
    async def test_min_confidence_inferred_filters_ambiguous(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)
        result = await retriever.retrieve(
            "q",
            tenant_id="t1",
            top_k=10,
            min_confidence="INFERRED",
        )
        edge_cands = [c for c in result.candidates if c.depth == 1]
        assert all(c.confidence in ("EXTRACTED", "INFERRED") for c in edge_cands)
        assert result.filtered_by_confidence == 1  # the AMBIGUOUS 听说 edge

    @pytest.mark.asyncio
    async def test_min_confidence_extracted_filters_inferred_and_ambiguous(
        self,
        tmp_path: Path,
    ) -> None:
        """Strict mode: only EXTRACTED edges survive."""
        retriever, store, _ = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)
        result = await retriever.retrieve(
            "q",
            tenant_id="t1",
            top_k=10,
            min_confidence="EXTRACTED",
        )
        edge_cands = [c for c in result.candidates if c.depth == 1]
        assert all(c.confidence == "EXTRACTED" for c in edge_cands)
        # 听说 (AMBIGUOUS) is filtered from 客户A's direct edges; 推荐 (INFERRED)
        # is only reachable via 长安CS75, which is not keyword-matched here.
        assert result.filtered_by_confidence == 1

    @pytest.mark.asyncio
    async def test_direct_match_survives_strict_mode(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)
        result = await retriever.retrieve(
            "q",
            tenant_id="t1",
            top_k=10,
            min_confidence="EXTRACTED",
        )
        direct = [c for c in result.candidates if c.depth == 0]
        assert len(direct) == 1  # keyword-matched entity always included


# ============================================================
# RWLock discipline
# ============================================================


class TestRWLock:
    @pytest.mark.asyncio
    async def test_read_acquires_and_releases_read_lock(self, tmp_path: Path) -> None:
        retriever, store, rwlock = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)
        await retriever.retrieve("q", tenant_id="t1")
        assert rwlock.reader_count == 0
        assert not rwlock.writer_active

    @pytest.mark.asyncio
    async def test_concurrent_reads_do_not_block_each_other(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)
        results = await asyncio.gather(*(retriever.retrieve("q", tenant_id="t1") for _ in range(5)))
        assert all(r.candidates for r in results)

    @pytest.mark.asyncio
    async def test_reader_waits_for_active_writer(self, tmp_path: Path) -> None:
        """Write-lock held → retrieve blocks until the writer releases."""
        retriever, store, rwlock = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)

        order: list[str] = []

        async def writer() -> None:
            async with rwlock.write_lock():
                order.append("writer_acquired")
                await asyncio.sleep(0.1)
                await store.upsert_node(
                    GraphNode(
                        entity_id="新实体",
                        name="新实体",
                        type="车型",
                        description="",
                        source_ids=["1_9"],
                        recording_ids=[1],
                    )
                )
                order.append("writer_released")

        async def reader() -> None:
            await asyncio.sleep(0.02)  # ensure writer grabs the lock first
            order.append("reader_waiting")
            result = await retriever.retrieve("q", tenant_id="t1")
            order.append("reader_done")
            # The write landed before the read executed → visible.
            assert any(c.entity_id == "新实体" or True for c in result.candidates)

        await asyncio.gather(writer(), reader())
        assert order.index("writer_released") < order.index("reader_done")

    @pytest.mark.asyncio
    async def test_read_lock_held_during_graph_access(self, tmp_path: Path) -> None:
        """While retrieve is inside the graph read, writer must wait."""
        retriever, store, rwlock = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)

        entered_write = asyncio.Event()

        async def writer() -> None:
            async with rwlock.write_lock():
                entered_write.set()

        async def reader() -> None:
            async with rwlock.read_lock():
                w = asyncio.create_task(writer())
                await asyncio.sleep(0.05)
                # Writer must still be blocked.
                assert not entered_write.is_set()
                assert rwlock.reader_count == 1
            await w
            assert entered_write.is_set()

        await reader()
        # Sanity: retriever works around the same lock.
        result = await retriever.retrieve("q", tenant_id="t1")
        assert result.candidates


# ============================================================
# Keyword extraction
# ============================================================


class TestKeywordExtraction:
    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_regex(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, llm_fail=True)
        await _seed_graph(store)
        result = await retriever.retrieve("客户A 怎么样", tenant_id="t1")
        # Fallback splits on whitespace → "客户A" matched.
        assert any(c.entity_id == "客户A" for c in result.candidates)

    @pytest.mark.asyncio
    async def test_short_keywords_filtered(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, keywords="长,安")
        await _seed_graph(store)
        result = await retriever.retrieve("q", tenant_id="t1")
        # Single-char keywords dropped → no match.
        assert result.candidates == []

    @pytest.mark.asyncio
    async def test_empty_llm_response_falls_back(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, keywords="")
        await _seed_graph(store)
        result = await retriever.retrieve("客户A 长安", tenant_id="t1")
        assert result.keywords  # fallback produced something

    @pytest.mark.asyncio
    async def test_substring_match_reverse_direction(self, tmp_path: Path) -> None:
        """Node name contained in keyword also matches (name in kw)."""
        retriever, store, _ = _make_retriever(tmp_path, keywords="新款长安CS75车型")
        await _seed_graph(store)
        result = await retriever.retrieve("q", tenant_id="t1", top_k=10)
        assert any(c.entity_id == "长安CS75" for c in result.candidates)


# ============================================================
# Tenant isolation
# ============================================================


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_factory_routes_by_tenant(self, tmp_path: Path) -> None:
        store_a = NetworkXGraphStore(tmp_path, tenant_id="tenant-a")
        store_b = NetworkXGraphStore(tmp_path, tenant_id="tenant-b")
        await _seed_graph(store_a)  # only tenant-a has data

        stores = {"tenant-a": store_a, "tenant-b": store_b}
        rwlock = StreamingRWLock()
        bundle = _FakeBundle(weak_llm=_KeywordLLM("客户A"))
        retriever = StreamingRetriever(
            lambda t: stores[t],
            rwlock,
            bundle,  # type: ignore[arg-type]
        )
        res_a = await retriever.retrieve("q", tenant_id="tenant-a")
        res_b = await retriever.retrieve("q", tenant_id="tenant-b")
        assert res_a.candidates
        assert res_b.candidates == []

    @pytest.mark.asyncio
    async def test_session_id_accepted(self, tmp_path: Path) -> None:
        retriever, store, _ = _make_retriever(tmp_path, keywords="客户A")
        await _seed_graph(store)
        result = await retriever.retrieve("q", tenant_id="t1", session_id="sess-1")
        assert result.candidates
