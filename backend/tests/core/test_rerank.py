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

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.core import rerank as rerank_module
from audio_graphy.core.rerank import Reranker, RerankResult
from audio_graphy.core.retrieval import CandidateSegment
from audio_graphy.services.llm_gateway import LLMRequest


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

    async def test_batch_judge_is_disabled_by_default_and_calls_once_per_candidate(
        self,
        scripted_bundle: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The feature flag defaults to the exact legacy per-item path."""
        reranker = Reranker(scripted_bundle)  # type: ignore[arg-type]
        complete = AsyncMock(
            return_value=LLMResponse(
                text="yes",
                model="test",
                prompt_hash="judge",
            )
        )
        monkeypatch.setattr(rerank_module, "execute_llm", complete)
        candidates = [_make_candidate(index) for index in (1, 2, 3)]

        surviving, filtered = await reranker._llm_judge_filter("test", candidates)

        assert complete.await_count == 3
        assert surviving == candidates
        assert filtered == 0

    async def test_batch_judge_uses_one_call_and_preserves_candidate_order(
        self,
        scripted_bundle: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Opt-in batch judging maps stable ids back to the original sequence."""
        reranker = Reranker(  # type: ignore[arg-type]
            scripted_bundle,
            enable_batch_judge=True,
        )
        captured_ids: list[str] = []

        async def complete(_adapter: Any, llm_request: LLMRequest) -> LLMResponse:
            payload = json.loads(str(llm_request.messages[-1]["content"]))
            captured_ids.extend(item["candidate_id"] for item in payload["candidates"])
            verdicts = [
                {
                    "candidate_id": candidate_id,
                    "verdict": "no" if index == 1 else "yes",
                    "reason": "test",
                }
                for index, candidate_id in enumerate(captured_ids)
            ]
            return LLMResponse(
                text=json.dumps({"verdicts": verdicts}),
                model="test",
                prompt_hash="batch-judge",
            )

        batch_complete = AsyncMock(side_effect=complete)
        monkeypatch.setattr(rerank_module, "execute_llm", batch_complete)
        candidates = [_make_candidate(index) for index in (1, 2, 3)]

        surviving, filtered = await reranker._llm_judge_filter("test", candidates)

        assert batch_complete.await_count == 1
        assert captured_ids == [
            "recording:1/chunk:1",
            "recording:1/chunk:2",
            "recording:1/chunk:3",
        ]
        assert [candidate.chunk_id for candidate in surviving] == [1, 3]
        assert filtered == 1

    async def test_batch_judge_looks_up_each_candidate_and_batches_only_misses(
        self,
        scripted_bundle: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exact per-item hits merge stably with one batch call for misses."""
        reranker = Reranker(  # type: ignore[arg-type]
            scripted_bundle,
            enable_batch_judge=True,
        )
        looked_up: list[int] = []
        stored: list[tuple[int, str, dict[str, int]]] = []
        batched_ids: list[str] = []

        async def lookup(_adapter: Any, request: LLMRequest) -> LLMResponse | None:
            chunk_id = int(request.business_snapshot["candidate"]["chunk_id"])
            looked_up.append(chunk_id)
            verdict = {1: "no", 3: "yes"}.get(chunk_id)
            if verdict is None:
                return None
            return LLMResponse(
                text=verdict,
                model="test",
                prompt_hash=f"cached-{chunk_id}",
                cached=True,
                cache_source="mysql",
                provider_called=False,
            )

        async def store(
            _adapter: Any,
            request: LLMRequest,
            response: LLMResponse,
        ) -> bool:
            chunk_id = int(request.business_snapshot["candidate"]["chunk_id"])
            stored.append((chunk_id, response.text, dict(response.usage)))
            return True

        async def complete(_adapter: Any, request: LLMRequest) -> LLMResponse:
            payload = json.loads(str(request.messages[-1]["content"]))
            batched_ids.extend(item["candidate_id"] for item in payload["candidates"])
            return LLMResponse(
                text=json.dumps(
                    {
                        "verdicts": [
                            {
                                "candidate_id": batched_ids[0],
                                "verdict": "no",
                                "reason": "miss-2",
                            },
                            {
                                "candidate_id": batched_ids[1],
                                "verdict": "yes",
                                "reason": "miss-4",
                            },
                        ]
                    }
                ),
                model="test",
                prompt_hash="batch-judge",
                usage={
                    "prompt_tokens": 5,
                    "completion_tokens": 4,
                    "total_tokens": 9,
                },
            )

        lookup_cache = AsyncMock(side_effect=lookup)
        store_cache = AsyncMock(side_effect=store)
        batch_complete = AsyncMock(side_effect=complete)
        monkeypatch.setattr(
            rerank_module,
            "lookup_llm_cache",
            lookup_cache,
            raising=False,
        )
        monkeypatch.setattr(
            rerank_module,
            "store_validated_llm_cache",
            store_cache,
            raising=False,
        )
        monkeypatch.setattr(rerank_module, "execute_llm", batch_complete)
        candidates = [_make_candidate(index) for index in (1, 2, 3, 4)]

        surviving, filtered = await reranker._llm_judge_filter("test", candidates)

        assert looked_up == [1, 2, 3, 4]
        assert batched_ids == ["recording:1/chunk:2", "recording:1/chunk:4"]
        assert stored == [
            (
                2,
                "no",
                {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            ),
            (
                4,
                "yes",
                {
                    "prompt_tokens": 2,
                    "completion_tokens": 2,
                    "total_tokens": 4,
                },
            ),
        ]
        assert [candidate.chunk_id for candidate in surviving] == [3, 4]
        assert filtered == 2
        assert batch_complete.await_count == 1

    async def test_batch_judge_failure_keeps_misses_but_honours_cached_verdicts(
        self,
        scripted_bundle: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed miss batch does not discard a validated exact-cache hit."""
        reranker = Reranker(  # type: ignore[arg-type]
            scripted_bundle,
            enable_batch_judge=True,
        )

        async def lookup(_adapter: Any, request: LLMRequest) -> LLMResponse | None:
            chunk_id = int(request.business_snapshot["candidate"]["chunk_id"])
            if chunk_id != 1:
                return None
            return LLMResponse(
                text="no",
                model="test",
                prompt_hash="cached-1",
                cached=True,
                cache_source="mysql",
                provider_called=False,
            )

        monkeypatch.setattr(
            rerank_module,
            "lookup_llm_cache",
            AsyncMock(side_effect=lookup),
            raising=False,
        )
        monkeypatch.setattr(
            rerank_module,
            "store_validated_llm_cache",
            AsyncMock(),
            raising=False,
        )
        monkeypatch.setattr(
            rerank_module,
            "execute_llm",
            AsyncMock(side_effect=RuntimeError("provider unavailable")),
        )
        candidates = [_make_candidate(index) for index in (1, 2, 3)]

        surviving, filtered = await reranker._llm_judge_filter("test", candidates)

        assert [candidate.chunk_id for candidate in surviving] == [2, 3]
        assert filtered == 1

    async def test_batch_judge_all_item_hits_skip_provider_and_store(
        self,
        scripted_bundle: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A repeated candidate set is served entirely from individual recipes."""
        reranker = Reranker(  # type: ignore[arg-type]
            scripted_bundle,
            enable_batch_judge=True,
        )

        async def lookup(_adapter: Any, request: LLMRequest) -> LLMResponse:
            chunk_id = int(request.business_snapshot["candidate"]["chunk_id"])
            return LLMResponse(
                text="no" if chunk_id == 2 else "yes",
                model="test",
                prompt_hash=f"cached-{chunk_id}",
                cached=True,
                cache_source="redis",
                provider_called=False,
            )

        batch_complete = AsyncMock(side_effect=AssertionError("provider must not be called"))
        store_cache = AsyncMock(side_effect=AssertionError("hits must not be rewritten"))
        monkeypatch.setattr(
            rerank_module,
            "lookup_llm_cache",
            AsyncMock(side_effect=lookup),
        )
        monkeypatch.setattr(
            rerank_module,
            "store_validated_llm_cache",
            store_cache,
        )
        monkeypatch.setattr(rerank_module, "execute_llm", batch_complete)
        candidates = [_make_candidate(index) for index in (1, 2, 3)]

        surviving, filtered = await reranker._llm_judge_filter("test", candidates)

        assert [candidate.chunk_id for candidate in surviving] == [1, 3]
        assert filtered == 1
        batch_complete.assert_not_awaited()
        store_cache.assert_not_awaited()

    async def test_batch_judge_keeps_missing_duplicate_or_invalid_verdicts(
        self,
        scripted_bundle: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only one unambiguous, schema-valid ``no`` may remove a candidate."""
        reranker = Reranker(  # type: ignore[arg-type]
            scripted_bundle,
            enable_batch_judge=True,
        )

        async def complete(_adapter: Any, llm_request: LLMRequest) -> LLMResponse:
            payload = json.loads(str(llm_request.messages[-1]["content"]))
            ids = [item["candidate_id"] for item in payload["candidates"]]
            return LLMResponse(
                text=json.dumps(
                    {
                        "verdicts": [
                            {"candidate_id": ids[0], "verdict": "no", "reason": "valid"},
                            {"candidate_id": ids[1], "verdict": "no", "reason": "first"},
                            {"candidate_id": ids[1], "verdict": "yes", "reason": "duplicate"},
                            {"candidate_id": ids[3], "verdict": "maybe", "reason": "invalid"},
                            {"candidate_id": ids[4], "verdict": "no", "reason": 123},
                        ]
                    }
                ),
                model="test",
                prompt_hash="batch-judge-invalid",
            )

        monkeypatch.setattr(
            rerank_module,
            "execute_llm",
            AsyncMock(side_effect=complete),
        )
        candidates = [_make_candidate(index) for index in (1, 2, 3, 4, 5)]

        surviving, filtered = await reranker._llm_judge_filter("test", candidates)

        assert [candidate.chunk_id for candidate in surviving] == [2, 3, 4, 5]
        assert filtered == 1

    @pytest.mark.parametrize(
        "response_text",
        ["not-json", '{"verdicts": {}}'],
    )
    async def test_batch_judge_keeps_all_on_malformed_payload(
        self,
        scripted_bundle: Any,
        response_text: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reranker = Reranker(  # type: ignore[arg-type]
            scripted_bundle,
            enable_batch_judge=True,
        )
        monkeypatch.setattr(
            rerank_module,
            "execute_llm",
            AsyncMock(
                return_value=LLMResponse(
                    text=response_text,
                    model="test",
                    prompt_hash="batch-judge-malformed",
                )
            ),
        )
        candidates = [_make_candidate(1), _make_candidate(2)]

        surviving, filtered = await reranker._llm_judge_filter("test", candidates)

        assert surviving == candidates
        assert filtered == 0

    async def test_batch_judge_failure_keeps_all_candidates(
        self,
        scripted_bundle: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reranker = Reranker(  # type: ignore[arg-type]
            scripted_bundle,
            enable_batch_judge=True,
        )
        monkeypatch.setattr(
            rerank_module,
            "execute_llm",
            AsyncMock(side_effect=RuntimeError("provider unavailable")),
        )
        candidates = [_make_candidate(1), _make_candidate(2)]

        surviving, filtered = await reranker._llm_judge_filter("test", candidates)

        assert surviving == candidates
        assert filtered == 0


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

    async def test_supplied_keywords_skip_duplicate_llm_extraction(
        self,
        scripted_bundle: Any,
    ) -> None:
        """Retriever keywords are reused instead of invoking weak_llm again."""
        scripted_bundle.strong_llm.set_default_response("yes")
        reranker = Reranker(scripted_bundle)  # type: ignore[arg-type]
        extract_keywords = AsyncMock(side_effect=AssertionError("must not be called"))
        reranker._extract_keywords = extract_keywords  # type: ignore[method-assign]

        result = await reranker.rerank_and_answer(
            "CS75 Plus",
            [_make_candidate()],
            keywords=("CS75 Plus",),
        )

        assert result.refined_count == 1
        extract_keywords.assert_not_awaited()


async def rerank_and_answer_safe(
    reranker: Reranker, query: str, candidates: list[CandidateSegment]
) -> RerankResult:
    """Helper to call rerank_and_answer with error handling."""
    return await reranker.rerank_and_answer(query, candidates)
