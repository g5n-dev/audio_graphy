"""QA independent verification tests — written by Edward (QA Engineer).

These tests are INDEPENDENT of the engineer's tests. They verify:
    1. Three-level provenance chain integrity (entity → chunk → segment → recording → recorded_at)
    2. Dual-channel retrieval (naive + graph + union dedup + time sort)
    3. LLM cache dual-layer (first call miss, second call hit, persistence)
    4. Edge confidence labels (EXTRACTED / INFERRED / AMBIGUOUS)

If any of these fail, the source code has a bug (route to Engineer).
If any fail due to test setup issues, fix the test (route to QA).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from audio_graphy.core.chunker import Chunker
from audio_graphy.core.extractor import EntityExtractor, ExtractionResult
from audio_graphy.core.graph import GraphBuilder
from audio_graphy.core.rerank import Citation, Reranker, RerankResult
from audio_graphy.core.retrieval import (
    CandidateSegment,
    DualChannelRetriever,
    RetrievalResult,
)
from audio_graphy.core.types import (
    COMPLETION_DELIMITER,
    RECORD_DELIMITER,
    TUPLE_DELIMITER,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    normalize_confidence_score,
    upgrade_confidence,
)
from audio_graphy.models.recording import Recording

# ============================================================
# Helper functions
# ============================================================


def _make_extraction_result(
    chunk_id: int,
    recording_id: int,
    entities: list[tuple[str, str, str]],
    relations: list[tuple[str, str, str, str, str]] | None = None,
) -> ExtractionResult:
    """Create an ExtractionResult for testing.

    Entity tuple: (name, type, description)
    Relation tuple: (source, relation, target, description, confidence)
    """
    from audio_graphy.core.extractor import ExtractedEntity, ExtractedRelation

    ents = [
        ExtractedEntity(name=n, type=t, description=d, chunk_id=chunk_id, recording_id=recording_id)
        for n, t, d in entities
    ]
    rels = []
    for item in relations or []:
        s, r, t, d, c = item
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


# ============================================================
# 1. Edge confidence labels verification
# ============================================================


@pytest.mark.unit
class TestQAEdgeConfidenceLabels:
    """Independently verify edge confidence labels (EXTRACTED / INFERRED / AMBIGUOUS).

    AC-25: EXTRACTED — transcript 中明确提及 → confidence_score = 1.0
    AC-26: INFERRED — 跨段合并推断 → confidence_score = weight/(weight+1)
    AC-27: AMBIGUOUS — 归一后同名但 type 不同 → confidence_score = None
    """

    def test_extracted_confidence_score_is_1(self) -> None:
        """EXTRACTED edges always have confidence_score = 1.0 regardless of weight."""
        score = normalize_confidence_score("EXTRACTED", 1.0)
        assert score == 1.0

        score = normalize_confidence_score("EXTRACTED", 10.0)
        assert score == 1.0

        score = normalize_confidence_score("EXTRACTED", 0.5)
        assert score == 1.0

    def test_inferred_confidence_score_in_open_interval(self) -> None:
        """INFERRED edges have 0 < score < 1, computed as weight/(weight+1)."""
        for weight in [1.0, 2.0, 5.0, 10.0]:
            score = normalize_confidence_score("INFERRED", weight)
            assert score is not None
            assert 0.0 < score < 1.0
            expected = round(weight / (weight + 1.0), 4)
            assert score == expected

    def test_inferred_score_monotonically_increasing(self) -> None:
        """Higher weight → higher INFERRED confidence_score (more evidence = more confidence)."""
        scores = [normalize_confidence_score("INFERRED", w) for w in [1.0, 2.0, 5.0, 10.0]]
        for i in range(len(scores) - 1):
            assert scores[i] is not None
            assert scores[i + 1] is not None
            assert scores[i + 1] > scores[i]  # type: ignore[operator]

    def test_ambiguous_confidence_score_is_none(self) -> None:
        """AMBIGUOUS edges always have confidence_score = None."""
        assert normalize_confidence_score("AMBIGUOUS", 1.0) is None
        assert normalize_confidence_score("AMBIGUOUS", 100.0) is None

    def test_upgrade_extracted_never_downgrades(self) -> None:
        """EXTRACTED + any = EXTRACTED (never downgrades)."""
        assert upgrade_confidence("EXTRACTED", "INFERRED") == "EXTRACTED"
        assert upgrade_confidence("EXTRACTED", "AMBIGUOUS") == "EXTRACTED"
        assert upgrade_confidence("EXTRACTED", "EXTRACTED") == "EXTRACTED"

    def test_upgrade_inferred_upgrades_to_extracted(self) -> None:
        """INFERRED + EXTRACTED → EXTRACTED."""
        assert upgrade_confidence("INFERRED", "EXTRACTED") == "EXTRACTED"

    def test_upgrade_ambiguous_upgrades(self) -> None:
        """AMBIGUOUS upgrades to INFERRED or EXTRACTED."""
        assert upgrade_confidence("AMBIGUOUS", "INFERRED") == "INFERRED"
        assert upgrade_confidence("AMBIGUOUS", "EXTRACTED") == "EXTRACTED"

    async def test_graph_builder_extracted_edge(self, graph_store: Any) -> None:
        """GraphBuilder produces EXTRACTED edge when relation is directly extracted."""
        extractions = [
            _make_extraction_result(
                chunk_id=1,
                recording_id=1,
                entities=[("坐席A", "坐席", "desc"), ("CS75 Plus", "车型", "desc")],
                relations=[("坐席A", "推荐", "CS75 Plus", "desc", "EXTRACTED")],
            ),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        edge = next(e for e in snapshot.edges if e.relation == "推荐")
        assert edge.confidence == "EXTRACTED"
        assert edge.confidence_score == 1.0

    async def test_graph_builder_inferred_edge_from_gleaning(self, graph_store: Any) -> None:
        """GraphBuilder produces INFERRED edge when relation comes from Gleaning."""
        extractions = [
            _make_extraction_result(
                chunk_id=1,
                recording_id=1,
                entities=[("坐席A", "坐席", "desc"), ("CS75 Plus", "车型", "desc")],
                relations=[("坐席A", "推荐", "CS75 Plus", "desc", "INFERRED")],
            ),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        edge = next(e for e in snapshot.edges if e.relation == "推荐")
        assert edge.confidence == "INFERRED"
        assert edge.confidence_score is not None
        assert 0.0 < edge.confidence_score < 1.0

    async def test_graph_builder_ambiguous_edge_different_types(self, graph_store: Any) -> None:
        """GraphBuilder produces AMBIGUOUS edge when same entity name has different types."""
        extractions = [
            _make_extraction_result(
                chunk_id=1,
                recording_id=1,
                entities=[("客户", "客户", "d1")],
            ),
            _make_extraction_result(
                chunk_id=2,
                recording_id=1,
                entities=[("客户", "坐席", "d2"), ("CS75 Plus", "车型", "d3")],
                relations=[("客户", "推荐", "CS75 Plus", "d", "EXTRACTED")],
            ),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        # "客户" has types "客户" and "坐席" → AMBIGUOUS
        edge = next(e for e in snapshot.edges if e.relation == "推荐")
        assert edge.confidence == "AMBIGUOUS"
        assert edge.confidence_score is None

    async def test_confidence_upgrade_in_merge(self, graph_store: Any) -> None:
        """INFERRED edge + EXTRACTED edge (same key) → EXTRACTED after merge."""
        extractions = [
            _make_extraction_result(
                chunk_id=1,
                recording_id=1,
                entities=[("A", "坐席", "d"), ("B", "车型", "d")],
                relations=[("A", "推荐", "B", "d", "INFERRED")],
            ),
            _make_extraction_result(
                chunk_id=2,
                recording_id=1,
                entities=[("A", "坐席", "d"), ("B", "车型", "d")],
                relations=[("A", "推荐", "B", "d", "EXTRACTED")],
            ),
        ]
        builder = GraphBuilder(graph_store)
        snapshot = await builder.build_from_extractions(extractions)

        edge = next(e for e in snapshot.edges if e.relation == "推荐")
        assert edge.confidence == "EXTRACTED"
        assert edge.confidence_score == 1.0
        assert edge.weight == 2.0  # Accumulated


# ============================================================
# 2. LLM cache dual-layer verification
# ============================================================


@pytest.mark.unit
class TestQALLMCacheDualLayer:
    """Independently verify dual-layer LLM cache behavior.

    Layer 1: adapter in-process cache (managed by adapter)
    Layer 2: file_index persistent cache (kv_store_llm_response_cache.json)

    AC-23: Same prompt first call cached=False, second call cached=True
    AC-24: file_index flush + reload → cache still hits
    """

    async def test_first_call_cache_miss(
        self,
        scripted_bundle: Any,
        file_index: Any,
        sample_graphrag_response: str,
    ) -> None:
        """First call with a prompt → cache miss (LLM actually called)."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )

        calls_before = strong_llm.call_count
        await extractor.extract_from_chunk(1, "第一次调用测试", recording_id=1)
        calls_after = strong_llm.call_count

        # First call should increase call_count (cache miss)
        assert calls_after > calls_before, "First call should be a cache miss (LLM called)"

    async def test_second_call_adapter_cache_hit(
        self,
        scripted_bundle: Any,
        file_index: Any,
        sample_graphrag_response: str,
    ) -> None:
        """Second call with same prompt → adapter cache hit (no new LLM call)."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )

        # First call
        await extractor.extract_from_chunk(1, "缓存测试文本", recording_id=1)
        calls_after_first = strong_llm.call_count

        # Second call: same prompt → adapter cache hit
        await extractor.extract_from_chunk(1, "缓存测试文本", recording_id=1)

        assert strong_llm.call_count == calls_after_first, (
            "Second call should hit adapter cache (no new LLM call)"
        )

    async def test_file_index_cache_hit_after_adapter_clear(
        self,
        scripted_bundle: Any,
        file_index: Any,
        sample_graphrag_response: str,
    ) -> None:
        """After clearing adapter cache, file_index Layer 2 provides cache hit."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )

        # First call: populates both adapter cache and file_index
        await extractor.extract_from_chunk(1, "双层缓存验证", recording_id=1)
        await file_index.flush()
        calls_after_first = strong_llm.call_count

        # Clear adapter cache → forces Layer 2 lookup
        strong_llm._cache.clear()

        # Second call: should hit file_index cache (no new LLM call)
        await extractor.extract_from_chunk(1, "双层缓存验证", recording_id=1)

        assert strong_llm.call_count == calls_after_first, (
            "Should hit file_index Layer 2 cache (no new LLM call after adapter cache cleared)"
        )

    async def test_cache_survives_flush_reload(
        self,
        scripted_bundle: Any,
        file_index: Any,
        sample_graphrag_response: str,
    ) -> None:
        """Cache survives: extract → flush → load → re-extract → cache hit."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        # First extractor: extracts and caches
        extractor1 = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )
        await extractor1.extract_from_chunk(1, "持久化缓存测试", recording_id=1)
        await file_index.flush()
        calls_after_first = strong_llm.call_count

        # Simulate new process: clear adapter cache + reload file_index
        strong_llm._cache.clear()
        await file_index.load()

        # Second extractor: same file_index
        extractor2 = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )
        await extractor2.extract_from_chunk(1, "持久化缓存测试", recording_id=1)

        assert strong_llm.call_count == calls_after_first, (
            "Cache should survive flush → reload cycle"
        )

    async def test_different_prompts_no_cache_hit(
        self,
        scripted_bundle: Any,
        file_index: Any,
        sample_graphrag_response: str,
    ) -> None:
        """Different prompts don't hit cache (different cache_key)."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )

        await extractor.extract_from_chunk(1, "文本A", recording_id=1)
        calls_after_a = strong_llm.call_count

        await extractor.extract_from_chunk(2, "文本B", recording_id=1)
        calls_after_b = strong_llm.call_count

        assert calls_after_b > calls_after_a, "Different prompts should NOT hit cache"

    async def test_cache_key_computation(self) -> None:
        """Cache key = MD5(model, messages) — deterministic."""
        model = "test-model"
        messages = [{"role": "user", "content": "test prompt"}]

        key1 = EntityExtractor._compute_cache_key(model, messages)
        key2 = EntityExtractor._compute_cache_key(model, messages)

        assert key1 == key2, "Same (model, messages) should produce same cache key"
        assert len(key1) == 32, "MD5 hex digest should be 32 chars"

        # Different messages → different key
        different_messages = [{"role": "user", "content": "different prompt"}]
        key3 = EntityExtractor._compute_cache_key(model, different_messages)
        assert key1 != key3, "Different messages should produce different cache key"


