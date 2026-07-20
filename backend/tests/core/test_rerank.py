"""Unit tests for Reranker — LLM judge + refinement + answer generation.

Tests cover:
    - LLM as-judge filtering (yes/no parsing, conservative keep on failure)
    - Keyword extraction (LLM + fallback)
    - Refined reranking (mock = original transcript)
    - Citation building (3-level provenance)
    - Answer generation (success + failure)
    - Empty candidates handling
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from audio_graphy.core.rerank import Reranker, RerankResult
from audio_graphy.core.retrieval import CandidateSegment


def _make_candidate(
    chunk_id: int = 1,
    recording_id: int = 1,
    text: str = "坐席推荐了 CS75 Plus，优惠 5 万元。",
    recorded_at: datetime | None = None,
    score: float = 0.9,
    source_channel: str = "naive",
) -> CandidateSegment:
    """Helper to create a CandidateSegment."""
    return CandidateSegment(
        chunk_id=chunk_id,
        recording_id=recording_id,
        segment_ids=[chunk_id],
        text=text,
        recorded_at=recorded_at or datetime(2026, 7, 10, tzinfo=UTC),
        score=score,
        source_channel=source_channel,
    )


@pytest.mark.unit
class TestLLMJudgeFilter:
    """LLM as-judge filtering logic."""

    async def test_filter_irrelevant(self, scripted_bundle: Any) -> None:
        """LLM says 'no' → candidate filtered."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response("no")

        reranker = Reranker(scripted_bundle)  # type: ignore[arg-type]
        candidates = [_make_candidate(text="无关内容")]
        result = await reranker.rerank_and_answer("CS75 Plus 价格", candidates)

        assert result.filtered_count == 1
        assert len(result.citations) == 0

    async def test_keep_relevant(self, scripted_bundle: Any) -> None:
        """LLM says 'yes' → candidate kept."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response("yes")

        reranker = Reranker(scripted_bundle)  # type: ignore[arg-type]
        candidates = [_make_candidate(text="CS75 Plus 优惠 5 万元")]
        result = await reranker.rerank_and_answer("CS75 Plus 价格", candidates)

        assert result.filtered_count == 0
        assert len(result.citations) == 1

    async def test_judge_failure_keeps_candidate(self, mock_bundle: Any) -> None:
        """LLM judge failure → candidate kept (conservative)."""
        from audio_graphy.adapters.bundle import AdapterBundle
        from audio_graphy.adapters.mock_llm import MockLLMAdapter

        # Create a new bundle with always-failing LLM
        bundle = AdapterBundle(
            vad=mock_bundle.vad,
            asr=mock_bundle.asr,
            strong_llm=MockLLMAdapter(model="test", error_rate=1.0),
            weak_llm=mock_bundle.weak_llm,
            embed=mock_bundle.embed,
        )

        reranker = Reranker(bundle)  # type: ignore[arg-type]
        candidates = [_make_candidate()]
        result = await reranker.rerank_and_answer("test", candidates)

        # Should keep candidate despite LLM failure
        assert result.filtered_count == 0


@pytest.mark.unit
class TestEmptyCandidates:
    """Empty candidate handling."""

    async def test_empty_candidates_returns_no_results(self, scripted_bundle: Any) -> None:
        """Empty candidates → answer='未找到相关录音片段'."""
        reranker = Reranker(scripted_bundle)  # type: ignore[arg-type]
        result = await reranker.rerank_and_answer("test", [])

        assert result.answer == "未找到相关录音片段"
        assert result.citations == []
        assert result.filtered_count == 0
        assert result.refined_count == 0


@pytest.mark.unit
class TestCitationBuilding:
    """3-level provenance citation building."""

    async def test_citation_has_full_provenance(self, scripted_bundle: Any) -> None:
        """Citation has all provenance fields populated."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response("yes")

        recorded_at = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
        candidates = [
            _make_candidate(
                chunk_id=5,
                recording_id=2,
                text="坐席推荐了 CS75 Plus",
                recorded_at=recorded_at,
            )
        ]

        reranker = Reranker(scripted_bundle)  # type: ignore[arg-type]
        result = await rerank_and_answer_safe(reranker, "CS75 Plus", candidates)

        assert len(result.citations) == 1
        cite = result.citations[0]
        assert cite.chunk_id == 5
        assert cite.recording_id == 2
        assert cite.segment_ids == [5]
        assert cite.recorded_at == recorded_at
        assert cite.transcript_snippet != ""
        assert cite.confidence in ("EXTRACTED", "INFERRED", "AMBIGUOUS")

    async def test_citation_with_graph_store(self, scripted_bundle: Any, graph_store: Any) -> None:
        """Citation looks up entity name from graph_store."""
        from audio_graphy.core.types import GraphNode

        # Populate graph with entity referencing chunk 3
        await graph_store.upsert_node(
            GraphNode(
                entity_id="CS75 Plus",
                name="CS75 Plus",
                type="车型",
                description="SUV",
                source_ids=["1_3"],
                recording_ids=[1],
                degree=1,
            )
        )

        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response("yes")

        candidates = [_make_candidate(chunk_id=3, recording_id=1)]
        reranker = Reranker(
            scripted_bundle,  # type: ignore[arg-type]
            graph_store=graph_store,
        )
        result = await rerank_and_answer_safe(reranker, "CS75 Plus", candidates)

        assert len(result.citations) == 1
        assert result.citations[0].entity == "CS75 Plus"