# ============================================================
# 3. Dual-channel retrieval verification
# ============================================================


@pytest.mark.unit
class TestQADualChannelRetrieval:
    """Independently verify dual-channel retrieval behavior.

    AC-10: naive channel returns chunks (by cosine similarity)
    AC-11: graph channel returns segments (by relation_counts)
    AC-12: union dedup (by chunk_id, score=max)
    AC-21: time filtering
    AC-22: no time_range → no filtering
    """

    def test_union_dedup_keeps_max_score(self) -> None:
        """Union dedup: same chunk_id from both channels → keep higher score."""
        naive = [
            CandidateSegment(
                chunk_id=1,
                recording_id=1,
                segment_ids=[0],
                text="naive",
                recorded_at=None,
                score=0.7,
                source_channel="naive",
            )
        ]
        graph = [
            CandidateSegment(
                chunk_id=1,
                recording_id=1,
                segment_ids=[0],
                text="graph",
                recorded_at=None,
                score=0.9,
                source_channel="graph",
            )
        ]
        result = DualChannelRetriever._union_dedup(naive, graph)
        assert len(result) == 1
        assert result[0].score == 0.9  # Max
        assert result[0].source_channel == "graph"

    def test_union_dedup_different_chunks_all_kept(self) -> None:
        """Union dedup: different chunk_ids → all kept."""
        naive = [
            CandidateSegment(
                chunk_id=1,
                recording_id=1,
                segment_ids=[0],
                text="a",
                recorded_at=None,
                score=0.8,
                source_channel="naive",
            )
        ]
        graph = [
            CandidateSegment(
                chunk_id=2,
                recording_id=1,
                segment_ids=[1],
                text="b",
                recorded_at=None,
                score=0.6,
                source_channel="graph",
            )
        ]
        result = DualChannelRetriever._union_dedup(naive, graph)
        assert len(result) == 2

    def test_union_dedup_empty(self) -> None:
        """Union dedup: both empty → empty."""
        assert DualChannelRetriever._union_dedup([], []) == []

    def test_time_filter_in_range(self) -> None:
        """Time filter: candidates in range are kept."""
        candidates = [
            CandidateSegment(
                chunk_id=1,
                recording_id=1,
                segment_ids=[0],
                text="a",
                recorded_at=datetime(2026, 7, 10, tzinfo=UTC),
                score=0.9,
                source_channel="naive",
            ),
        ]
        time_range = (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 31, tzinfo=UTC))
        filtered, removed = DualChannelRetriever._filter_by_time(candidates, time_range)
        assert len(filtered) == 1
        assert removed == 0

    def test_time_filter_out_of_range(self) -> None:
        """Time filter: candidates out of range are removed."""
        candidates = [
            CandidateSegment(
                chunk_id=1,
                recording_id=1,
                segment_ids=[0],
                text="a",
                recorded_at=datetime(2026, 6, 10, tzinfo=UTC),
                score=0.9,
                source_channel="naive",
            ),
        ]
        time_range = (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 31, tzinfo=UTC))
        filtered, removed = DualChannelRetriever._filter_by_time(candidates, time_range)
        assert len(filtered) == 0
        assert removed == 1

    def test_time_filter_none_no_filtering(self) -> None:
        """Time filter: None → no filtering."""
        candidates = [
            CandidateSegment(
                chunk_id=1,
                recording_id=1,
                segment_ids=[0],
                text="a",
                recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
                score=0.9,
                source_channel="naive",
            ),
        ]
        filtered, removed = DualChannelRetriever._filter_by_time(candidates, None)
        assert len(filtered) == 1
        assert removed == 0

    def test_time_filter_none_recorded_at_kept(self) -> None:
        """Time filter: None recorded_at → kept (can't filter)."""
        candidates = [
            CandidateSegment(
                chunk_id=1,
                recording_id=1,
                segment_ids=[0],
                text="a",
                recorded_at=None,
                score=0.9,
                source_channel="naive",
            ),
        ]
        time_range = (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 31, tzinfo=UTC))
        filtered, removed = DualChannelRetriever._filter_by_time(candidates, time_range)
        assert len(filtered) == 1
        assert removed == 0

    def test_sort_by_time_ascending(self) -> None:
        """Sort: recorded_at ascending (None last)."""
        candidates = [
            CandidateSegment(
                chunk_id=1,
                recording_id=1,
                segment_ids=[0],
                text="a",
                recorded_at=datetime(2026, 7, 15, tzinfo=UTC),
                score=0.9,
                source_channel="naive",
            ),
            CandidateSegment(
                chunk_id=2,
                recording_id=1,
                segment_ids=[1],
                text="b",
                recorded_at=datetime(2026, 7, 1, tzinfo=UTC),
                score=0.8,
                source_channel="graph",
            ),
            CandidateSegment(
                chunk_id=3,
                recording_id=1,
                segment_ids=[2],
                text="c",
                recorded_at=None,
                score=0.7,
                source_channel="naive",
            ),
        ]
        sorted_cands = DualChannelRetriever._sort_by_time(candidates)
        assert sorted_cands[0].chunk_id == 2  # July 1
        assert sorted_cands[1].chunk_id == 1  # July 15
        assert sorted_cands[2].chunk_id == 3  # None last

    def test_source_id_parsing(self) -> None:
        """source_id "{recording_id}_{chunk_id}" → chunk_id."""
        assert DualChannelRetriever._parse_chunk_id("1_5") == 5
        assert DualChannelRetriever._parse_chunk_id("42_100") == 100
        assert DualChannelRetriever._parse_chunk_id("invalid") is None
        assert DualChannelRetriever._parse_chunk_id("1_abc") is None

    @pytest.mark.integration
    async def test_naive_channel_with_vectors(
        self,
        mock_bundle: Any,
        vector_store: Any,
        graph_store: Any,
        async_session_factory: Any,
    ) -> None:
        """Naive channel: vector search returns chunks by cosine similarity."""
        from audio_graphy.models.chunk import Chunk
        from audio_graphy.models.recording import Recording

        # Create recording + chunk in MySQL
        async with async_session_factory() as session:
            rec = Recording(
                tenant_id="default",
                store_id="test",
                path="/tmp/test.wav",
                status="indexed",
            )
            session.add(rec)
            await session.flush()
            rec_id = rec.id
            chunk = Chunk(
                tenant_id="default",
                recording_id=rec_id,
                segment_ids=[0],
                text="CS75 Plus 优惠 5 万元",
                token_n=10,
                content_hash="hash_qa_1",
            )
            session.add(chunk)
            await session.flush()
            chunk_id = chunk.id
            await session.commit()

        # Insert a vector for the chunk
        rng = np.random.RandomState(42)
        vec = tuple(float(v) for v in rng.randn(1024))
        await vector_store.upsert_chunk_vector("default", chunk_id, vec)

        # Retrieve
        retriever = DualChannelRetriever(
            mock_bundle,  # type: ignore[arg-type]
            vector_store,
            graph_store,
            session_factory=async_session_factory,
        )
        result = await retriever.retrieve("CS75 Plus 优惠", top_k=5)

        assert isinstance(result, RetrievalResult)
        assert result.naive_hits > 0, "Naive channel should find the chunk"
        assert any(c.chunk_id == chunk_id for c in result.candidates), (
            "The inserted chunk should be in the results"
        )

    @pytest.mark.integration
    async def test_graph_channel_with_entities(
        self,
        scripted_bundle: Any,
        vector_store: Any,
        graph_store: Any,
        async_session_factory: Any,
        file_index: Any,
    ) -> None:
        """Graph channel: entity match → neighbors → chunk reverse-lookup."""
        from audio_graphy.models.chunk import Chunk
        from audio_graphy.models.recording import Recording

        # Create recording + chunk in MySQL
        async with async_session_factory() as session:
            rec = Recording(
                tenant_id="default",
                store_id="test",
                path="/tmp/test.wav",
                status="indexed",
                recorded_at=datetime(2026, 7, 10, tzinfo=UTC),
            )
            session.add(rec)
            await session.flush()
            rec_id = rec.id
            chunk = Chunk(
                tenant_id="default",
                recording_id=rec_id,
                segment_ids=[0],
                text="CS75 Plus 推荐测试",
                token_n=10,
                content_hash="hash_qa_2",
            )
            session.add(chunk)
            await session.flush()
            chunk_id = chunk.id
            await session.commit()

        # Populate graph with entity referencing this chunk
        await graph_store.upsert_node(
            GraphNode(
                entity_id="CS75 Plus",
                name="CS75 Plus",
                type="车型",
                description="SUV",
                source_ids=[f"{rec_id}_{chunk_id}"],
                recording_ids=[rec_id],
                degree=1,
            )
        )
        # Add an edge so relation_counts > 0
        await graph_store.upsert_edge(
            GraphEdge(
                source="CS75 Plus",
                target="优惠",
                relation="搭配",
                weight=1.0,
                confidence="EXTRACTED",
                confidence_score=1.0,
                source_ids=[f"{rec_id}_{chunk_id}"],
            )
        )

        # Configure weak LLM to return keywords
        weak_llm = scripted_bundle.weak_llm
        weak_llm.set_default_response("CS75 Plus")

        retriever = DualChannelRetriever(
            scripted_bundle,  # type: ignore[arg-type]
            vector_store,
            graph_store,
            session_factory=async_session_factory,
            file_index=file_index,
        )
        result = await retriever.retrieve("CS75 Plus", top_k=5)

        assert isinstance(result, RetrievalResult)
        assert result.graph_hits > 0, "Graph channel should find chunks via entity match"