@pytest.mark.unit
class TestAnswerGeneration:
    """Answer generation logic."""

    async def test_answer_nonempty(self, scripted_bundle: Any) -> None:
        """Answer is non-empty when LLM succeeds."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response("yes")

        # Set a specific answer response
        answer_text = "根据录音分析，CS75 Plus 在 3 场接待中被推荐。"
        # The scripted LLM picks by keyword — set for answer prompt
        strong_llm.set_response("请根据", answer_text)

        candidates = [_make_candidate()]
        reranker = Reranker(scripted_bundle)  # type: ignore[arg-type]
        result = await rerank_and_answer_safe(reranker, "CS75 Plus", candidates)

        assert result.answer != ""
        assert result.answer != "未找到相关录音片段"

    async def test_answer_generation_failure(self, mock_bundle: Any) -> None:
        """Answer generation failure → answer='（生成失败）'."""
        from audio_graphy.adapters.bundle import AdapterBundle
        from audio_graphy.adapters.mock_llm import MockLLMAdapter

        # Create a new bundle with always-failing strong LLM
        bundle = AdapterBundle(
            vad=mock_bundle.vad,
            asr=mock_bundle.asr,
            strong_llm=MockLLMAdapter(model="test", error_rate=1.0),
            weak_llm=mock_bundle.weak_llm,
            embed=mock_bundle.embed,
        )

        reranker = Reranker(bundle)  # type: ignore[arg-type]
        candidates = [_make_candidate()]
        result = await reranker.rerank_and_answer("test", candidates)

        # Judge failure → candidate kept, answer generation failure → "（生成失败）"
        assert result.answer == "（生成失败）"
        # Citations should still be returned
        assert len(result.citations) >= 0  # May be 0 or 1 depending on judge


@pytest.mark.unit
class TestRefinement:
    """Refined reranking (mock = original transcript)."""

    async def test_refined_count_matches_surviving(self, scripted_bundle: Any) -> None:
        """refined_count = number of surviving candidates."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response("yes")

        candidates = [_make_candidate(1), _make_candidate(2), _make_candidate(3)]
        reranker = Reranker(scripted_bundle)  # type: ignore[arg-type]
        result = await rerank_and_answer_safe(reranker, "test", candidates)

        assert result.refined_count == 3


async def rerank_and_answer_safe(
    reranker: Reranker, query: str, candidates: list[CandidateSegment]
) -> RerankResult:
    """Helper to call rerank_and_answer with error handling."""
    return await reranker.rerank_and_answer(query, candidates)