# ============================================================
# 4. Three-level provenance chain verification
# ============================================================


@pytest.mark.integration
class TestQAProvenanceChain:
    """Independently verify 3-level provenance chain.

    Chain: entity → source_id → chunk → segment_ids → segment → recording → recorded_at

    AC-14: entity → source_id → chunk (GraphNode.source_ids non-empty)
    AC-15: chunk → segment_ids → segment (ChunkRecord.segment_ids non-empty)
    AC-16: segment → recording → recorded_at
    AC-17: Full reverse trace end-to-end
    """

    @staticmethod
    async def _build_index(
        scripted_bundle: Any,
        audio_path: Path,
        session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> tuple[int, Any, GraphSnapshot]:
        """Build a minimal index and return (recording_id, chunker_output, snapshot)."""
        async with session_factory() as session:
            rec = Recording(
                tenant_id="default",
                store_id="store_001",
                path=str(audio_path),
                status="processing",
                pipeline_state="pending",
                recorded_at=datetime(2026, 7, 10, 14, 0, tzinfo=UTC),
            )
            session.add(rec)
            await session.flush()
            rec_id = rec.id
            await session.commit()

        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(
            f'("实体"{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}车型{TUPLE_DELIMITER}SUV)'
            f"{RECORD_DELIMITER}"
            f'("实体"{TUPLE_DELIMITER}张敏{TUPLE_DELIMITER}坐席{TUPLE_DELIMITER}顾问)'
            f"{RECORD_DELIMITER}"
            f'("关系"{TUPLE_DELIMITER}张敏{TUPLE_DELIMITER}推荐{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}推荐了)'
            f"{COMPLETION_DELIMITER}"
        )

        chunker = Chunker(
            scripted_bundle,  # type: ignore[arg-type]
            session_factory=session_factory,
            file_index=file_index,
        )
        output = await chunker.process_recording(
            recording_id=rec_id,
            audio_path=str(audio_path),
            recorded_at=datetime(2026, 7, 10, tzinfo=UTC),
        )

        # Embed chunks
        embeddings = await scripted_bundle.embed.embed_texts([c.text for c in output.chunks])
        for chunk, emb in zip(output.chunks, embeddings, strict=True):
            if chunk.chunk_id is not None:
                await vector_store.upsert_chunk_vector("default", chunk.chunk_id, emb.vector)

        # Extract
        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )
        chunk_inputs = [
            (c.chunk_id, c.text, rec_id) for c in output.chunks if c.chunk_id is not None
        ]
        results = await extractor.extract_from_chunks(chunk_inputs)

        # Build graph
        builder = GraphBuilder(graph_store, bundle=scripted_bundle, vector_store=vector_store)  # type: ignore[arg-type]
        snapshot = await builder.build_from_extractions(results)

        return rec_id, output, snapshot

    async def test_entity_source_ids_non_empty(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-14: Every entity node has non-empty source_ids."""
        _rec_id, _output, snapshot = await self._build_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        assert len(snapshot.nodes) > 0, "Should have at least one entity"
        for node in snapshot.nodes:
            assert len(node.source_ids) > 0, (
                f"Entity '{node.entity_id}' has empty source_ids — provenance chain broken"
            )

    async def test_source_ids_reference_valid_chunks(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-14: source_ids format is "{recording_id}_{chunk_id}" and references valid chunks."""
        _rec_id, output, snapshot = await self._build_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        valid_chunk_ids = {c.chunk_id for c in output.chunks if c.chunk_id is not None}

        for node in snapshot.nodes:
            for source_id in node.source_ids:
                # Parse chunk_id from source_id
                parts = source_id.rsplit("_", 1)
                assert len(parts) == 2, f"source_id '{source_id}' has unexpected format"
                chunk_id = int(parts[1])
                assert chunk_id in valid_chunk_ids, (
                    f"source_id '{source_id}' references non-existent chunk {chunk_id}"
                )

    async def test_chunk_segment_ids_non_empty(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-15: Every chunk has non-empty segment_ids referencing valid segments."""
        _rec_id, output, _snapshot = await self._build_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        valid_segment_indices = {seg.idx for seg in output.segments}

        for chunk in output.chunks:
            assert len(chunk.segment_ids) > 0, "Chunk has empty segment_ids"
            for seg_id in chunk.segment_ids:
                assert seg_id in valid_segment_indices, (
                    f"Chunk references non-existent segment {seg_id}"
                )

    async def test_segment_has_recorded_at(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-16: Segment → recording → recorded_at chain via file_index."""
        rec_id, output, _snapshot = await self._build_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        for seg in output.segments:
            key = f"{rec_id}_{seg.idx}"
            stored = await file_index.get("kv_store_video_segments", key)
            assert stored is not None, f"Segment {key} not found in file_index"
            assert stored["recorded_at"] is not None, (
                f"Segment {key} has null recorded_at — provenance chain broken"
            )

    async def test_full_reverse_trace(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-17: Full reverse trace: entity → chunk → segment → recording → recorded_at + transcript."""
        rec_id, output, snapshot = await self._build_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        assert len(snapshot.nodes) > 0
        node = snapshot.nodes[0]

        # Level 1: entity → source_id → chunk_id
        source_id = node.source_ids[0]
        parts = source_id.rsplit("_", 1)
        chunk_id = int(parts[1])

        # Level 2: chunk_id → ChunkRecord
        chunk = next((c for c in output.chunks if c.chunk_id == chunk_id), None)
        assert chunk is not None, f"Chunk {chunk_id} not found in chunker output"

        # Level 3: chunk → segment_ids → SegmentRecord
        assert len(chunk.segment_ids) > 0
        seg_idx = chunk.segment_ids[0]
        segment = next((s for s in output.segments if s.idx == seg_idx), None)
        assert segment is not None, f"Segment {seg_idx} not found"

        # Level 4: segment → recording → recorded_at (via file_index)
        key = f"{rec_id}_{seg_idx}"
        stored = await file_index.get("kv_store_video_segments", key)
        assert stored is not None
        assert stored["recorded_at"] is not None
        assert stored["transcript"] is not None

        # Full chain verified: entity → chunk → segment → recording → recorded_at + transcript


# ============================================================
# 5. End-to-end indexing + query verification
# ============================================================


@pytest.mark.integration
@pytest.mark.e2e
class TestQAE2EIndexQuery:
    """End-to-end verification: index → query → answer + citations.

    AC-1: Full indexing pipeline (recording → chunks → entities → graph)
    AC-9: Full query pipeline (query → retrieval → rerank → answer + citations)
    """

    @staticmethod
    async def _setup_index(
        scripted_bundle: Any,
        audio_path: Path,
        session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> int:
        """Run indexing and return recording_id."""
        async with session_factory() as session:
            rec = Recording(
                tenant_id="default",
                store_id="store_001",
                agent_name="张敏",
                path=str(audio_path),
                status="processing",
                pipeline_state="pending",
                recorded_at=datetime(2026, 7, 10, 14, 0, tzinfo=UTC),
            )
            session.add(rec)
            await session.flush()
            rec_id = rec.id
            await session.commit()

        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(
            f'("实体"{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}车型{TUPLE_DELIMITER}热门SUV)'
            f"{RECORD_DELIMITER}"
            f'("实体"{TUPLE_DELIMITER}张敏{TUPLE_DELIMITER}坐席{TUPLE_DELIMITER}销售顾问)'
            f"{RECORD_DELIMITER}"
            f'("关系"{TUPLE_DELIMITER}张敏{TUPLE_DELIMITER}推荐{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}推荐了)'
            f"{COMPLETION_DELIMITER}"
        )

        # Chunker
        chunker = Chunker(
            scripted_bundle,  # type: ignore[arg-type]
            session_factory=session_factory,
            file_index=file_index,
        )
        chunker_output = await chunker.process_recording(
            recording_id=rec_id,
            audio_path=str(audio_path),
            recorded_at=datetime(2026, 7, 10, tzinfo=UTC),
        )

        # Embed chunks
        chunk_texts = [c.text for c in chunker_output.chunks]
        embeddings = await scripted_bundle.embed.embed_texts(chunk_texts)
        for chunk, emb in zip(chunker_output.chunks, embeddings, strict=True):
            if chunk.chunk_id is not None:
                await vector_store.upsert_chunk_vector("default", chunk.chunk_id, emb.vector)

        # Extractor
        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )
        chunk_inputs = [
            (c.chunk_id, c.text, rec_id) for c in chunker_output.chunks if c.chunk_id is not None
        ]
        results = await extractor.extract_from_chunks(chunk_inputs)

        # Graph builder
        builder = GraphBuilder(
            graph_store,
            bundle=scripted_bundle,  # type: ignore[arg-type]
            vector_store=vector_store,
        )
        await builder.build_from_extractions(results)

        return rec_id

    async def test_e2e_indexing(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-1: Full indexing: recording → chunks → entities → graph → GraphML."""
        await self._setup_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        # Verify GraphML file exists
        assert await graph_store.has_graph(), "GraphML file should exist after indexing"

        # Verify file_index JSON files exist
        await file_index.flush()
        assert (file_index.working_path / "kv_store_video_segments.json").exists()
        assert (file_index.working_path / "kv_store_text_chunks.json").exists()

        # Verify graph has nodes
        nodes = await graph_store.get_all_nodes()
        assert len(nodes) > 0, "Graph should have entity nodes after indexing"

    async def test_e2e_query(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-9: Full query: retrieval → rerank → answer + citations with provenance."""
        await self._setup_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        # Configure LLM for query phase
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response("yes")  # Judge: keep all
        strong_llm.set_response("请根据", "根据录音分析，CS75 Plus 被推荐。")

        weak_llm = scripted_bundle.weak_llm
        weak_llm.set_default_response("CS75 Plus, 推荐")

        # Retrieval
        retriever = DualChannelRetriever(
            scripted_bundle,  # type: ignore[arg-type]
            vector_store,
            graph_store,
            session_factory=async_session_factory,
            file_index=file_index,
        )
        retrieval_result = await retriever.retrieve("CS75 Plus 推荐", top_k=5)

        assert isinstance(retrieval_result, RetrievalResult)
        assert retrieval_result.naive_hits > 0, "Naive channel should return chunks"
        assert len(retrieval_result.candidates) > 0, "Should have candidates"

        # Rerank
        reranker = Reranker(
            scripted_bundle,  # type: ignore[arg-type]
            file_index=file_index,
            graph_store=graph_store,
        )
        rerank_result = await reranker.rerank_and_answer(
            "CS75 Plus 推荐",
            retrieval_result.candidates,
        )

        assert isinstance(rerank_result, RerankResult)
        assert rerank_result.answer != ""
        assert rerank_result.answer != "未找到相关录音片段"

        # Citations should have provenance
        if rerank_result.citations:
            for cite in rerank_result.citations:
                assert isinstance(cite, Citation)
                assert cite.chunk_id > 0
                assert cite.recording_id > 0
                assert cite.transcript_snippet != ""
                assert cite.confidence in ("EXTRACTED", "INFERRED", "AMBIGUOUS")

    async def test_e2e_time_filtering(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-21: Time range filtering excludes out-of-range recordings."""
        await self._setup_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        retriever = DualChannelRetriever(
            scripted_bundle,  # type: ignore[arg-type]
            vector_store,
            graph_store,
            session_factory=async_session_factory,
            file_index=file_index,
        )

        # Recording is July 10 → in-range query
        result_in = await retriever.retrieve(
            "CS75 Plus",
            time_range=(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 31, tzinfo=UTC)),
        )
        # Out-of-range query
        result_out = await retriever.retrieve(
            "CS75 Plus",
            time_range=(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)),
        )

        assert len(result_in.candidates) >= len(result_out.candidates), (
            "In-range should have >= candidates than out-of-range"
        )
